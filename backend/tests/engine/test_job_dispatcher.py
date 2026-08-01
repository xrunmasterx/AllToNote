from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.engine.job_dispatcher as job_dispatcher_module
from app.adapters.jobs.machine_resource_lease import MachineResourceLeaseStore
from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.adapters.jobs.workspace_instance_registry import WorkspaceInstanceRegistry
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.external_operation import ExternalOutcome
from app.core.jobs.model import (
    JobExecutionBinding,
    JobExecutionOwner,
    JobState,
)
from app.core.jobs.resource_lease import (
    HEAVY_PRODUCTION_RESOURCE_NAME,
    JobExecutionAuthority,
    ResourceOwner,
)
from app.engine.contracts import EngineJobReference, EngineWorkerLaunchV1
from app.engine.job_dispatcher import EngineJobDispatcher
from app.runtime_paths import RuntimePaths, resolve_runtime_paths


_BINDING = JobExecutionBinding(
    recipe_id="alltonote.document-note",
    recipe_version=1,
    executor_id="alltonote.document",
    executor_version=1,
    pack_id="document-basic",
    pack_version="docling-2.117.0-tableformer-v2.3.0",
)


def test_default_worker_command_preserves_all_runtime_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = RuntimePaths(
        config_dir=tmp_path / "config root" / "AllToNote",
        data_dir=tmp_path / "data root" / "AllToNote",
        cache_dir=tmp_path / "cache root" / "AllToNote",
        state_dir=tmp_path / "state root" / "AllToNote",
        log_dir=tmp_path / "log root" / "AllToNote",
    )
    observed: list[tuple[tuple[str, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        "app.engine.job_dispatcher.run_worker_process",
        lambda command, **kwargs: observed.append((command, kwargs)) or 0,
    )
    reference = EngineJobReference(
        "1" * 32,
        "job_018f0000-0000-7000-8000-000000000001",
    )
    owner = ResourceOwner("workspace", "engine-worker")
    from app.core.jobs.resource_lease import (
        JobExecutionAuthority,
        ResourceLeaseHandoff,
    )

    launch = EngineWorkerLaunchV1(
        1,
        reference,
        ResourceLeaseHandoff(
            1,
            HEAVY_PRODUCTION_RESOURCE_NAME,
            owner,
            1,
            30_000,
            "a" * 43,
        ),
        JobExecutionAuthority(owner.process_instance_id, 1, reference.job_id),
    )

    assert EngineJobDispatcher(paths)._run_worker(launch, lambda: None) == 0
    command, options = observed[0]
    assert options["stdin_payload"] == launch.to_bytes()
    for flag, expected in (
        ("--config-dir", paths.config_dir),
        ("--data-dir", paths.data_dir),
        ("--cache-dir", paths.cache_dir),
        ("--state-dir", paths.state_dir),
        ("--log-dir", paths.log_dir),
    ):
        assert command[command.index(flag) + 1] == str(expected)


def test_dispatcher_admits_resource_and_exact_job_authority_before_worker(
    tmp_path: Path,
) -> None:
    paths, repository, job, reference = _registered_job(
        tmp_path,
        client_request_id="pre-admission",
    )
    observed = threading.Event()

    def run_worker(launch: EngineWorkerLaunchV1, check_running) -> int:
        assert launch.reference == reference
        store = MachineResourceLeaseStore.open(paths.data_dir / "machine")
        adopted = store.adopt(launch.resource_handoff, ttl_seconds=30)
        assert adopted.owner.process_instance_id == launch.job_authority.owner_id
        claim = repository.claim_job(
            job.job_id,
            adopted.owner.process_instance_id,
            ttl_seconds=30,
        )
        assert claim.authority == launch.job_authority
        check_running()
        repository.cancel_job(job.job_id)
        observed.set()
        return 0

    dispatcher = EngineJobDispatcher(
        paths,
        worker_runner=run_worker,
        reconcile_interval_seconds=60,
    )
    try:
        dispatcher.start()
        assert observed.wait(timeout=5)
        for _index in range(500):
            if repository.get_job(job.job_id).state is JobState.CANCELLED:
                break
            threading.Event().wait(0.01)
    finally:
        dispatcher.close(force=True)

    assert repository.get_job(job.job_id).state is JobState.CANCELLED


def test_dispatcher_resource_busy_starts_no_worker_and_keeps_job_durable(
    tmp_path: Path,
) -> None:
    paths, repository, job, reference = _registered_job(
        tmp_path,
        client_request_id="resource-busy",
    )
    store = MachineResourceLeaseStore.open(paths.data_dir / "machine")
    competing = store.acquire(
        HEAVY_PRODUCTION_RESOURCE_NAME,
        ResourceOwner("another-workspace", "another-runtime"),
        ttl_seconds=30,
    )
    called = threading.Event()
    dispatcher = EngineJobDispatcher(
        paths,
        worker_runner=lambda _launch, _check: called.set() or 0,
        reconcile_interval_seconds=60,
    )
    try:
        dispatcher.start()
        dispatcher.notify(reference)
        threading.Event().wait(0.2)
    finally:
        dispatcher.close(force=True)
        competing.release()

    assert called.is_set() is False
    assert repository.get_job(job.job_id).state is JobState.QUEUED


def test_dispatcher_spawn_failure_releases_both_preclaims(
    tmp_path: Path,
) -> None:
    paths, repository, job, reference = _registered_job(
        tmp_path,
        client_request_id="spawn-failure",
    )
    attempted = threading.Event()

    def fail_spawn(_launch: EngineWorkerLaunchV1, _check_running) -> int:
        attempted.set()
        raise OSError("spawn failed")

    dispatcher = EngineJobDispatcher(
        paths,
        worker_runner=fail_spawn,
        reconcile_interval_seconds=60,
    )
    try:
        dispatcher.start()
        assert attempted.wait(timeout=5)
        for _index in range(500):
            if not dispatcher.has_work:
                break
            threading.Event().wait(0.01)
    finally:
        dispatcher.close(force=True)

    store = MachineResourceLeaseStore.open(paths.data_dir / "machine")
    resource = store.acquire(
        HEAVY_PRODUCTION_RESOURCE_NAME,
        ResourceOwner("identity-spawn-failure", "replacement-worker"),
        ttl_seconds=30,
    )
    claim = repository.claim_job(job.job_id, "replacement-worker", ttl_seconds=30)
    assert claim.authority.owner_id == "replacement-worker"
    repository.release_job_claim(claim.authority)
    resource.release()


def _registered_job(
    tmp_path: Path,
    *,
    client_request_id: str,
    execution_owner: JobExecutionOwner = JobExecutionOwner.ENGINE,
    principal: str = "local-user",
):
    local_parent = tmp_path / "local data"
    local_parent.mkdir(exist_ok=True)
    paths = resolve_runtime_paths(local_data_parent=local_parent)
    workspace = tmp_path / f"workspace-{client_request_id}"
    workspace.mkdir()
    identity = f"identity-{client_request_id}"
    (workspace / "workspace-id.txt").write_text(identity, encoding="utf-8")
    registry = WorkspaceInstanceRegistry(
        local_parent,
        inspect_workspace=lambda root: (root / "workspace-id.txt").read_text(
            encoding="utf-8"
        ),
    )
    instance = registry.resolve(workspace)
    repository = SqliteJobRepository.open(instance.machine_root / "job-store")
    job = repository.create_job(
        request_hash="sha256:" + "a" * 64,
        principal=principal,
        client_request_id=client_request_id,
        execution_owner=execution_owner,
        execution_binding=_BINDING,
    )
    reference = EngineJobReference(instance.instance_id, job.job_id)
    return paths, repository, job, reference


def test_startup_reconcile_dispatches_persisted_engine_job_without_notify(
    tmp_path: Path,
) -> None:
    paths, repository, job, reference = _registered_job(
        tmp_path,
        client_request_id="lost-notify",
    )
    observed = threading.Event()
    calls: list[EngineJobReference] = []

    def run_worker(launch: EngineWorkerLaunchV1, _check_running) -> int:
        calls.append(launch.reference)
        repository.cancel_job(job.job_id)
        observed.set()
        return 0

    dispatcher = EngineJobDispatcher(
        paths,
        worker_runner=run_worker,
        reconcile_interval_seconds=60,
    )
    try:
        dispatcher.start()
        assert observed.wait(timeout=5)
    finally:
        dispatcher.close(force=True)

    assert calls == [reference]
    assert repository.get_job(job.job_id).state is JobState.CANCELLED


def test_duplicate_notify_is_idempotent_and_keeps_one_pending_reference(
    tmp_path: Path,
) -> None:
    paths, _repository, _job, reference = _registered_job(
        tmp_path,
        client_request_id="duplicate-notify",
    )
    dispatcher = EngineJobDispatcher(paths)

    first = dispatcher.notify(reference)
    second = dispatcher.notify(reference)

    assert first.scheduled is True
    assert second.scheduled is False
    assert dispatcher.pending_count == 1


def test_concurrent_duplicate_notifications_schedule_one_reference(
    tmp_path: Path,
) -> None:
    paths, _repository, _job, reference = _registered_job(
        tmp_path,
        client_request_id="concurrent-duplicate",
    )
    dispatcher = EngineJobDispatcher(paths)

    with ThreadPoolExecutor(max_workers=32) as executor:
        receipts = tuple(executor.map(lambda _index: dispatcher.notify(reference), range(32)))

    assert sum(receipt.scheduled for receipt in receipts) == 1
    assert dispatcher.pending_count == 1


def test_notify_rejects_foreground_owned_job(tmp_path: Path) -> None:
    paths, _repository, _job, reference = _registered_job(
        tmp_path,
        client_request_id="foreground",
        execution_owner=JobExecutionOwner.FOREGROUND,
    )
    dispatcher = EngineJobDispatcher(paths)

    with pytest.raises(DomainError) as raised:
        dispatcher.notify(reference)

    assert raised.value.code == "engine_job_authority_denied"
    assert raised.value.category is ErrorCategory.POLICY_DENIED
    assert dispatcher.pending_count == 0


def test_notify_rejects_non_local_principal(tmp_path: Path) -> None:
    paths, _repository, _job, reference = _registered_job(
        tmp_path,
        client_request_id="non-local-principal",
        principal="remote-agent",
    )
    dispatcher = EngineJobDispatcher(paths)

    with pytest.raises(DomainError) as raised:
        dispatcher.notify(reference)

    assert raised.value.code == "engine_job_authority_denied"
    assert raised.value.category is ErrorCategory.POLICY_DENIED
    assert dispatcher.pending_count == 0


def test_notify_observes_terminal_job_without_requeue(tmp_path: Path) -> None:
    paths, repository, job, reference = _registered_job(
        tmp_path,
        client_request_id="terminal",
    )
    repository.cancel_job(job.job_id)
    dispatcher = EngineJobDispatcher(paths)

    receipt = dispatcher.notify(reference)

    assert receipt.state is JobState.CANCELLED
    assert receipt.scheduled is False
    assert dispatcher.pending_count == 0


def test_pending_queue_is_bounded_and_reports_backpressure(tmp_path: Path) -> None:
    paths, _first_repository, _first_job, first = _registered_job(
        tmp_path,
        client_request_id="first",
    )
    _paths, _second_repository, _second_job, second = _registered_job(
        tmp_path,
        client_request_id="second",
    )
    dispatcher = EngineJobDispatcher(paths, maximum_pending_jobs=1)

    dispatcher.notify(first)
    with pytest.raises(DomainError) as raised:
        dispatcher.notify(second)

    assert raised.value.code == "engine_notification_backpressure"
    assert raised.value.category is ErrorCategory.RETRYABLE_RUNTIME
    assert dispatcher.pending_count == 1


def test_draining_dispatcher_rejects_new_notifications(tmp_path: Path) -> None:
    paths, _repository, _job, reference = _registered_job(
        tmp_path,
        client_request_id="draining",
    )
    dispatcher = EngineJobDispatcher(paths)

    assert dispatcher.try_begin_draining() is True
    with pytest.raises(DomainError) as raised:
        dispatcher.notify(reference)

    assert raised.value.code == "engine_draining"
    assert dispatcher.pending_count == 0


def test_failed_oldest_job_does_not_starve_lost_notification_job(
    tmp_path: Path,
) -> None:
    paths, poison_repository, poison_job, poison = _registered_job(
        tmp_path,
        client_request_id="a-poison",
    )
    _paths, healthy_repository, healthy_job, healthy = _registered_job(
        tmp_path,
        client_request_id="z-healthy",
    )
    healthy_observed = threading.Event()
    calls: list[EngineJobReference] = []

    def run_worker(launch: EngineWorkerLaunchV1, _check_running) -> int:
        candidate = launch.reference
        calls.append(candidate)
        if candidate == poison and calls.count(poison) == 1:
            return 1
        if candidate == poison:
            poison_repository.cancel_job(poison_job.job_id)
        else:
            healthy_repository.cancel_job(healthy_job.job_id)
            healthy_observed.set()
        return 0

    dispatcher = EngineJobDispatcher(
        paths,
        worker_runner=run_worker,
        maximum_pending_jobs=1,
        reconcile_interval_seconds=0.01,
    )
    try:
        dispatcher.start()
        assert healthy_observed.wait(timeout=5)
        deadline = threading.Event()
        for _index in range(500):
            if not dispatcher.has_work:
                break
            deadline.wait(0.01)
        assert dispatcher.has_work is False
    finally:
        dispatcher.close(force=True)

    assert calls[:2] == [poison, healthy]


def test_repeated_worker_failure_becomes_terminal_and_is_not_restarted(
    tmp_path: Path,
) -> None:
    paths, repository, job, reference = _registered_job(
        tmp_path,
        client_request_id="worker-retry-exhausted",
    )
    terminal = threading.Event()
    calls = 0

    def fail_worker(_launch: EngineWorkerLaunchV1, _check_running) -> int:
        nonlocal calls
        calls += 1
        assert repository.get_engine_worker_launch_count(job.job_id) == calls
        if calls > 3:
            repository.cancel_job(job.job_id)
            terminal.set()
        return 70

    dispatcher = EngineJobDispatcher(
        paths,
        worker_runner=fail_worker,
        reconcile_interval_seconds=0.01,
    )
    try:
        dispatcher.start()
        for _index in range(1_000):
            if repository.get_job(job.job_id).state in {
                JobState.FAILED,
                JobState.CANCELLED,
            }:
                terminal.set()
                break
            terminal.wait(0.01)
        assert terminal.is_set()
    finally:
        dispatcher.close(force=True)

    assert calls == 3
    assert repository.get_job(job.job_id).state is JobState.FAILED
    assert repository.get_job_error(job.job_id).code == (
        "engine_worker_retry_exhausted"
    )

    restarted_calls = 0

    def observe_restart(_launch: EngineWorkerLaunchV1, _check_running) -> int:
        nonlocal restarted_calls
        restarted_calls += 1
        return 0

    restarted = EngineJobDispatcher(
        paths,
        worker_runner=observe_restart,
        reconcile_interval_seconds=0.01,
    )
    try:
        restarted.start()
        threading.Event().wait(0.05)
    finally:
        restarted.close(force=True)

    assert restarted_calls == 0

    retry = repository.create_retry_job_atomic(
        job.job_id,
        expected_original_state=JobState.FAILED,
        confirmed_unknown_operation_ids=(),
        client_request_id="worker-retry-exhausted-user-retry",
    )
    assert repository.get_job(job.job_id).state is JobState.FAILED
    assert retry.state is JobState.QUEUED
    assert repository.get_engine_worker_launch_count(retry.job_id) == 0


def test_unobserved_launches_exhaust_budget_across_engine_restart(
    tmp_path: Path,
) -> None:
    paths, repository, job, _reference = _registered_job(
        tmp_path,
        client_request_id="unobserved-worker-launches",
    )
    for index in range(3):
        claim = repository.claim_job(
            job.job_id,
            f"dead-engine-{index}",
            ttl_seconds=30,
        )
        assert repository.record_engine_worker_launch(
            job.job_id,
            claim.authority,
        ) == index + 1
        assert repository.release_job_claim(claim.authority) is True

    calls = 0

    def worker_must_not_start(
        _launch: EngineWorkerLaunchV1,
        _check_running,
    ) -> int:
        nonlocal calls
        calls += 1
        return 0

    restarted = EngineJobDispatcher(
        paths,
        worker_runner=worker_must_not_start,
        reconcile_interval_seconds=0.01,
    )
    try:
        restarted.start()
        for _index in range(500):
            if repository.get_job(job.job_id).state is JobState.FAILED:
                break
            threading.Event().wait(0.01)
    finally:
        restarted.close(force=True)

    assert calls == 0
    assert repository.get_job(job.job_id).state is JobState.FAILED
    error = repository.get_job_error(job.job_id)
    assert error is not None
    assert error.code == "engine_worker_retry_exhausted"
    assert error.details == {"automatic_launch_count": 3}


def test_exhausted_worker_budget_pauses_unknown_external_outcome(
    tmp_path: Path,
) -> None:
    paths, repository, job, _reference = _registered_job(
        tmp_path,
        client_request_id="worker-exhaustion-unknown-outcome",
    )
    first = repository.claim_job(job.job_id, "dead-engine", ttl_seconds=30)
    attempt = repository.create_attempt(
        job.job_id,
        "model-call",
        authority=first.authority,
    )
    attempt = repository.start_attempt(attempt.attempt_id, first.authority)
    operation = repository.prepare_external_operation(
        job_id=job.job_id,
        step_id=attempt.step_id,
        attempt_id=attempt.attempt_id,
        provider="fixture/provider-v1",
        request_hash="sha256:" + "a" * 64,
        operation_idempotency_key=None,
        summary_json="{}",
        authority=first.authority,
    )
    repository.start_external_operation(operation.operation_id, first.authority)
    for expected in range(1, 4):
        assert repository.record_engine_worker_launch(
            job.job_id,
            first.authority,
        ) == expected
    assert repository.release_job_claim(first.authority) is True

    calls = 0

    def worker_must_not_start(
        _launch: EngineWorkerLaunchV1,
        _check_running,
    ) -> int:
        nonlocal calls
        calls += 1
        return 0

    restarted = EngineJobDispatcher(
        paths,
        worker_runner=worker_must_not_start,
        reconcile_interval_seconds=0.01,
    )
    try:
        restarted.start()
        for _index in range(500):
            if repository.get_job(job.job_id).state is JobState.WAITING_FOR_INPUT:
                break
            threading.Event().wait(0.01)
    finally:
        restarted.close(force=True)

    assert calls == 0
    assert repository.get_job(job.job_id).state is JobState.WAITING_FOR_INPUT
    assert repository.get_job_error(job.job_id) is None
    assert repository.get_engine_worker_launch_count(job.job_id) == 0
    assert (
        repository.get_external_operation(operation.operation_id).outcome
        is ExternalOutcome.UNKNOWN
    )


def test_cancellation_wins_on_last_automatic_worker_launch(
    tmp_path: Path,
) -> None:
    paths, repository, job, _reference = _registered_job(
        tmp_path,
        client_request_id="worker-exhaustion-cancel",
    )
    prior = repository.claim_job(job.job_id, "prior-engine", ttl_seconds=30)
    assert repository.record_engine_worker_launch(job.job_id, prior.authority) == 1
    assert repository.record_engine_worker_launch(job.job_id, prior.authority) == 2
    assert repository.release_job_claim(prior.authority) is True
    calls = 0

    def cancel_on_last_launch(
        _launch: EngineWorkerLaunchV1,
        _check_running,
    ) -> int:
        nonlocal calls
        calls += 1
        repository.cancel_job(job.job_id)
        return 70

    dispatcher = EngineJobDispatcher(
        paths,
        worker_runner=cancel_on_last_launch,
        reconcile_interval_seconds=0.01,
    )
    try:
        dispatcher.start()
        for _index in range(500):
            if repository.get_job(job.job_id).state is JobState.CANCELLED:
                break
            threading.Event().wait(0.01)
    finally:
        dispatcher.close(force=True)

    assert calls == 1
    assert repository.get_job(job.job_id).state is JobState.CANCELLED
    assert repository.get_job_error(job.job_id) is None
    assert repository.get_engine_worker_launch_count(job.job_id) == 0


def test_repeated_worker_spawn_error_exhausts_same_durable_budget(
    tmp_path: Path,
) -> None:
    paths, repository, job, _reference = _registered_job(
        tmp_path,
        client_request_id="worker-spawn-error-budget",
    )
    calls = 0

    def fail_to_spawn(
        _launch: EngineWorkerLaunchV1,
        _check_running,
    ) -> int:
        nonlocal calls
        calls += 1
        raise OSError("injected spawn failure")

    dispatcher = EngineJobDispatcher(
        paths,
        worker_runner=fail_to_spawn,
        reconcile_interval_seconds=0.01,
    )
    try:
        dispatcher.start()
        for _index in range(1_000):
            if repository.get_job(job.job_id).state is JobState.FAILED:
                break
            threading.Event().wait(0.01)
    finally:
        dispatcher.close(force=True)

    assert calls == 3
    assert repository.get_job(job.job_id).state is JobState.FAILED
    assert repository.get_job_error(job.job_id).code == (
        "engine_worker_retry_exhausted"
    )


def test_unexpected_worker_exception_does_not_kill_dispatcher_loop(
    tmp_path: Path,
) -> None:
    paths, poison_repository, poison_job, poison = _registered_job(
        tmp_path,
        client_request_id="unexpected-worker-exception",
    )
    _paths, healthy_repository, healthy_job, healthy = _registered_job(
        tmp_path,
        client_request_id="healthy-after-unexpected-exception",
    )
    healthy_observed = threading.Event()
    calls: list[EngineJobReference] = []

    def run_worker(launch: EngineWorkerLaunchV1, _check_running) -> int:
        reference = launch.reference
        calls.append(reference)
        if reference == poison:
            if calls.count(poison) == 1:
                raise TypeError("injected unexpected worker failure")
            poison_repository.cancel_job(poison_job.job_id)
            return 0
        healthy_repository.cancel_job(healthy_job.job_id)
        healthy_observed.set()
        return 0

    dispatcher = EngineJobDispatcher(
        paths,
        worker_runner=run_worker,
        maximum_pending_jobs=1,
        reconcile_interval_seconds=0.01,
    )
    try:
        dispatcher.notify(poison)
        dispatcher.start()
        assert healthy_observed.wait(timeout=5)
    finally:
        dispatcher.close(force=True)

    assert calls[:2] == [poison, healthy]


def test_reconcile_failure_prevents_idle_draining(tmp_path: Path) -> None:
    local_parent = tmp_path / "corrupt local data"
    registry_path = local_parent / "AllToNote" / "workspace-instances.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text("{not-json", encoding="utf-8")
    dispatcher = EngineJobDispatcher(
        resolve_runtime_paths(local_data_parent=local_parent)
    )

    with pytest.raises(DomainError) as raised:
        dispatcher.reconcile()

    assert raised.value.code == "workspace_instance_registry_invalid"
    assert dispatcher.has_work is True
    assert dispatcher.try_begin_draining() is False
    dispatcher.close(force=True)


def test_empty_reconcile_is_read_only(tmp_path: Path) -> None:
    machine_root = tmp_path / "absent machine"
    dispatcher = EngineJobDispatcher(
        resolve_runtime_paths(machine_state_root=machine_root)
    )

    assert dispatcher.reconcile() == 0
    assert dispatcher.has_work is False
    assert not machine_root.exists()


def test_graceful_close_refuses_active_worker(tmp_path: Path) -> None:
    paths, repository, job, _reference = _registered_job(
        tmp_path,
        client_request_id="active-close",
    )
    started = threading.Event()
    release = threading.Event()

    def run_worker(_launch: EngineWorkerLaunchV1, _check_running) -> int:
        started.set()
        assert release.wait(timeout=5)
        repository.cancel_job(job.job_id)
        return 0

    dispatcher = EngineJobDispatcher(
        paths,
        worker_runner=run_worker,
        reconcile_interval_seconds=60,
    )
    dispatcher.start()
    try:
        assert started.wait(timeout=5)
        with pytest.raises(RuntimeError, match="engine_dispatcher_busy"):
            dispatcher.close()
    finally:
        release.set()
        dispatcher.close(force=True)


def test_worker_watchdog_settles_durable_cancellation_and_releases_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, repository, job, reference = _registered_job(
        tmp_path,
        client_request_id="watchdog-cancellation",
    )
    monkeypatch.setattr(
        "app.engine.job_dispatcher.WORKER_CANCELLATION_GRACE_SECONDS",
        0.0,
    )

    def run_worker(_launch: EngineWorkerLaunchV1, check_running) -> int:
        repository.cancel_job(job.job_id)
        check_running()
        pytest.fail("cancelled worker was not stopped")

    dispatcher = EngineJobDispatcher(
        paths,
        worker_runner=run_worker,
        reconcile_interval_seconds=60,
    )
    dispatcher.start()
    try:
        dispatcher.notify(reference)
        for _index in range(500):
            if not dispatcher.has_work:
                break
            threading.Event().wait(0.01)
        assert dispatcher.has_work is False
        assert repository.get_job(job.job_id).state is JobState.CANCELLED
        resource_store = MachineResourceLeaseStore.open(paths.data_dir / "machine")
        lease = resource_store.acquire(
            HEAVY_PRODUCTION_RESOURCE_NAME,
            ResourceOwner("other-workspace", "other-worker", process_id=303),
            ttl_seconds=300,
        )
        assert lease.release()
    finally:
        dispatcher.close(force=True)


def test_cancellation_settlement_releases_replacement_claim_when_authority_is_fenced(
    tmp_path: Path,
) -> None:
    _paths, repository, job, reference = _registered_job(
        tmp_path,
        client_request_id="fenced-cancellation-settlement",
    )
    expected = repository.claim_job(job.job_id, "engine-worker", ttl_seconds=30)
    attempt = repository.create_attempt(
        job.job_id,
        "produce",
        authority=expected.authority,
    )
    repository.start_attempt(attempt.attempt_id, expected.authority)
    repository.cancel_job(job.job_id)
    assert repository.release_job_claim(expected.authority)

    admission = job_dispatcher_module._WorkerAdmission(
        repository=repository,
        resource_store=SimpleNamespace(),
        source_lease=SimpleNamespace(),
        launch=SimpleNamespace(
            reference=reference,
            job_authority=expected.authority,
        ),
    )
    admission.settle_cancellation()

    replacement_authority = JobExecutionAuthority(
        expected.authority.owner_id,
        expected.authority.fencing_token + 1,
        job.job_id,
    )
    with pytest.raises(DomainError, match="job_claim_fenced"):
        repository.heartbeat_job_claim(
            replacement_authority,
            ttl_seconds=30,
        )


def test_graceful_shutdown_waits_for_active_worker_without_killing_it(
    tmp_path: Path,
) -> None:
    paths, repository, job, reference = _registered_job(
        tmp_path,
        client_request_id="graceful-active",
    )
    started = threading.Event()
    release = threading.Event()

    def run_worker(_launch: EngineWorkerLaunchV1, check_running) -> int:
        started.set()
        assert release.wait(timeout=5)
        check_running()
        repository.cancel_job(job.job_id)
        return 0

    dispatcher = EngineJobDispatcher(
        paths,
        worker_runner=run_worker,
        reconcile_interval_seconds=60,
    )
    dispatcher.start()
    try:
        assert started.wait(timeout=5)
        dispatcher.begin_graceful_shutdown()
        assert dispatcher.ready_to_close is False
        with pytest.raises(DomainError, match="engine_draining"):
            dispatcher.notify(reference)
        release.set()
        for _index in range(500):
            if dispatcher.ready_to_close:
                break
            threading.Event().wait(0.01)
        assert dispatcher.ready_to_close is True
        dispatcher.close()
    finally:
        if not dispatcher.ready_to_close:
            release.set()
            dispatcher.close(force=True)

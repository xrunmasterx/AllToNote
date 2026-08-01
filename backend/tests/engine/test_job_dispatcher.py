from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.adapters.jobs.workspace_instance_registry import WorkspaceInstanceRegistry
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.model import (
    JobExecutionBinding,
    JobExecutionOwner,
    JobState,
)
from app.engine.contracts import EngineJobReference
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
    observed: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "app.engine.job_dispatcher.run_worker_process",
        lambda command, **_kwargs: observed.append(command) or 0,
    )
    reference = EngineJobReference(
        "1" * 32,
        "job_018f0000-0000-7000-8000-000000000001",
    )

    assert EngineJobDispatcher(paths)._run_worker(reference, lambda: None) == 0
    command = observed[0]
    for flag, expected in (
        ("--config-dir", paths.config_dir),
        ("--data-dir", paths.data_dir),
        ("--cache-dir", paths.cache_dir),
        ("--state-dir", paths.state_dir),
        ("--log-dir", paths.log_dir),
    ):
        assert command[command.index(flag) + 1] == str(expected)


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

    def run_worker(candidate: EngineJobReference, _check_running) -> int:
        calls.append(candidate)
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

    def run_worker(candidate: EngineJobReference, _check_running) -> int:
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

    def run_worker(_candidate: EngineJobReference, _check_running) -> int:
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


def test_graceful_shutdown_waits_for_active_worker_without_killing_it(
    tmp_path: Path,
) -> None:
    paths, repository, job, reference = _registered_job(
        tmp_path,
        client_request_id="graceful-active",
    )
    started = threading.Event()
    release = threading.Event()

    def run_worker(_candidate: EngineJobReference, check_running) -> int:
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

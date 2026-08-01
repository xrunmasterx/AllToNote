from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.engine.job_worker as job_worker_module
from app.adapters.documents.document_basic_pack import PACK_VERSION
from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.adapters.jobs.machine_resource_lease import MachineResourceLeaseStore
from app.adapters.jobs.workspace_instance_registry import WorkspaceInstanceRegistry
from app.core.domain.ids import sha256_digest
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.model import (
    AttemptState,
    JobExecutionBinding,
    JobExecutionOwner,
    JobState,
)
from app.core.jobs.resource_lease import (
    HEAVY_PRODUCTION_RESOURCE_NAME,
    JobExecutionAuthority,
    ResourceLeaseHandoff,
    ResourceOwner,
)
from app.engine.contracts import (
    EngineJobReference,
    EngineWorkerLaunchV1,
)
from app.engine.job_worker import execute_engine_job, main
from app.job_runtime import LOCAL_CLI_PRINCIPAL
from app.runtime_paths import RuntimePaths, resolve_runtime_paths


WORKSPACE_ID = "worker-workspace-id"


def _inspect_workspace(root: Path) -> str:
    return (root / "workspace-id.txt").read_text(encoding="utf-8")


def _registered_job(
    tmp_path: Path,
    *,
    execution_owner: JobExecutionOwner = JobExecutionOwner.ENGINE,
    principal: str = LOCAL_CLI_PRINCIPAL,
    initial_events: tuple[tuple[str, str], ...] = (),
    execution_binding: JobExecutionBinding | None = None,
):
    local_parent = tmp_path / "local data"
    local_parent.mkdir()
    workspace = tmp_path / "Workspace 有空格"
    workspace.mkdir()
    (workspace / "workspace-id.txt").write_text(
        WORKSPACE_ID, encoding="utf-8"
    )
    paths = resolve_runtime_paths(local_data_parent=local_parent)
    registry = WorkspaceInstanceRegistry(
        local_parent,
        inspect_workspace=_inspect_workspace,
    )
    instance = registry.resolve(workspace)
    repository = SqliteJobRepository.open(instance.machine_root / "job-store")
    binding = execution_binding or JobExecutionBinding(
        recipe_id="alltonote.document-note",
        recipe_version=1,
        executor_id="alltonote.document",
        executor_version=1,
        pack_id="document-basic",
        pack_version=PACK_VERSION,
    )
    job = repository.create_job(
        request_hash="sha256:" + "a" * 64,
        principal=principal,
        client_request_id="worker-job",
        execution_owner=execution_owner,
        initial_events=initial_events,
        execution_binding=binding,
    )
    return paths, workspace, instance, repository, job


def _preclaimed_launch(paths, instance, repository, job):
    store = MachineResourceLeaseStore.open(paths.data_dir / "machine")
    supervisor = ResourceOwner(
        WORKSPACE_ID,
        "engine-supervisor",
        process_id=100,
    )
    worker = ResourceOwner(WORKSPACE_ID, "engine-worker")
    source = store.acquire(
        HEAVY_PRODUCTION_RESOURCE_NAME,
        supervisor,
        ttl_seconds=30,
    )
    handoff = store.handoff(source, worker, ttl_seconds=30)
    claim = repository.claim_job(job.job_id, worker.process_instance_id, ttl_seconds=30)
    return (
        EngineWorkerLaunchV1(
            1,
            EngineJobReference(instance.instance_id, job.job_id),
            handoff,
            claim.authority,
        ),
        store,
        source,
    )


def _private_launch_payload(
    workspace_instance_id: str,
    job_id: str,
) -> bytes:
    owner = ResourceOwner("workspace", "engine-worker")
    return EngineWorkerLaunchV1(
        1,
        EngineJobReference(workspace_instance_id, job_id),
        ResourceLeaseHandoff(
            1,
            HEAVY_PRODUCTION_RESOURCE_NAME,
            owner,
            1,
            30_000,
            "a" * 43,
        ),
        JobExecutionAuthority(owner.process_instance_id, 1, job_id),
    ).to_bytes()


def _private_worker_arguments(
    tmp_path: Path,
    workspace_instance_id: str,
    job_id: str,
) -> list[str]:
    return [
        "--config-dir",
        str(tmp_path / "config" / "AllToNote"),
        "--data-dir",
        str(tmp_path / "data" / "AllToNote"),
        "--cache-dir",
        str(tmp_path / "cache" / "AllToNote"),
        "--state-dir",
        str(tmp_path / "state" / "AllToNote"),
        "--log-dir",
        str(tmp_path / "log" / "AllToNote"),
        "--workspace-instance-id",
        workspace_instance_id,
        "--job-id",
        job_id,
    ]


def test_worker_adopts_launch_before_runtime_and_passes_exact_authorities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, workspace, instance, repository, job = _registered_job(tmp_path)
    launch, _store, source = _preclaimed_launch(
        paths, instance, repository, job
    )
    observed: list[tuple[object, object]] = []
    claim_ttls: list[int] = []
    original_claim_job = repository.claim_job

    def claim_job(job_id: str, owner_id: str, *, ttl_seconds: int):
        claim_ttls.append(ttl_seconds)
        return original_claim_job(job_id, owner_id, ttl_seconds=ttl_seconds)

    monkeypatch.setattr(repository, "claim_job", claim_job)
    monkeypatch.setattr(
        job_worker_module,
        "open_engine_job_store",
        lambda _paths, _instance: repository,
    )

    class Runtime:
        def wait_for_job(self, job_id: str):
            assert job_id == job.job_id
            return object()

    def runtime_factory(
        workspace_root: Path,
        *,
        adopted_resource_lease,
        expected_job_authority,
        **_kwargs,
    ):
        assert workspace_root == workspace.resolve()
        observed.append((adopted_resource_lease, expected_job_authority))
        return Runtime()

    execute_engine_job(
        paths,
        workspace_instance_id=instance.instance_id,
        job_id=job.job_id,
        inspect_workspace=_inspect_workspace,
        runtime_factory=runtime_factory,
        launch=launch,
    )

    assert observed[0][0].owner == launch.resource_handoff.owner
    assert (
        observed[0][0].expires_at_ms
        - launch.resource_handoff.expires_at_ms
        >= 240_000
    )
    assert observed[0][1] == launch.job_authority
    assert claim_ttls == [300]
    assert source.release() is False


def test_worker_launch_is_one_use_and_replay_never_reaches_runtime(
    tmp_path: Path,
) -> None:
    paths, _workspace, instance, repository, job = _registered_job(tmp_path)
    launch, _store, _source = _preclaimed_launch(paths, instance, repository, job)

    class Runtime:
        def wait_for_job(self, _job_id: str):
            return object()

    execute_engine_job(
        paths,
        workspace_instance_id=instance.instance_id,
        job_id=job.job_id,
        inspect_workspace=_inspect_workspace,
        runtime_factory=lambda *_args, **_kwargs: Runtime(),
        launch=launch,
    )
    with pytest.raises(DomainError, match="resource_handoff_invalid"):
        execute_engine_job(
            paths,
            workspace_instance_id=instance.instance_id,
            job_id=job.job_id,
            inspect_workspace=_inspect_workspace,
            runtime_factory=lambda *_args, **_kwargs: pytest.fail(
                "replayed launch must fail before runtime"
            ),
            launch=launch,
        )


def test_worker_permanent_failure_uses_dispatcher_preclaim(
    tmp_path: Path,
) -> None:
    paths, _workspace, instance, repository, job = _registered_job(tmp_path)
    launch, _store, _source = _preclaimed_launch(paths, instance, repository, job)

    with pytest.raises(DomainError, match="document_pack_unavailable"):
        execute_engine_job(
            paths,
            workspace_instance_id=instance.instance_id,
            job_id=job.job_id,
            inspect_workspace=_inspect_workspace,
            runtime_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                DomainError(
                    "document_pack_unavailable",
                    ErrorCategory.WORKSPACE_INCOMPATIBLE,
                    "Install the compatible document Pack",
                )
            ),
            launch=launch,
        )

    assert repository.get_job(job.job_id).state is JobState.FAILED
    assert repository.get_job_error(job.job_id).code == "document_pack_unavailable"


def test_worker_executes_existing_engine_job_using_registry_ids_only(
    tmp_path: Path,
) -> None:
    paths, workspace, instance, _repository, job = _registered_job(tmp_path)
    calls: list[object] = []
    expected = object()

    class Runtime:
        def wait_for_job(self, job_id: str):
            calls.append(("wait", job_id))
            return expected

    def runtime_factory(
        workspace_root: Path,
        *,
        runtime_paths: RuntimePaths,
        current_config_snapshot,
        execution_owner: JobExecutionOwner,
        require_existing_job_store: bool = False,
    ):
        assert require_existing_job_store is True
        assert execution_owner is JobExecutionOwner.ENGINE
        calls.append(
            (
                "runtime",
                workspace_root,
                runtime_paths,
                current_config_snapshot,
            )
        )
        return Runtime()

    result = execute_engine_job(
        paths,
        workspace_instance_id=instance.instance_id,
        job_id=job.job_id,
        inspect_workspace=_inspect_workspace,
        runtime_factory=runtime_factory,
    )

    assert result is expected
    assert calls == [
        ("runtime", workspace.resolve(), paths, None),
        ("wait", job.job_id),
    ]


@pytest.mark.parametrize(
    ("execution_owner", "principal"),
    (
        (JobExecutionOwner.FOREGROUND, LOCAL_CLI_PRINCIPAL),
        (JobExecutionOwner.ENGINE, "another-principal"),
    ),
)
def test_worker_rejects_jobs_outside_engine_local_user_authority(
    tmp_path: Path,
    execution_owner: JobExecutionOwner,
    principal: str,
) -> None:
    paths, _workspace, instance, _repository, job = _registered_job(
        tmp_path,
        execution_owner=execution_owner,
        principal=principal,
    )

    with pytest.raises(DomainError) as raised:
        execute_engine_job(
            paths,
            workspace_instance_id=instance.instance_id,
            job_id=job.job_id,
            inspect_workspace=_inspect_workspace,
            runtime_factory=lambda *_args, **_kwargs: pytest.fail(
                "unauthorized Job must not create a runtime"
            ),
        )

    assert raised.value.code == "engine_job_authority_denied"
    assert raised.value.category is ErrorCategory.POLICY_DENIED


def test_worker_persists_permanent_preflight_failure(tmp_path: Path) -> None:
    paths, _workspace, instance, repository, job = _registered_job(tmp_path)

    with pytest.raises(DomainError, match="document_pack_unavailable"):
        execute_engine_job(
            paths,
            workspace_instance_id=instance.instance_id,
            job_id=job.job_id,
            inspect_workspace=_inspect_workspace,
            runtime_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                DomainError(
                    "document_pack_unavailable",
                    ErrorCategory.WORKSPACE_INCOMPATIBLE,
                    "Install the compatible document Pack",
                )
            ),
        )

    failed = repository.get_job(job.job_id)
    stored = repository.get_job_error(job.job_id)
    assert failed.state is JobState.FAILED
    assert stored is not None
    assert stored.code == "document_pack_unavailable"
    assert repository.list_engine_execution_candidates(
        principal=LOCAL_CLI_PRINCIPAL,
        after_created_at=None,
        after_job_id=None,
        limit=10,
    ) == ()


def test_worker_persists_frozen_pack_snapshot_conflict(tmp_path: Path) -> None:
    paths, _workspace, instance, repository, job = _registered_job(tmp_path)

    with pytest.raises(DomainError, match="execution_pack_snapshot_missing"):
        execute_engine_job(
            paths,
            workspace_instance_id=instance.instance_id,
            job_id=job.job_id,
            inspect_workspace=_inspect_workspace,
            runtime_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                DomainError(
                    "execution_pack_snapshot_missing",
                    ErrorCategory.CONFLICT,
                    "The Job has no frozen Pack environment",
                )
            ),
        )

    assert repository.get_job(job.job_id).state is JobState.FAILED
    assert repository.get_job_error(job.job_id).code == (
        "execution_pack_snapshot_missing"
    )


def test_worker_does_not_persist_unknown_exception(tmp_path: Path) -> None:
    paths, _workspace, instance, repository, job = _registered_job(tmp_path)

    with pytest.raises(RuntimeError, match="unexpected bug"):
        execute_engine_job(
            paths,
            workspace_instance_id=instance.instance_id,
            job_id=job.job_id,
            inspect_workspace=_inspect_workspace,
            runtime_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("unexpected bug")
            ),
        )

    assert repository.get_job(job.job_id).state is JobState.QUEUED
    assert repository.get_job_error(job.job_id) is None


def test_worker_cancellation_wins_permanent_failure_settlement(
    tmp_path: Path,
) -> None:
    paths, _workspace, instance, repository, job = _registered_job(tmp_path)
    repository.cancel_job(job.job_id)

    with pytest.raises(DomainError, match="document_pack_unavailable"):
        execute_engine_job(
            paths,
            workspace_instance_id=instance.instance_id,
            job_id=job.job_id,
            inspect_workspace=_inspect_workspace,
            runtime_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                DomainError(
                    "document_pack_unavailable",
                    ErrorCategory.WORKSPACE_INCOMPATIBLE,
                    "Install the compatible document Pack",
                )
            ),
        )

    assert repository.get_job(job.job_id).state is JobState.CANCELLED
    assert repository.get_job_error(job.job_id) is None


@pytest.mark.parametrize(
    ("code", "category"),
    (
        ("job_store_busy", ErrorCategory.RETRYABLE_RUNTIME),
        ("scheduler_busy", ErrorCategory.CONFLICT),
        ("resource_busy", ErrorCategory.CONFLICT),
        ("job_claim_fenced", ErrorCategory.CONFLICT),
    ),
)
def test_worker_does_not_persist_recoverable_failure(
    tmp_path: Path,
    code: str,
    category: ErrorCategory,
) -> None:
    paths, _workspace, instance, repository, job = _registered_job(tmp_path)

    with pytest.raises(DomainError, match=code):
        execute_engine_job(
            paths,
            workspace_instance_id=instance.instance_id,
            job_id=job.job_id,
            inspect_workspace=_inspect_workspace,
            runtime_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                DomainError(code, category, "retry later")
            ),
        )

    assert repository.get_job(job.job_id).state is JobState.QUEUED
    assert repository.get_job_error(job.job_id) is None


def test_worker_failure_settlement_never_overrides_live_claim(
    tmp_path: Path,
) -> None:
    paths, _workspace, instance, repository, job = _registered_job(tmp_path)
    live = repository.claim_job(job.job_id, "other-worker", ttl_seconds=30)
    try:
        with pytest.raises(DomainError, match="document_pack_unavailable"):
            execute_engine_job(
                paths,
                workspace_instance_id=instance.instance_id,
                job_id=job.job_id,
                inspect_workspace=_inspect_workspace,
                runtime_factory=lambda *_args, **_kwargs: (
                    _ for _ in ()
                ).throw(
                    DomainError(
                        "document_pack_unavailable",
                        ErrorCategory.WORKSPACE_INCOMPATIBLE,
                        "Install the compatible document Pack",
                    )
                ),
            )

        assert repository.get_job(job.job_id).state is JobState.RUNNING
        assert repository.get_job_error(job.job_id) is None
    finally:
        repository.release_job_claim(live.authority)


def test_worker_failure_settlement_takes_over_expired_attempt(
    tmp_path: Path,
) -> None:
    paths, _workspace, instance, repository, job = _registered_job(tmp_path)
    previous = repository.claim_job(job.job_id, "lost-worker", ttl_seconds=30)
    attempt = repository.create_attempt(
        job.job_id,
        "preflight",
        authority=previous.authority,
    )
    repository.start_attempt(attempt.attempt_id, previous.authority)
    repository.release_job_claim(previous.authority)

    with pytest.raises(DomainError, match="job_request_invalid"):
        execute_engine_job(
            paths,
            workspace_instance_id=instance.instance_id,
            job_id=job.job_id,
            inspect_workspace=_inspect_workspace,
            runtime_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                DomainError(
                    "job_request_invalid",
                    ErrorCategory.INTERNAL,
                    "Stored Job request is invalid",
                )
            ),
        )

    attempts = repository.list_attempts(job.job_id)
    assert repository.get_job(job.job_id).state is JobState.FAILED
    assert tuple(item.state for item in attempts) == (
        AttemptState.INTERRUPTED,
        AttemptState.FAILED,
    )
    assert attempts[1].fencing_token > attempts[0].fencing_token


def test_worker_persists_registry_workspace_identity_mismatch(
    tmp_path: Path,
) -> None:
    paths, workspace, instance, repository, job = _registered_job(tmp_path)
    (workspace / "workspace-id.txt").write_text("changed-id", encoding="utf-8")

    with pytest.raises(DomainError) as raised:
        execute_engine_job(
            paths,
            workspace_instance_id=instance.instance_id,
            job_id=job.job_id,
            inspect_workspace=_inspect_workspace,
            runtime_factory=lambda *_args, **_kwargs: pytest.fail(
                "mismatched Workspace must not create a runtime"
            ),
        )

    assert raised.value.code == "workspace_instance_identity_mismatch"
    assert repository.get_job(job.job_id).state is JobState.FAILED
    stored = repository.get_job_error(job.job_id)
    assert stored is not None
    assert stored.code == "workspace_instance_identity_mismatch"


def test_worker_missing_jobstore_fails_without_creating_machine_state(
    tmp_path: Path,
) -> None:
    local_parent = tmp_path / "local data"
    local_parent.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "workspace-id.txt").write_text(
        WORKSPACE_ID, encoding="utf-8"
    )
    paths = resolve_runtime_paths(local_data_parent=local_parent)
    instance = WorkspaceInstanceRegistry(
        local_parent,
        inspect_workspace=_inspect_workspace,
    ).resolve(workspace)
    assert not instance.machine_root.exists()

    with pytest.raises(DomainError) as raised:
        execute_engine_job(
            paths,
            workspace_instance_id=instance.instance_id,
            job_id="job_018f0000-0000-7000-8000-000000000001",
            inspect_workspace=_inspect_workspace,
            runtime_factory=lambda *_args, **_kwargs: pytest.fail(
                "missing JobStore must not create a runtime"
            ),
        )

    assert raised.value.code == "engine_job_store_unavailable"
    assert not instance.machine_root.exists()


def test_worker_rejects_empty_jobstore_without_initializing_it(
    tmp_path: Path,
) -> None:
    local_parent = tmp_path / "local data"
    local_parent.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "workspace-id.txt").write_text(
        WORKSPACE_ID, encoding="utf-8"
    )
    paths = resolve_runtime_paths(local_data_parent=local_parent)
    instance = WorkspaceInstanceRegistry(
        local_parent,
        inspect_workspace=_inspect_workspace,
    ).resolve(workspace)
    job_store = instance.machine_root / "job-store"
    job_store.mkdir(parents=True)
    database = job_store / "jobs.sqlite"
    database.write_bytes(b"")

    with pytest.raises(DomainError) as raised:
        execute_engine_job(
            paths,
            workspace_instance_id=instance.instance_id,
            job_id="job_018f0000-0000-7000-8000-000000000001",
            inspect_workspace=_inspect_workspace,
            runtime_factory=lambda *_args, **_kwargs: pytest.fail(
                "empty JobStore must not create a runtime"
            ),
        )

    assert raised.value.code == "job_store_schema_invalid"
    assert database.read_bytes() == b""


def test_worker_restores_persisted_configuration_snapshot(
    tmp_path: Path,
) -> None:
    values: dict[str, object] = {}
    digest = sha256_digest("{}")
    snapshot_payload = json.dumps(
        {
            "digest": digest,
            "semantic_digest": digest,
            "snapshot_version": 1,
            "values": values,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    paths, _workspace, instance, _repository, job = _registered_job(
        tmp_path,
        initial_events=(("configuration.snapshot.v1", snapshot_payload),),
    )
    observed = []

    class Runtime:
        def wait_for_job(self, _job_id: str):
            return object()

    def runtime_factory(
        _workspace_root: Path,
        *,
        runtime_paths: RuntimePaths,
        current_config_snapshot,
        execution_owner: JobExecutionOwner,
        require_existing_job_store: bool = False,
    ):
        assert require_existing_job_store is True
        assert execution_owner is JobExecutionOwner.ENGINE
        observed.append((runtime_paths, current_config_snapshot))
        return Runtime()

    execute_engine_job(
        paths,
        workspace_instance_id=instance.instance_id,
        job_id=job.job_id,
        inspect_workspace=_inspect_workspace,
        runtime_factory=runtime_factory,
    )

    assert len(observed) == 1
    assert observed[0][0] == paths
    assert observed[0][1].values == values
    assert observed[0][1].semantic_digest == digest


def test_private_worker_main_maps_domain_failure_without_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "app.engine.job_worker.execute_engine_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            DomainError(
                "engine_job_store_busy",
                ErrorCategory.RETRYABLE_RUNTIME,
                "private detail",
            )
        ),
    )

    workspace_instance_id = "1" * 32
    job_id = "job_018f0000-0000-7000-8000-000000000001"
    exit_code = main(
        [
            "--config-dir",
            str(tmp_path / "config" / "AllToNote"),
            "--data-dir",
            str(tmp_path / "data" / "AllToNote"),
            "--cache-dir",
            str(tmp_path / "cache" / "AllToNote"),
            "--state-dir",
            str(tmp_path / "state" / "AllToNote"),
            "--log-dir",
            str(tmp_path / "log" / "AllToNote"),
            "--workspace-instance-id",
            workspace_instance_id,
            "--job-id",
            job_id,
        ],
        stdin_payload=_private_launch_payload(workspace_instance_id, job_id),
    )

    assert exit_code == 30
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("payload", (b"", b"{}", b"x" * 4097))
def test_private_worker_main_rejects_missing_malformed_or_oversized_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: bytes,
) -> None:
    monkeypatch.setattr(
        "app.engine.job_worker.execute_engine_job",
        lambda *_args, **_kwargs: pytest.fail("invalid launch executed"),
    )
    workspace_instance_id = "1" * 32
    job_id = "job_018f0000-0000-7000-8000-000000000001"

    assert main(
        _private_worker_arguments(tmp_path, workspace_instance_id, job_id),
        stdin_payload=payload,
    ) == 2


def test_private_worker_main_rejects_cli_and_launch_reference_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "app.engine.job_worker.execute_engine_job",
        lambda *_args, **_kwargs: pytest.fail("mismatched launch executed"),
    )
    workspace_instance_id = "1" * 32
    job_id = "job_018f0000-0000-7000-8000-000000000001"

    assert main(
        _private_worker_arguments(tmp_path, workspace_instance_id, job_id),
        stdin_payload=_private_launch_payload("2" * 32, job_id),
    ) == 2


def test_private_worker_main_preserves_all_runtime_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: list[RuntimePaths] = []
    expected = RuntimePaths(
        config_dir=tmp_path / "config root" / "AllToNote",
        data_dir=tmp_path / "data root" / "AllToNote",
        cache_dir=tmp_path / "cache root" / "AllToNote",
        state_dir=tmp_path / "state root" / "AllToNote",
        log_dir=tmp_path / "log root" / "AllToNote",
    )
    monkeypatch.setattr(
        "app.engine.job_worker.execute_engine_job",
        lambda paths, **_kwargs: observed.append(paths),
    )

    workspace_instance_id = "1" * 32
    job_id = "job_018f0000-0000-7000-8000-000000000001"
    exit_code = main(
        [
            "--config-dir",
            str(expected.config_dir),
            "--data-dir",
            str(expected.data_dir),
            "--cache-dir",
            str(expected.cache_dir),
            "--state-dir",
            str(expected.state_dir),
            "--log-dir",
            str(expected.log_dir),
            "--workspace-instance-id",
            workspace_instance_id,
            "--job-id",
            job_id,
        ],
        stdin_payload=_private_launch_payload(workspace_instance_id, job_id),
    )

    assert exit_code == 0
    assert observed == [expected]

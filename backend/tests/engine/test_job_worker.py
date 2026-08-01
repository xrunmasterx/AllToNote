from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.adapters.documents.document_basic_pack import PACK_VERSION
from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.adapters.jobs.workspace_instance_registry import WorkspaceInstanceRegistry
from app.core.domain.ids import sha256_digest
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.model import JobExecutionBinding, JobExecutionOwner
from app.engine.job_worker import execute_engine_job, main
from app.job_runtime import LOCAL_CLI_PRINCIPAL
from app.runtime_paths import resolve_runtime_paths


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
        local_app_data: Path,
        current_config_snapshot,
        require_existing_job_store: bool = False,
    ):
        assert require_existing_job_store is True
        calls.append(
            (
                "runtime",
                workspace_root,
                local_app_data,
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
        ("runtime", workspace.resolve(), paths.workspace_registry_parent, None),
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


def test_worker_revalidates_registry_workspace_identity_before_jobstore_open(
    tmp_path: Path,
) -> None:
    paths, workspace, instance, repository, job = _registered_job(tmp_path)
    database_bytes = repository.database_path.read_bytes()
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
    assert repository.database_path.read_bytes() == database_bytes


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
        local_app_data: Path,
        current_config_snapshot,
        require_existing_job_store: bool = False,
    ):
        assert require_existing_job_store is True
        observed.append((local_app_data, current_config_snapshot))
        return Runtime()

    execute_engine_job(
        paths,
        workspace_instance_id=instance.instance_id,
        job_id=job.job_id,
        inspect_workspace=_inspect_workspace,
        runtime_factory=runtime_factory,
    )

    assert len(observed) == 1
    assert observed[0][0] == paths.workspace_registry_parent
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

    exit_code = main(
        [
            "--local-data-parent",
            str(tmp_path),
            "--workspace-instance-id",
            "1" * 32,
            "--job-id",
            "job_018f0000-0000-7000-8000-000000000001",
        ]
    )

    assert exit_code == 30
    assert capsys.readouterr() == ("", "")

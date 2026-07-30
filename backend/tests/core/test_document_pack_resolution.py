from __future__ import annotations

import os
from pathlib import Path
import shutil

import pytest

from app.adapters.documents.document_basic_pack import PACK_ID, PACK_VERSION
from app.core.errors import DomainError
from app.core.jobs.model import JobExecutionBinding, JobSnapshot, JobState
from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.adapters.jobs.workspace_instance_registry import WorkspaceInstanceRegistry
from app.job_runtime import create_job_runtime_for_workspace
from app.runtime import _resolve_document_worker_config
from app.runtime_paths import resolve_runtime_paths
from iwiki.workspace import open_workspace


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "workspace-v2"


def _python_relative_path() -> Path:
    return (
        Path("venv/Scripts/python.exe")
        if os.name == "nt"
        else Path("venv/bin/python")
    )


def test_document_pack_resolves_frozen_standard_installation(tmp_path: Path) -> None:
    paths = resolve_runtime_paths(local_data_parent=tmp_path / "local")
    pack_root = paths.data_dir / "packs" / PACK_ID / PACK_VERSION
    python = pack_root / _python_relative_path()
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    artifacts = pack_root / "artifacts"
    artifacts.mkdir()

    config = _resolve_document_worker_config(paths, {})

    assert config.python_executable == python
    assert config.artifacts_path == artifacts
    assert config.backend_root.name == "backend"


def test_document_pack_explicit_paths_override_standard_installation(
    tmp_path: Path,
) -> None:
    paths = resolve_runtime_paths(local_data_parent=tmp_path / "local")
    python = tmp_path / "pack-python.exe"
    python.write_bytes(b"python")
    artifacts = tmp_path / "models"
    artifacts.mkdir()

    config = _resolve_document_worker_config(
        paths,
        {
            "ALLTONOTE_DOCUMENT_BASIC_PYTHON": str(python),
            "ALLTONOTE_DOCUMENT_BASIC_ARTIFACTS": str(artifacts),
        },
    )

    assert config.python_executable == python
    assert config.artifacts_path == artifacts


@pytest.mark.parametrize(
    "environment",
    (
        {},
        {"ALLTONOTE_DOCUMENT_BASIC_PYTHON": "missing-python"},
        {"ALLTONOTE_DOCUMENT_BASIC_ARTIFACTS": "missing-models"},
    ),
)
def test_document_pack_missing_or_partial_installation_fails_closed(
    tmp_path: Path,
    environment: dict[str, str],
) -> None:
    paths = resolve_runtime_paths(local_data_parent=tmp_path / "local")

    with pytest.raises(DomainError) as raised:
        _resolve_document_worker_config(paths, environment)

    assert raised.value.code == "document_pack_unavailable"


def test_job_wait_selects_document_runtime_from_persisted_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    shutil.copytree(FIXTURE_ROOT, workspace)
    local_data = tmp_path / "local-data"
    local_data.mkdir()
    registry = WorkspaceInstanceRegistry(
        local_data,
        inspect_workspace=lambda root: open_workspace(
            root, writable=False
        ).manifest.workspace_id,
    )
    instance = registry.resolve(workspace)
    repository = SqliteJobRepository.open(instance.machine_root / "job-store")
    job = repository.create_job(
        request_hash="sha256:" + "a" * 64,
        principal="local-user",
        client_request_id="document-reconnect",
        execution_binding=JobExecutionBinding(
            recipe_id="alltonote.document-note",
            recipe_version=1,
            executor_id="alltonote.document",
            executor_version=1,
            pack_id=PACK_ID,
            pack_version=PACK_VERSION,
        ),
    )
    selected: list[tuple[Path, Path | None]] = []

    class Runtime:
        def wait_job(self, job_id: str) -> JobSnapshot:
            return JobSnapshot(
                job_id,
                JobState.QUEUED,
                False,
                None,
                None,
                None,
                None,
                None,
            )

    def create(
        workspace_root: Path,
        *,
        local_app_data: Path | None = None,
    ) -> Runtime:
        selected.append((workspace_root, local_app_data))
        return Runtime()

    monkeypatch.setattr(
        "app.runtime.create_document_runtime_for_workspace",
        create,
    )
    runtime = create_job_runtime_for_workspace(
        workspace,
        local_app_data=local_data,
        current_config_snapshot=None,
    )

    assert runtime.wait_for_job(job.job_id).snapshot.state is JobState.QUEUED
    assert selected == [(workspace, local_data)]

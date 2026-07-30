from __future__ import annotations

import json
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
from app.runtime_paths import RuntimePaths, resolve_runtime_paths
from iwiki.workspace import open_workspace


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "workspace-v2"


def _python_relative_path() -> Path:
    return (
        Path("venv/Scripts/python.exe")
        if os.name == "nt"
        else Path("venv/bin/python")
    )


def _managed_python_relative_path() -> Path:
    return Path("python/python.exe") if os.name == "nt" else Path("python/bin/python")


def _create_managed_install(paths: RuntimePaths) -> tuple[Path, Path]:
    digest = "a" * 64
    pack_root = paths.data_dir / "packs" / PACK_ID / PACK_VERSION
    generation = pack_root / "installs" / digest
    python = generation / _managed_python_relative_path()
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    artifacts = generation / "artifacts"
    artifacts.mkdir()
    receipt = {
        "schema_version": 1,
        "pack_id": PACK_ID,
        "pack_version": PACK_VERSION,
        "manifest_sha256": f"sha256:{digest}",
        "verified": True,
    }
    (generation / "receipt.json").write_text(
        json.dumps(receipt, separators=(",", ":")),
        encoding="utf-8",
    )
    pointer = {
        "schema_version": 1,
        "pack_id": PACK_ID,
        "pack_version": PACK_VERSION,
        "manifest_sha256": f"sha256:{digest}",
    }
    (pack_root / "active.json").write_text(
        json.dumps(pointer, separators=(",", ":")),
        encoding="utf-8",
    )
    return python, artifacts


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


def test_document_pack_resolves_verified_managed_generation(tmp_path: Path) -> None:
    paths = resolve_runtime_paths(local_data_parent=tmp_path / "local")
    python, artifacts = _create_managed_install(paths)

    config = _resolve_document_worker_config(paths, {})

    assert config.python_executable == python
    assert config.artifacts_path == artifacts


@pytest.mark.parametrize(
    "mutation",
    ("pointer_identity", "receipt_digest", "receipt_unverified", "missing_python"),
)
def test_document_pack_managed_generation_fails_closed_when_invalid(
    tmp_path: Path,
    mutation: str,
) -> None:
    paths = resolve_runtime_paths(local_data_parent=tmp_path / "local")
    python, _artifacts = _create_managed_install(paths)
    pack_root = paths.data_dir / "packs" / PACK_ID / PACK_VERSION
    pointer_path = pack_root / "active.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    generation = pack_root / "installs" / ("a" * 64)
    receipt_path = generation / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

    if mutation == "pointer_identity":
        pointer["pack_id"] = "other-pack"
        pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
    elif mutation == "receipt_digest":
        receipt["manifest_sha256"] = "sha256:" + "b" * 64
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    elif mutation == "receipt_unverified":
        receipt["verified"] = False
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    else:
        python.unlink()

    with pytest.raises(DomainError) as raised:
        _resolve_document_worker_config(paths, {})

    assert raised.value.code == "document_pack_unavailable"


def test_document_pack_invalid_active_pointer_never_falls_back_to_legacy(
    tmp_path: Path,
) -> None:
    paths = resolve_runtime_paths(local_data_parent=tmp_path / "local")
    pack_root = paths.data_dir / "packs" / PACK_ID / PACK_VERSION
    legacy_python = pack_root / _python_relative_path()
    legacy_python.parent.mkdir(parents=True)
    legacy_python.write_bytes(b"python")
    (pack_root / "artifacts").mkdir()
    (pack_root / "active.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pack_id": PACK_ID,
                "pack_version": PACK_VERSION,
                "manifest_sha256": "sha256:../escape",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(DomainError) as raised:
        _resolve_document_worker_config(paths, {})

    assert raised.value.code == "document_pack_unavailable"


@pytest.mark.parametrize("schema_version", (True, False))
def test_document_pack_rejects_boolean_active_schema_version(
    tmp_path: Path,
    schema_version: bool,
) -> None:
    paths = resolve_runtime_paths(local_data_parent=tmp_path / "local")
    _create_managed_install(paths)
    pointer_path = (
        paths.data_dir / "packs" / PACK_ID / PACK_VERSION / "active.json"
    )
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer["schema_version"] = schema_version
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")

    with pytest.raises(DomainError) as raised:
        _resolve_document_worker_config(paths, {})

    assert raised.value.code == "document_pack_unavailable"


def test_document_pack_rejects_duplicate_active_pointer_keys(tmp_path: Path) -> None:
    paths = resolve_runtime_paths(local_data_parent=tmp_path / "local")
    _create_managed_install(paths)
    pointer_path = (
        paths.data_dir / "packs" / PACK_ID / PACK_VERSION / "active.json"
    )
    pointer_path.write_text(
        pointer_path.read_text(encoding="utf-8").replace(
            '"schema_version":1',
            '"schema_version":1,"schema_version":1',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(DomainError) as raised:
        _resolve_document_worker_config(paths, {})

    assert raised.value.code == "document_pack_unavailable"


def test_document_pack_rejects_linked_generation_artifacts(tmp_path: Path) -> None:
    paths = resolve_runtime_paths(local_data_parent=tmp_path / "local")
    _python, artifacts = _create_managed_install(paths)
    artifacts.rmdir()
    external_artifacts = tmp_path / "external-artifacts"
    external_artifacts.mkdir()
    try:
        artifacts.symlink_to(external_artifacts, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")

    with pytest.raises(DomainError) as raised:
        _resolve_document_worker_config(paths, {})

    assert raised.value.code == "document_pack_unavailable"


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

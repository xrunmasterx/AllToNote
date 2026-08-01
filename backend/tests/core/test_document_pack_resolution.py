from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from app.adapters.documents.document_basic_pack import PACK_ID, PACK_VERSION
from app.adapters.pack_layout import (
    legacy_generation_root,
    managed_generation_root,
)
from app.adapters.video_packs.official_video_pack import MEDIA_BASIC
from app.core.domain.ids import sha256_digest
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.model import JobExecutionBinding, JobSnapshot, JobState
from app.core.packs.events import (
    JOB_PACK_ENVIRONMENT_EVENT,
    ExecutionPackIdentity,
    JobPackEnvironmentSnapshot,
)
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
    pack_root.mkdir(parents=True)
    generation = managed_generation_root(paths.data_dir, PACK_ID) / digest
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
    generation = managed_generation_root(paths.data_dir, PACK_ID) / ("a" * 64)
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


def test_document_pack_keeps_read_only_legacy_generation_compatibility(
    tmp_path: Path,
) -> None:
    paths = resolve_runtime_paths(local_data_parent=tmp_path / "local")
    python, artifacts = _create_managed_install(paths)
    digest = "a" * 64
    legacy_root = legacy_generation_root(paths.data_dir, PACK_ID, PACK_VERSION)
    legacy_root.mkdir(parents=True)
    legacy_generation = legacy_root / digest
    python.parents[1].rename(legacy_generation)

    config = _resolve_document_worker_config(paths, {})

    assert config.python_executable == (
        legacy_generation / _managed_python_relative_path()
    )
    assert config.artifacts_path == legacy_generation / "artifacts"


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


@pytest.mark.parametrize(
    (
        "request_schema_version",
        "verifier_profile",
        "verifier_model",
        "require_existing_job_store",
    ),
    (
        (2, None, None, False),
        (3, "fixture/reviewer-profile", "fixture/reviewer-model", False),
        (2, None, None, True),
    ),
)
def test_job_wait_selects_document_runtime_from_persisted_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_schema_version: int,
    verifier_profile: str | None,
    verifier_model: str | None,
    require_existing_job_store: bool,
) -> None:
    workspace = tmp_path / "workspace"
    shutil.copytree(FIXTURE_ROOT, workspace)
    local_data = tmp_path / "local-data"
    local_data.mkdir()
    expected_paths = RuntimePaths(
        config_dir=tmp_path / "config" / "AllToNote",
        data_dir=local_data / "AllToNote",
        cache_dir=tmp_path / "cache" / "AllToNote",
        state_dir=tmp_path / "state" / "AllToNote",
        log_dir=tmp_path / "log" / "AllToNote",
    )
    registry = WorkspaceInstanceRegistry(
        local_data,
        inspect_workspace=lambda root: open_workspace(
            root, writable=False
        ).manifest.workspace_id,
    )
    instance = registry.resolve(workspace)
    repository = SqliteJobRepository.open(instance.machine_root / "job-store")
    request_values = {
        "expected_source_mtime_ns": 1,
        "expected_source_sha256": "sha256:" + "b" * 64,
        "expected_source_size": 1,
        "input_path": str((workspace / "paper.pdf").resolve()),
        "model_override": "fixture/frozen-model",
        "output_language": "en",
        "provider_profile": "fixture/frozen-profile",
        "recipe_id": "alltonote.document-note",
        "recipe_version": 1,
        "request_schema_version": request_schema_version,
        "workspace_root": str(workspace.resolve()),
    }
    if request_schema_version == 3:
        request_values.update(
            {
                "verifier_model_override": verifier_model,
                "verifier_provider_profile": verifier_profile,
            }
        )
    request_json = json.dumps(
        request_values,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    job = repository.create_job(
        request_hash=sha256_digest(request_json),
        request_json=request_json,
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
    selected: list[
        tuple[
            Path,
            Path | None,
            str | None,
            str | None,
            str | None,
            str | None,
        ]
    ] = []

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
        runtime_paths: RuntimePaths,
        current_config_snapshot=None,
        requested_model_identity: str | None = None,
        requested_provider_profile: str | None = None,
        requested_verifier_model_identity: str | None = None,
        requested_verifier_provider_profile: str | None = None,
        require_existing_job_store: bool = False,
    ) -> Runtime:
        assert current_config_snapshot is None
        assert require_existing_job_store is expected_existing_job_store
        selected.append(
            (
                workspace_root,
                runtime_paths,
                requested_model_identity,
                requested_provider_profile,
                requested_verifier_model_identity,
                requested_verifier_provider_profile,
            )
        )
        return Runtime()

    monkeypatch.setattr(
        "app.runtime.create_document_runtime_for_workspace",
        create,
    )
    expected_existing_job_store = require_existing_job_store
    runtime = create_job_runtime_for_workspace(
        workspace,
        runtime_paths=expected_paths,
        current_config_snapshot=None,
        require_existing_job_store=require_existing_job_store,
    )

    assert runtime.wait_for_job(job.job_id).snapshot.state is JobState.QUEUED
    assert selected == [
        (
            workspace,
            expected_paths,
            "fixture/frozen-model",
            "fixture/frozen-profile",
            verifier_model,
            verifier_profile,
        )
    ]


def test_job_wait_rejects_wrong_binding_before_recipe_runtime_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    shutil.copytree(FIXTURE_ROOT, workspace)
    local_data = tmp_path / "local-data"
    local_data.mkdir()
    instance = WorkspaceInstanceRegistry(
        local_data,
        inspect_workspace=lambda root: open_workspace(
            root, writable=False
        ).manifest.workspace_id,
    ).resolve(workspace)
    repository = SqliteJobRepository.open(instance.machine_root / "job-store")
    job = repository.create_job(
        request_hash="sha256:" + "a" * 64,
        principal="local-user",
        client_request_id="wrong-binding",
        execution_binding=JobExecutionBinding(
            recipe_id="alltonote.document-note",
            recipe_version=1,
            executor_id="wrong.executor",
            executor_version=99,
            pack_id="wrong-pack",
            pack_version="wrong-version",
        ),
    )
    monkeypatch.setattr(
        "app.runtime.create_document_runtime_for_workspace",
        lambda *_args, **_kwargs: pytest.fail(
            "wrong binding must fail before Recipe runtime creation"
        ),
    )
    runtime = create_job_runtime_for_workspace(
        workspace,
        local_app_data=local_data,
        current_config_snapshot=None,
    )

    with pytest.raises(DomainError) as raised:
        runtime.wait_for_job(job.job_id)

    assert raised.value.code == "job_executor_unavailable"
    assert raised.value.category is ErrorCategory.WORKSPACE_INCOMPATIBLE


def test_public_job_runtime_opens_empty_store_for_fresh_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    shutil.copytree(FIXTURE_ROOT, workspace)
    local_data = tmp_path / "local-data"
    local_data.mkdir()

    runtime = create_job_runtime_for_workspace(
        workspace,
        local_app_data=local_data,
        current_config_snapshot=None,
    )

    page = runtime.list_jobs()
    assert page.jobs == ()
    assert page.next_cursor is None


def test_public_job_wait_routes_frozen_video_binding_to_exact_generation(
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
    frozen = JobPackEnvironmentSnapshot(
        schema_version=1,
        packs=(
            ExecutionPackIdentity(
                pack_id=MEDIA_BASIC.pack_id,
                pack_version=MEDIA_BASIC.pack_version,
                platform="windows-x86_64",
                manifest_sha256="sha256:" + "a" * 64,
            ),
        ),
    )
    frozen_payload = json.dumps(
        {
            "schema_version": frozen.schema_version,
            "packs": [
                {
                    "pack_id": pack.pack_id,
                    "pack_version": pack.pack_version,
                    "platform": pack.platform,
                    "manifest_sha256": pack.manifest_sha256,
                }
                for pack in frozen.packs
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    request_json = "{}"
    job = repository.create_job(
        request_hash=sha256_digest(request_json),
        request_json=request_json,
        principal="local-user",
        client_request_id="video-public-reconnect",
        initial_events=((JOB_PACK_ENVIRONMENT_EVENT, frozen_payload),),
        execution_binding=JobExecutionBinding(
            recipe_id="alltonote.video-producer",
            recipe_version=2,
            executor_id="alltonote.video",
            executor_version=1,
            pack_id=MEDIA_BASIC.pack_id,
            pack_version=MEDIA_BASIC.pack_version,
        ),
    )
    runtime_module = importlib.import_module("app.runtime")
    monkeypatch.setattr(
        runtime_module.CodexAppServerStatusService,
        "get_status",
        staticmethod(
            lambda: SimpleNamespace(ready=True, default_model="codex-test-model")
        ),
    )

    class Bridge:
        @staticmethod
        def complete_once(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("model execution is outside this routing test")

        @staticmethod
        def complete_request(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("model execution is outside this routing test")

    monkeypatch.setattr(
        runtime_module,
        "CodexAppServerCompletionBridge",
        lambda **_kwargs: Bridge(),
    )
    generation = tmp_path / "media-generation"
    entrypoints: dict[str, Path] = {}
    for name, relative in MEDIA_BASIC.entrypoints("windows-x86_64").items():
        path = generation.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
        entrypoints[name] = path.resolve()
    resolved = runtime_module.ResolvedOfficialVideoPack(
        pack_id=MEDIA_BASIC.pack_id,
        pack_version=MEDIA_BASIC.pack_version,
        platform="windows-x86_64",
        manifest_sha256=frozen.packs[0].manifest_sha256,
        generation=generation.resolve(),
        entrypoints=entrypoints,
    )
    exact_resolutions: list[tuple[str, str]] = []

    class Resolver:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        @staticmethod
        def resolve_active(_contract: object) -> object:
            raise AssertionError("public recovery must not read active Pack state")

        @staticmethod
        def resolve_exact(contract, manifest_sha256: str):
            exact_resolutions.append((contract.pack_id, manifest_sha256))
            return resolved

    monkeypatch.setattr(runtime_module, "OfficialVideoPackResolver", Resolver)
    routed: list[str] = []

    def wait_job(service, job_id: str) -> JobSnapshot:
        routed.append(job_id)
        return service.get_job(job_id)

    monkeypatch.setattr(runtime_module.VideoService, "wait_job", wait_job)
    runtime = create_job_runtime_for_workspace(
        workspace,
        local_app_data=local_data,
        current_config_snapshot=None,
    )

    assert runtime.wait_for_job(job.job_id).snapshot.state is JobState.QUEUED
    assert routed == [job.job_id]
    assert exact_resolutions == [
        (MEDIA_BASIC.pack_id, frozen.packs[0].manifest_sha256)
    ]

    missing_request = '{"case":"missing-pack-event"}'
    missing = repository.create_job(
        request_hash=sha256_digest(missing_request),
        request_json=missing_request,
        principal="local-user",
        client_request_id="video-pack-missing-event",
        execution_binding=JobExecutionBinding(
            recipe_id="alltonote.video-producer",
            recipe_version=2,
            executor_id="alltonote.video",
            executor_version=1,
            pack_id=MEDIA_BASIC.pack_id,
            pack_version=MEDIA_BASIC.pack_version,
        ),
    )
    with pytest.raises(DomainError, match="execution_pack_snapshot_missing"):
        runtime.wait_for_job(missing.job_id)

    mismatch_request = '{"case":"pack-binding-mismatch"}'
    mismatch = repository.create_job(
        request_hash=sha256_digest(mismatch_request),
        request_json=mismatch_request,
        principal="local-user",
        client_request_id="video-pack-binding-mismatch",
        initial_events=((JOB_PACK_ENVIRONMENT_EVENT, frozen_payload),),
        execution_binding=JobExecutionBinding(
            recipe_id="alltonote.video-producer",
            recipe_version=2,
            executor_id="alltonote.video",
            executor_version=1,
            pack_id=MEDIA_BASIC.pack_id,
            pack_version="unsupported-version",
        ),
    )
    with pytest.raises(DomainError, match="execution_pack_snapshot_invalid"):
        runtime.wait_for_job(mismatch.job_id)

    assert routed == [job.job_id]
    assert exact_resolutions == [
        (MEDIA_BASIC.pack_id, frozen.packs[0].manifest_sha256)
    ]

from __future__ import annotations

import ast
import inspect
import json
import pickle
import shutil
import sys
from importlib import metadata, resources
from pathlib import Path

import pytest
from iwiki.errors import ErrorCode, IWikiError
from iwiki.portable import PreparedBundle

from app.adapters.iwiki import portable_gateway as gateway_module
from app.adapters.iwiki.portable_gateway import IWikiPortableGateway
from app.core.errors import DomainError, ErrorCategory


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "workspace-v2"
RUNTIME_LOCK_PATH = Path(__file__).parents[2] / "app" / "runtime-lock.json"
STAGING_RELATIVE_PATH = (
    "raw/personal/.staging/fixture/job.nonce/bundle.partial"
)
BUNDLE_ID = "bnd_018f0000-0000-7000-8000-000000000001"
MANIFEST_SHA256 = (
    "sha256:3dc91bcb9af372ee881bb2770577801914edf39553f1733a020356213c7cd998"
)


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    shutil.copytree(FIXTURE_ROOT, root)
    for relative in (
        "raw/common",
        "wiki/common",
        "wiki/personal",
        ".cache",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def gateway() -> IWikiPortableGateway:
    return IWikiPortableGateway()


def _runtime_lock() -> dict[str, object]:
    return json.loads(RUNTIME_LOCK_PATH.read_text(encoding="utf-8"))


def _patch_packaged_runtime_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    **changes: object,
) -> None:
    lock = _runtime_lock()
    lock.update(changes)
    package_root = tmp_path / "app-package"
    package_root.mkdir()
    (package_root / "runtime-lock.json").write_text(
        json.dumps(lock),
        encoding="utf-8",
    )
    original_files = resources.files

    def patched_files(package: str) -> object:
        if package == "app":
            return package_root
        return original_files(package)

    monkeypatch.setattr(resources, "files", patched_files)


def _imported_module_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module == "iwiki":
                names.update(
                    f"iwiki.{alias.name}" for alias in node.names
                )
            else:
                names.add(node.module)
    return names


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        ("import iwiki", {"iwiki"}),
        ("from iwiki import portable", {"iwiki.portable"}),
        ("from iwiki import _private", {"iwiki._private"}),
    ),
)
def test_import_scanner_resolves_root_iwiki_imports(
    source: str,
    expected: set[str],
) -> None:
    assert _imported_module_names(ast.parse(source)) == expected


def test_gateway_inspects_the_pinned_contract(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
) -> None:
    info = gateway.inspect(workspace_root)

    assert info.iwiki_sdk_api_version == 1
    assert info.contract_id == "iwiki-portable-contract-v1"
    assert info.schema_set_id == "2026-07-portable-v1"
    assert info.schema_set_sha256 == _runtime_lock()["schema_sha256"]


def test_gateway_opens_workspace_as_writable(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
) -> None:
    manifest_path = workspace_root / ".llm-wiki" / "manifest.yaml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "schema_version: 2", "schema_version: 999"
        ),
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(DomainError) as caught:
        gateway.inspect(workspace_root)

    assert caught.value.code == "portable_schema_too_new"
    assert caught.value.category is ErrorCategory.WORKSPACE_INCOMPATIBLE


def test_gateway_rejects_wrong_schema_fingerprint(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_packaged_runtime_lock(
        monkeypatch,
        tmp_path,
        schema_sha256="sha256:" + "0" * 64,
    )

    with pytest.raises(DomainError) as caught:
        gateway.inspect(workspace_root)

    assert caught.value.code == "portable_contract_incompatible"
    assert caught.value.category is ErrorCategory.WORKSPACE_INCOMPATIBLE


def test_gateway_rejects_installed_iwiki_version_not_matching_runtime_lock(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed_version = metadata.version

    def mismatched_version(distribution_name: str) -> str:
        if distribution_name == "llm-iwiki":
            return "999.0.0"
        return installed_version(distribution_name)

    monkeypatch.setattr(metadata, "version", mismatched_version)

    with pytest.raises(DomainError) as caught:
        gateway.inspect(workspace_root)

    assert caught.value.code == "portable_contract_incompatible"
    assert caught.value.category is ErrorCategory.WORKSPACE_INCOMPATIBLE


def test_gateway_imports_only_public_iwiki_sdk() -> None:
    tree = ast.parse(
        inspect.getsource(sys.modules[IWikiPortableGateway.__module__])
    )
    imports = _imported_module_names(tree)
    iwiki_imports = {
        name
        for name in imports
        if name == "iwiki" or name.startswith("iwiki.")
    }

    assert iwiki_imports <= {
        "iwiki.errors",
        "iwiki.portable",
        "iwiki.workspace",
    }


def test_gateway_validates_candidate_with_the_real_pinned_sdk(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
) -> None:
    report = gateway.validate_candidate(workspace_root, STAGING_RELATIVE_PATH)

    assert report.valid
    assert report.bundle_id == BUNDLE_ID
    assert report.manifest_sha256 == MANIFEST_SHA256
    assert report.issues == ()


def test_validate_candidate_fails_closed_when_runtime_lock_drifts(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_packaged_runtime_lock(
        monkeypatch,
        tmp_path,
        schema_sha256="sha256:" + "0" * 64,
    )

    with pytest.raises(DomainError) as caught:
        gateway.validate_candidate(workspace_root, STAGING_RELATIVE_PATH)

    assert caught.value.code == "portable_contract_incompatible"
    assert caught.value.category is ErrorCategory.WORKSPACE_INCOMPATIBLE


def test_prepare_candidate_fails_closed_when_runtime_lock_drifts(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_packaged_runtime_lock(
        monkeypatch,
        tmp_path,
        schema_sha256="sha256:" + "0" * 64,
    )

    with pytest.raises(DomainError) as caught:
        gateway.prepare_candidate(
            workspace_root,
            STAGING_RELATIVE_PATH,
            expected_bundle_id=BUNDLE_ID,
            expected_manifest_sha256=MANIFEST_SHA256,
        )

    assert caught.value.code == "portable_contract_incompatible"
    assert caught.value.category is ErrorCategory.WORKSPACE_INCOMPATIBLE


def test_gateway_preserves_prepared_bundle_as_opaque_sdk_handle(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
) -> None:
    prepared = gateway.prepare_candidate(
        workspace_root,
        STAGING_RELATIVE_PATH,
        expected_bundle_id=BUNDLE_ID,
        expected_manifest_sha256=MANIFEST_SHA256,
    )

    try:
        assert type(prepared) is PreparedBundle
        assert prepared.bundle_id == BUNDLE_ID
        assert prepared.manifest_sha256 == MANIFEST_SHA256
        with pytest.raises(TypeError, match="cannot be serialized"):
            pickle.dumps(prepared)
    finally:
        prepared.close()


def test_gateway_commits_prepared_bundle_with_the_real_pinned_sdk(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
) -> None:
    prepared = gateway.prepare_candidate(
        workspace_root,
        STAGING_RELATIVE_PATH,
        expected_bundle_id=BUNDLE_ID,
        expected_manifest_sha256=MANIFEST_SHA256,
    )

    result = gateway.commit_prepared(prepared)

    final = workspace_root / "raw" / "personal" / "bundles" / BUNDLE_ID
    assert result.bundle_id == BUNDLE_ID
    assert result.manifest_sha256 == MANIFEST_SHA256
    assert result.relative_path == f"raw/personal/bundles/{BUNDLE_ID}"
    assert not result.idempotent
    assert final.is_dir()
    assert (final / "commit.json").is_file()
    assert not (workspace_root / STAGING_RELATIVE_PATH).exists()


def test_commit_prepared_fails_closed_when_runtime_lock_drifts(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared = gateway.prepare_candidate(
        workspace_root,
        STAGING_RELATIVE_PATH,
        expected_bundle_id=BUNDLE_ID,
        expected_manifest_sha256=MANIFEST_SHA256,
    )
    _patch_packaged_runtime_lock(
        monkeypatch,
        tmp_path,
        schema_sha256="sha256:" + "0" * 64,
    )

    try:
        with pytest.raises(DomainError) as caught:
            gateway.commit_prepared(prepared)

        assert caught.value.code == "portable_contract_incompatible"
        assert caught.value.category is ErrorCategory.WORKSPACE_INCOMPATIBLE
    finally:
        prepared.close()


def test_commit_prepared_rejects_handle_from_another_gateway(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
) -> None:
    prepared = gateway.prepare_candidate(
        workspace_root,
        STAGING_RELATIVE_PATH,
        expected_bundle_id=BUNDLE_ID,
        expected_manifest_sha256=MANIFEST_SHA256,
    )

    try:
        with pytest.raises(DomainError) as caught:
            IWikiPortableGateway().commit_prepared(prepared)

        assert caught.value.code == "portable_prepared_bundle_invalid"
        assert caught.value.category is ErrorCategory.INVALID_REQUEST
    finally:
        prepared.close()


def test_commit_prepared_rejects_consumed_handle(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
) -> None:
    prepared = gateway.prepare_candidate(
        workspace_root,
        STAGING_RELATIVE_PATH,
        expected_bundle_id=BUNDLE_ID,
        expected_manifest_sha256=MANIFEST_SHA256,
    )
    gateway.commit_prepared(prepared)

    with pytest.raises(DomainError) as caught:
        gateway.commit_prepared(prepared)

    assert caught.value.code == "portable_prepared_bundle_invalid"
    assert caught.value.category is ErrorCategory.INVALID_REQUEST


def test_commit_prepared_rejects_closed_handle(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
) -> None:
    prepared = gateway.prepare_candidate(
        workspace_root,
        STAGING_RELATIVE_PATH,
        expected_bundle_id=BUNDLE_ID,
        expected_manifest_sha256=MANIFEST_SHA256,
    )
    prepared.close()

    with pytest.raises(DomainError) as caught:
        gateway.commit_prepared(prepared)

    assert caught.value.code == "portable_prepared_bundle_invalid"
    assert caught.value.category is ErrorCategory.INVALID_REQUEST


@pytest.mark.parametrize(
    ("upstream_code", "domain_code", "category"),
    (
        (
            ErrorCode.INVALID_ARGUMENT,
            "portable_request_invalid",
            ErrorCategory.INVALID_REQUEST,
        ),
        (
            ErrorCode.INVALID_WORKSPACE,
            "workspace_invalid",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
        ),
        (
            ErrorCode.SCHEMA_TOO_NEW,
            "portable_schema_too_new",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
        ),
        (
            ErrorCode.VALIDATION_FAILED,
            "portable_bundle_validation_failed",
            ErrorCategory.RECIPE_FAILED,
        ),
        (ErrorCode.CONFLICT, "bundle_id_conflict", ErrorCategory.CONFLICT),
        (
            ErrorCode.PERMISSION_DENIED,
            "workspace_write_denied",
            ErrorCategory.POLICY_DENIED,
        ),
        (
            ErrorCode.RETRYABLE_RUNTIME,
            "iwiki_runtime_retryable",
            ErrorCategory.RETRYABLE_RUNTIME,
        ),
        (ErrorCode.INTERNAL, "iwiki_internal_error", ErrorCategory.INTERNAL),
    ),
)
def test_gateway_maps_iwiki_errors_without_leaking_absolute_paths(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    upstream_code: ErrorCode,
    domain_code: str,
    category: ErrorCategory,
) -> None:
    secret_path = str((workspace_root / "private" / "secret.txt").resolve())

    def fail(_workspace: object) -> object:
        raise IWikiError(
            upstream_code,
            f"failed at {secret_path}",
            {"path": secret_path},
        )

    monkeypatch.setattr(gateway_module, "inspect_portable_contract", fail)

    with pytest.raises(DomainError) as caught:
        gateway.inspect(workspace_root)

    assert caught.value.code == domain_code
    assert caught.value.category is category
    assert caught.value.details == {
        "upstream": "iwiki",
        "upstream_code": upstream_code.wire_value,
    }
    assert secret_path not in str(caught.value)
    assert secret_path not in repr(caught.value.details)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__

from __future__ import annotations

import ast
import inspect
import json
import pickle
import shutil
import sys
import threading
from importlib import metadata, resources
from pathlib import Path

import pytest
from iwiki.errors import ErrorCode, IWikiError
from iwiki.portable import CommitResult, PreparedBundle

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


class _InstrumentedPreparedBundle:
    def __init__(
        self,
        *,
        close_error: Exception | None = None,
        close_entered: threading.Event | None = None,
        release_close: threading.Event | None = None,
    ) -> None:
        self.enter_calls = 0
        self.close_calls = 0
        self.close_error = close_error
        self.close_entered = close_entered
        self.release_close = release_close

    def __enter__(self) -> _InstrumentedPreparedBundle:
        self.enter_calls += 1
        return self

    def close(self) -> None:
        self.close_calls += 1
        if self.close_entered is not None:
            self.close_entered.set()
        if self.release_close is not None:
            assert self.release_close.wait(timeout=3)
        if self.close_error is not None:
            raise self.close_error


def _prepare_instrumented_bundle(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    prepared: _InstrumentedPreparedBundle,
) -> _InstrumentedPreparedBundle:
    monkeypatch.setattr(
        gateway_module,
        "PreparedBundle",
        _InstrumentedPreparedBundle,
    )
    monkeypatch.setattr(
        gateway_module,
        "prepare_bundle_commit",
        lambda *_args, **_kwargs: prepared,
    )
    return gateway.prepare_candidate(
        workspace_root,
        STAGING_RELATIVE_PATH,
        expected_bundle_id=BUNDLE_ID,
        expected_manifest_sha256=MANIFEST_SHA256,
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
            if node.module == "iwiki" or node.module.startswith("iwiki."):
                names.update(
                    f"{node.module}.{alias.name}" for alias in node.names
                )
            else:
                names.add(node.module)
        elif isinstance(node, ast.Call):
            function = node.func
            is_dynamic_import = (
                isinstance(function, ast.Name)
                and function.id in {"__import__", "import_module"}
            ) or (
                isinstance(function, ast.Attribute)
                and function.attr == "import_module"
            )
            if is_dynamic_import:
                names.add("<dynamic-import>")
                if (
                    node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and (
                        node.args[0].value == "iwiki"
                        or node.args[0].value.startswith("iwiki.")
                    )
                ):
                    names.add(node.args[0].value)
    return names


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        ("import iwiki", {"iwiki"}),
        ("from iwiki import portable", {"iwiki.portable"}),
        ("from iwiki import _private", {"iwiki._private"}),
        (
            "from iwiki.portable import _private",
            {"iwiki.portable._private"},
        ),
    ),
)
def test_import_scanner_resolves_root_iwiki_imports(
    source: str,
    expected: set[str],
) -> None:
    assert _imported_module_names(ast.parse(source)) == expected


@pytest.mark.parametrize(
    "source",
    (
        "__import__('iwiki._private')",
        "import importlib\nimportlib.import_module('iwiki._private')",
        "from importlib import import_module\n"
        "import_module('iwiki._private')",
    ),
)
def test_import_scanner_detects_dynamic_imports(source: str) -> None:
    imports = _imported_module_names(ast.parse(source))

    assert "<dynamic-import>" in imports
    assert "iwiki._private" in imports


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


def test_gateway_rejects_another_installed_distribution_in_runtime_lock(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_packaged_runtime_lock(
        monkeypatch,
        tmp_path,
        iwiki_package=f"pytest=={metadata.version('pytest')}",
    )

    with pytest.raises(DomainError) as caught:
        gateway.inspect(workspace_root)

    assert caught.value.code == "portable_contract_incompatible"
    assert caught.value.category is ErrorCategory.WORKSPACE_INCOMPATIBLE


@pytest.mark.parametrize(
    "upstream_error",
    (
        RuntimeError("secret C:/private/runtime-lock.json"),
        OSError("secret C:/private/runtime-lock.json"),
        KeyError("secret C:/private/runtime-lock.json"),
    ),
)
def test_gateway_sanitizes_public_inspect_compatibility_failures(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    upstream_error: Exception,
) -> None:
    def fail(_workspace: object) -> object:
        raise upstream_error

    monkeypatch.setattr(gateway_module, "inspect_portable_contract", fail)

    with pytest.raises(DomainError) as caught:
        gateway.inspect(workspace_root)

    assert caught.value.code == "portable_contract_incompatible"
    assert caught.value.category is ErrorCategory.WORKSPACE_INCOMPATIBLE
    assert "C:/private" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


def test_gateway_sanitizes_packaged_resource_runtime_failure(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_package: str) -> object:
        raise RuntimeError("secret C:/private/runtime-lock.json")

    monkeypatch.setattr(resources, "files", fail)

    with pytest.raises(DomainError) as caught:
        gateway.inspect(workspace_root)

    assert caught.value.code == "portable_contract_incompatible"
    assert "C:/private" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


def test_gateway_sanitizes_distribution_metadata_failure(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_distribution_name: str) -> str:
        raise OSError("secret C:/private/distribution-metadata")

    monkeypatch.setattr(metadata, "version", fail)

    with pytest.raises(DomainError) as caught:
        gateway.inspect(workspace_root)

    assert caught.value.code == "portable_contract_incompatible"
    assert "C:/private" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("fatal_error", (KeyboardInterrupt(), SystemExit(), MemoryError()))
def test_gateway_does_not_swallow_fatal_base_exceptions(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    fatal_error: BaseException,
) -> None:
    def fail(_workspace: object) -> object:
        raise fatal_error

    monkeypatch.setattr(gateway_module, "inspect_portable_contract", fail)

    with pytest.raises(type(fatal_error)):
        gateway.inspect(workspace_root)


def test_gateway_imports_only_public_iwiki_sdk() -> None:
    tree = ast.parse(
        inspect.getsource(sys.modules[IWikiPortableGateway.__module__])
    )
    imports = _imported_module_names(tree)
    iwiki_imports = {
        name
        for name in imports
        if name == "<dynamic-import>"
        or name == "iwiki"
        or name.startswith("iwiki.")
    }

    assert iwiki_imports <= {
        "iwiki.errors.ErrorCode",
        "iwiki.errors.IWikiError",
        "iwiki.portable.CommitResult",
        "iwiki.portable.PortableBundleRef",
        "iwiki.portable.PortableContractInfo",
        "iwiki.portable.PortableValidationReport",
        "iwiki.portable.PreparedBundle",
        "iwiki.portable.ValidationLevel",
        "iwiki.portable.commit_prepared_bundle",
        "iwiki.portable.inspect_portable_contract",
        "iwiki.portable.prepare_bundle_commit",
        "iwiki.portable.validate_bundle",
        "iwiki.workspace.Workspace",
        "iwiki.workspace.open_workspace",
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

    with pytest.raises(DomainError) as caught:
        gateway.commit_prepared(prepared)

    assert caught.value.code == "portable_contract_incompatible"
    assert caught.value.category is ErrorCategory.WORKSPACE_INCOMPATIBLE
    with pytest.raises(IWikiError):
        prepared.__enter__()


def test_concurrent_commit_claims_prepared_handle_once(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = gateway.prepare_candidate(
        workspace_root,
        STAGING_RELATIVE_PATH,
        expected_bundle_id=BUNDLE_ID,
        expected_manifest_sha256=MANIFEST_SHA256,
    )
    sdk_barrier = threading.Barrier(2)
    start_barrier = threading.Barrier(3)
    count_lock = threading.Lock()
    call_count = 0
    errors: list[DomainError] = []

    def commit_once(_prepared: PreparedBundle) -> CommitResult:
        nonlocal call_count
        with count_lock:
            call_count += 1
        try:
            sdk_barrier.wait(timeout=1)
        except threading.BrokenBarrierError:
            pass
        return CommitResult(
            bundle_id=BUNDLE_ID,
            manifest_sha256=MANIFEST_SHA256,
            commit_sha256="sha256:" + "1" * 64,
            relative_path=f"raw/personal/bundles/{BUNDLE_ID}",
            idempotent=False,
        )

    def commit_in_thread() -> None:
        start_barrier.wait()
        try:
            gateway.commit_prepared(prepared)
        except DomainError as error:
            errors.append(error)

    monkeypatch.setattr(gateway_module, "commit_prepared_bundle", commit_once)
    threads = [threading.Thread(target=commit_in_thread) for _ in range(2)]
    for thread in threads:
        thread.start()
    start_barrier.wait()
    for thread in threads:
        thread.join(timeout=3)

    assert all(not thread.is_alive() for thread in threads)
    assert call_count == 1
    assert [error.code for error in errors] == [
        "portable_prepared_bundle_invalid"
    ]
    with pytest.raises(IWikiError):
        prepared.__enter__()


def test_discard_prepared_releases_abandoned_handle_and_is_idempotent(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
) -> None:
    prepared = gateway.prepare_candidate(
        workspace_root,
        STAGING_RELATIVE_PATH,
        expected_bundle_id=BUNDLE_ID,
        expected_manifest_sha256=MANIFEST_SHA256,
    )

    assert hasattr(gateway, "discard_prepared")
    gateway.discard_prepared(prepared)
    gateway.discard_prepared(prepared)

    with pytest.raises(IWikiError):
        prepared.__enter__()
    with pytest.raises(DomainError) as caught:
        gateway.commit_prepared(prepared)
    assert caught.value.code == "portable_prepared_bundle_invalid"


def test_discard_prepared_does_not_affect_another_handle(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    other_workspace = tmp_path / "other-workspace"
    shutil.copytree(FIXTURE_ROOT, other_workspace)
    for relative in (
        "raw/common",
        "wiki/common",
        "wiki/personal",
        ".cache",
    ):
        (other_workspace / relative).mkdir(parents=True, exist_ok=True)
    discarded = gateway.prepare_candidate(
        workspace_root,
        STAGING_RELATIVE_PATH,
        expected_bundle_id=BUNDLE_ID,
        expected_manifest_sha256=MANIFEST_SHA256,
    )
    committed = gateway.prepare_candidate(
        other_workspace,
        STAGING_RELATIVE_PATH,
        expected_bundle_id=BUNDLE_ID,
        expected_manifest_sha256=MANIFEST_SHA256,
    )

    gateway.discard_prepared(discarded)
    result = gateway.commit_prepared(committed)

    assert result.bundle_id == BUNDLE_ID
    with pytest.raises(IWikiError):
        discarded.__enter__()


def test_discard_prepared_rejects_open_foreign_handle(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
) -> None:
    owner = IWikiPortableGateway()
    prepared = owner.prepare_candidate(
        workspace_root,
        STAGING_RELATIVE_PATH,
        expected_bundle_id=BUNDLE_ID,
        expected_manifest_sha256=MANIFEST_SHA256,
    )

    try:
        with pytest.raises(DomainError) as caught:
            gateway.discard_prepared(prepared)

        assert caught.value.code == "portable_prepared_bundle_invalid"
        assert prepared.__enter__() is prepared
    finally:
        owner.discard_prepared(prepared)


def test_discard_loses_to_an_atomic_commit_claim(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = gateway.prepare_candidate(
        workspace_root,
        STAGING_RELATIVE_PATH,
        expected_bundle_id=BUNDLE_ID,
        expected_manifest_sha256=MANIFEST_SHA256,
    )
    sdk_entered = threading.Event()
    release_sdk = threading.Event()

    def commit_once(_prepared: PreparedBundle) -> CommitResult:
        sdk_entered.set()
        assert release_sdk.wait(timeout=3)
        return CommitResult(
            bundle_id=BUNDLE_ID,
            manifest_sha256=MANIFEST_SHA256,
            commit_sha256="sha256:" + "1" * 64,
            relative_path=f"raw/personal/bundles/{BUNDLE_ID}",
            idempotent=False,
        )

    monkeypatch.setattr(gateway_module, "commit_prepared_bundle", commit_once)
    commit_thread = threading.Thread(
        target=gateway.commit_prepared,
        args=(prepared,),
    )
    commit_thread.start()
    assert sdk_entered.wait(timeout=3)

    try:
        with pytest.raises(DomainError) as caught:
            gateway.discard_prepared(prepared)
    finally:
        release_sdk.set()
        commit_thread.join(timeout=3)

    assert caught.value.code == "portable_prepared_bundle_invalid"
    assert not commit_thread.is_alive()
    with pytest.raises(IWikiError):
        prepared.__enter__()


def test_discard_does_not_touch_handle_claimed_by_commit(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepare_instrumented_bundle(
        gateway,
        workspace_root,
        monkeypatch,
        _InstrumentedPreparedBundle(),
    )
    sdk_entered = threading.Event()
    release_sdk = threading.Event()
    sdk_calls = 0
    winner_errors: list[BaseException] = []

    def commit_once(_prepared: object) -> CommitResult:
        nonlocal sdk_calls
        sdk_calls += 1
        sdk_entered.set()
        assert release_sdk.wait(timeout=3)
        return CommitResult(
            bundle_id=BUNDLE_ID,
            manifest_sha256=MANIFEST_SHA256,
            commit_sha256="sha256:" + "1" * 64,
            relative_path=f"raw/personal/bundles/{BUNDLE_ID}",
            idempotent=False,
        )

    def commit_in_thread() -> None:
        try:
            gateway.commit_prepared(prepared)
        except BaseException as error:
            winner_errors.append(error)

    monkeypatch.setattr(gateway_module, "commit_prepared_bundle", commit_once)
    winner = threading.Thread(target=commit_in_thread)
    winner.start()
    assert sdk_entered.wait(timeout=3)

    try:
        with pytest.raises(DomainError) as caught:
            gateway.discard_prepared(prepared)

        assert caught.value.code == "portable_prepared_bundle_invalid"
        assert prepared.enter_calls == 0
        assert prepared.close_calls == 0
        assert sdk_calls == 1
    finally:
        release_sdk.set()
        winner.join(timeout=3)

    assert not winner.is_alive()
    assert winner_errors == []
    assert prepared.enter_calls == 0
    assert prepared.close_calls == 1


def test_claimed_discard_rejects_other_discard_and_commit_without_native_calls(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_entered = threading.Event()
    release_close = threading.Event()
    prepared = _prepare_instrumented_bundle(
        gateway,
        workspace_root,
        monkeypatch,
        _InstrumentedPreparedBundle(
            close_entered=close_entered,
            release_close=release_close,
        ),
    )
    sdk_calls = 0
    winner_errors: list[BaseException] = []

    def unexpected_commit(_prepared: object) -> CommitResult:
        nonlocal sdk_calls
        sdk_calls += 1
        raise AssertionError("claimed discard reached commit SDK")

    def discard_in_thread() -> None:
        try:
            gateway.discard_prepared(prepared)
        except BaseException as error:
            winner_errors.append(error)

    monkeypatch.setattr(
        gateway_module,
        "commit_prepared_bundle",
        unexpected_commit,
    )
    winner = threading.Thread(target=discard_in_thread)
    winner.start()
    assert close_entered.wait(timeout=3)

    try:
        with pytest.raises(DomainError) as discard_error:
            gateway.discard_prepared(prepared)
        with pytest.raises(DomainError) as commit_error:
            gateway.commit_prepared(prepared)

        assert discard_error.value.code == "portable_prepared_bundle_invalid"
        assert commit_error.value.code == "portable_prepared_bundle_invalid"
        assert prepared.enter_calls == 0
        assert prepared.close_calls == 1
        assert sdk_calls == 0
    finally:
        release_close.set()
        winner.join(timeout=3)

    assert not winner.is_alive()
    assert winner_errors == []
    assert prepared.enter_calls == 0
    assert prepared.close_calls == 1


def test_consumed_tombstones_are_bounded_and_never_probe_evicted_handles(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tombstone_limit = 128
    prepared_handles = [
        _InstrumentedPreparedBundle()
        for _ in range(tombstone_limit + 1)
    ]
    prepared_iterator = iter(prepared_handles)
    monkeypatch.setattr(
        gateway_module,
        "PreparedBundle",
        _InstrumentedPreparedBundle,
    )
    monkeypatch.setattr(
        gateway_module,
        "prepare_bundle_commit",
        lambda *_args, **_kwargs: next(prepared_iterator),
    )

    for _ in prepared_handles:
        prepared = gateway.prepare_candidate(
            workspace_root,
            STAGING_RELATIVE_PATH,
            expected_bundle_id=BUNDLE_ID,
            expected_manifest_sha256=MANIFEST_SHA256,
        )
        gateway.discard_prepared(prepared)

    gateway.discard_prepared(prepared_handles[-1])
    with pytest.raises(DomainError) as caught:
        gateway.discard_prepared(prepared_handles[0])

    assert caught.value.code == "portable_prepared_bundle_invalid"
    assert prepared_handles[-1].enter_calls == 0
    assert prepared_handles[-1].close_calls == 1
    assert prepared_handles[0].enter_calls == 0
    assert prepared_handles[0].close_calls == 1


def test_sdk_commit_failure_closes_claimed_handle(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = gateway.prepare_candidate(
        workspace_root,
        STAGING_RELATIVE_PATH,
        expected_bundle_id=BUNDLE_ID,
        expected_manifest_sha256=MANIFEST_SHA256,
    )

    def fail(_prepared: PreparedBundle) -> CommitResult:
        raise IWikiError(ErrorCode.RETRYABLE_RUNTIME, "temporary failure")

    monkeypatch.setattr(gateway_module, "commit_prepared_bundle", fail)

    with pytest.raises(DomainError) as caught:
        gateway.commit_prepared(prepared)

    assert caught.value.code == "iwiki_runtime_retryable"
    with pytest.raises(IWikiError):
        prepared.__enter__()


def test_runtime_primary_is_not_masked_by_close_failure(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepare_instrumented_bundle(
        gateway,
        workspace_root,
        monkeypatch,
        _InstrumentedPreparedBundle(
            close_error=OSError("secret C:/private/close")
        ),
    )

    def fail_open(_workspace_root: Path) -> object:
        raise RuntimeError("primary runtime failure")

    monkeypatch.setattr(gateway_module, "_open_locked_workspace", fail_open)

    with pytest.raises(RuntimeError, match="primary runtime failure"):
        gateway.commit_prepared(prepared)

    assert prepared.close_calls == 1


def test_mapped_iwiki_primary_is_not_masked_by_close_failure(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepare_instrumented_bundle(
        gateway,
        workspace_root,
        monkeypatch,
        _InstrumentedPreparedBundle(
            close_error=OSError("secret C:/private/close")
        ),
    )

    def fail_commit(_prepared: object) -> CommitResult:
        raise IWikiError(ErrorCode.RETRYABLE_RUNTIME, "primary iwiki failure")

    monkeypatch.setattr(gateway_module, "commit_prepared_bundle", fail_commit)

    with pytest.raises(DomainError) as caught:
        gateway.commit_prepared(prepared)

    assert caught.value.code == "iwiki_runtime_retryable"
    assert caught.value.category is ErrorCategory.RETRYABLE_RUNTIME
    assert prepared.close_calls == 1


def test_fatal_primary_is_not_masked_by_close_failure(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepare_instrumented_bundle(
        gateway,
        workspace_root,
        monkeypatch,
        _InstrumentedPreparedBundle(
            close_error=OSError("secret C:/private/close")
        ),
    )

    def fail_open(_workspace_root: Path) -> object:
        raise KeyboardInterrupt("primary fatal failure")

    monkeypatch.setattr(gateway_module, "_open_locked_workspace", fail_open)

    with pytest.raises(KeyboardInterrupt, match="primary fatal failure"):
        gateway.commit_prepared(prepared)

    assert prepared.close_calls == 1


def test_discard_reports_sanitized_close_failure_without_primary_error(
    gateway: IWikiPortableGateway,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepare_instrumented_bundle(
        gateway,
        workspace_root,
        monkeypatch,
        _InstrumentedPreparedBundle(
            close_error=OSError("secret C:/private/close")
        ),
    )

    with pytest.raises(DomainError) as caught:
        gateway.discard_prepared(prepared)

    assert caught.value.code == "portable_contract_incompatible"
    assert "C:/private" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    assert prepared.close_calls == 1


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

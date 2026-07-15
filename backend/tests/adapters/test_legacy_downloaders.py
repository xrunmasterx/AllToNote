from __future__ import annotations

import ast
import shutil
import sys
import warnings
from dataclasses import replace
from pathlib import Path

import pytest

from app.adapters.iwiki.portable_gateway import IWikiPortableGateway
from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.adapters.sources.legacy_video import VerifiedSourceIdentityRegistry
from app.core.domain.video import VideoProduceRequest
from app.core.ports.jobs import SourceIdentityBinding


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "workspace-v2"
LEGACY_ROOT = Path(__file__).parents[2] / "app" / "downloaders"
SOURCE_ID = "src_018f0000-0000-7000-8000-000000000001"
BUNDLE_ID = "bnd_018f0000-0000-7000-8000-000000000002"
MANIFEST_SHA256 = "sha256:" + "a" * 64


def _method_parameters(
    file_name: str,
    class_name: str,
    method_name: str,
) -> tuple[str, ...]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        tree = ast.parse((LEGACY_ROOT / file_name).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                    item.name == method_name
                ):
                    return tuple(argument.arg for argument in item.args.args)
    return ()


def test_legacy_metadata_only_capability_is_youtube_specific() -> None:
    cases = (
        ("bilibili_downloader.py", "BilibiliDownloader", False),
        ("youtube_downloader.py", "YoutubeDownloader", True),
        ("douyin_downloader.py", "DouyinDownloader", False),
        ("kuaishou_downloader.py", "KuaiShouDownloader", False),
        ("local_downloader.py", "LocalDownloader", False),
    )

    for file_name, class_name, expected in cases:
        parameters = _method_parameters(file_name, class_name, "download")
        assert ("skip_download" in parameters) is expected


def test_only_bilibili_and_youtube_override_legacy_subtitle_acquisition() -> None:
    cases = (
        ("bilibili_downloader.py", "BilibiliDownloader", True),
        ("youtube_downloader.py", "YoutubeDownloader", True),
        ("douyin_downloader.py", "DouyinDownloader", False),
        ("kuaishou_downloader.py", "KuaiShouDownloader", False),
        ("local_downloader.py", "LocalDownloader", False),
    )

    for file_name, class_name, expected in cases:
        parameters = _method_parameters(file_name, class_name, "download_subtitles")
        assert bool(parameters) is expected


class _Cache:
    def __init__(self, binding: SourceIdentityBinding | None = None) -> None:
        self.binding = binding
        self.cached: list[SourceIdentityBinding] = []
        self.discarded: list[SourceIdentityBinding] = []
        self.replacements: list[
            tuple[SourceIdentityBinding, SourceIdentityBinding]
        ] = []

    def read_source_identity_candidate(
        self, connector_id: str, canonical_identity: str
    ) -> SourceIdentityBinding | None:
        if self.binding is None:
            return None
        if (
            self.binding.connector_id,
            self.binding.canonical_identity,
        ) != (connector_id, canonical_identity):
            return None
        return self.binding

    def cache_source_identity_candidate(self, binding: SourceIdentityBinding) -> None:
        self.binding = binding
        self.cached.append(binding)

    def discard_source_identity_candidate(self, binding: SourceIdentityBinding) -> None:
        if self.binding == binding:
            self.binding = None
        self.discarded.append(binding)

    def replace_source_identity_candidate(
        self,
        observed: SourceIdentityBinding,
        replacement: SourceIdentityBinding,
    ) -> bool:
        self.replacements.append((observed, replacement))
        if self.binding != observed:
            return False
        self.binding = replacement
        return True


class _Truth:
    def __init__(
        self,
        *,
        verified: set[SourceIdentityBinding] | None = None,
        bindings_by_workspace: dict[Path, tuple[SourceIdentityBinding, ...]] | None = None,
    ) -> None:
        self.verified = verified or set()
        self.bindings_by_workspace = bindings_by_workspace or {}
        self.verify_calls: list[tuple[Path, SourceIdentityBinding]] = []

    def verify_committed_source_binding(
        self, workspace_root: Path, binding: SourceIdentityBinding
    ) -> bool:
        self.verify_calls.append((workspace_root, binding))
        return binding in self.verified

    def iter_verified_source_bindings(
        self, workspace_root: Path
    ) -> tuple[SourceIdentityBinding, ...]:
        return self.bindings_by_workspace.get(workspace_root, ())


def _binding(
    *,
    canonical_identity: str = "youtube:dQw4w9WgXcQ",
    manifest_sha256: str = MANIFEST_SHA256,
    source_id: str = SOURCE_ID,
    bundle_id: str = BUNDLE_ID,
) -> SourceIdentityBinding:
    return SourceIdentityBinding(
        connector_id="youtube",
        canonical_identity=canonical_identity,
        source_id=source_id,
        owning_bundle_id=bundle_id,
        manifest_sha256=manifest_sha256,
    )


def test_cached_source_identity_is_reused_only_after_exact_portable_verification(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    binding = _binding()
    cache = _Cache(binding)
    truth = _Truth(
        verified={binding},
        bindings_by_workspace={workspace.resolve(): (binding,)},
    )
    registry = VerifiedSourceIdentityRegistry(workspace, cache=cache, truth=truth)

    resolved = registry.resolve_verified("youtube", "youtube:dQw4w9WgXcQ")

    assert resolved == binding
    assert truth.verify_calls == [(workspace.resolve(), binding)]
    assert cache.discarded == []


def test_unverified_machine_cache_entry_is_a_miss_not_a_source_link(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    binding = _binding()
    cache = _Cache(binding)
    registry = VerifiedSourceIdentityRegistry(
        workspace,
        cache=cache,
        truth=_Truth(),
    )

    assert registry.resolve_verified("youtube", binding.canonical_identity) is None
    assert cache.discarded == [binding]


def test_missing_registry_is_rebuilt_from_verified_portable_truth(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    binding = _binding()
    cache = _Cache()
    truth = _Truth(
        verified={binding},
        bindings_by_workspace={workspace: (binding,)},
    )
    registry = VerifiedSourceIdentityRegistry(workspace, cache=cache, truth=truth)

    rebuilt = registry.rebuild_from_portable_truth()

    assert rebuilt == 1
    assert cache.cached == [binding]
    assert registry.resolve_verified("youtube", binding.canonical_identity) == binding


def test_source_identity_rebuild_is_workspace_local(tmp_path: Path) -> None:
    workspace_a = (tmp_path / "a").resolve()
    workspace_b = (tmp_path / "b").resolve()
    binding = _binding()
    truth = _Truth(
        verified={binding},
        bindings_by_workspace={workspace_a: (binding,)},
    )
    registry_a = VerifiedSourceIdentityRegistry(workspace_a, cache=_Cache(), truth=truth)
    registry_b = VerifiedSourceIdentityRegistry(workspace_b, cache=_Cache(), truth=truth)

    assert registry_a.rebuild_from_portable_truth() == 1
    assert registry_a.resolve_verified("youtube", binding.canonical_identity) == binding
    assert registry_b.rebuild_from_portable_truth() == 0
    assert registry_b.resolve_verified("youtube", binding.canonical_identity) is None


def test_verified_cache_hit_is_rejected_when_portable_truth_is_ambiguous(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    first = _binding()
    second = _binding(
        source_id="src_018f0000-0000-7000-8000-000000000011",
        bundle_id="bnd_018f0000-0000-7000-8000-000000000012",
        manifest_sha256="sha256:" + "b" * 64,
    )
    cache = _Cache(first)
    truth = _Truth(
        verified={first, second},
        bindings_by_workspace={workspace: (first, second)},
    )
    registry = VerifiedSourceIdentityRegistry(workspace, cache=cache, truth=truth)

    assert registry.resolve_verified("youtube", first.canonical_identity) is None
    assert cache.binding == first


def test_rebuild_replaces_stale_cache_with_exact_cas(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    stale = _binding()
    current = _binding(
        source_id="src_018f0000-0000-7000-8000-000000000021",
        bundle_id="bnd_018f0000-0000-7000-8000-000000000022",
        manifest_sha256="sha256:" + "c" * 64,
    )
    cache = _Cache(stale)
    truth = _Truth(
        verified={current},
        bindings_by_workspace={workspace: (current,)},
    )
    registry = VerifiedSourceIdentityRegistry(workspace, cache=cache, truth=truth)

    assert registry.rebuild_from_portable_truth() == 1
    assert cache.binding == current
    assert cache.replacements == [(stale, current)]


def test_rebuild_does_not_overwrite_concurrent_cache_replacement(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    stale = _binding()
    current = _binding(
        source_id="src_018f0000-0000-7000-8000-000000000031",
        bundle_id="bnd_018f0000-0000-7000-8000-000000000032",
        manifest_sha256="sha256:" + "d" * 64,
    )
    concurrent = _binding(
        source_id="src_018f0000-0000-7000-8000-000000000041",
        bundle_id="bnd_018f0000-0000-7000-8000-000000000042",
        manifest_sha256="sha256:" + "e" * 64,
    )

    class _RacingCache(_Cache):
        def replace_source_identity_candidate(
            self,
            observed: SourceIdentityBinding,
            replacement: SourceIdentityBinding,
        ) -> bool:
            self.binding = concurrent
            return False

    cache = _RacingCache(stale)
    truth = _Truth(
        verified={current},
        bindings_by_workspace={workspace: (current,)},
    )
    registry = VerifiedSourceIdentityRegistry(workspace, cache=cache, truth=truth)

    assert registry.rebuild_from_portable_truth() == 0
    assert cache.binding == concurrent


def test_sqlite_source_identity_cache_is_concrete_but_not_a_core_bare_read_port(
    tmp_path: Path,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "machine", clock=lambda: 0)
    binding = _binding()

    repository.cache_source_identity_candidate(binding)

    assert repository.read_source_identity_candidate(
        binding.connector_id, binding.canonical_identity
    ) == binding
    repository.discard_source_identity_candidate(binding)
    assert repository.read_source_identity_candidate(
        binding.connector_id, binding.canonical_identity
    ) is None


def test_sqlite_source_identity_cache_replace_is_exact_compare_and_swap(
    tmp_path: Path,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "machine", clock=lambda: 0)
    observed = _binding()
    replacement = replace(
        observed,
        source_id="src_018f0000-0000-7000-8000-000000000051",
        owning_bundle_id="bnd_018f0000-0000-7000-8000-000000000052",
        manifest_sha256="sha256:" + "f" * 64,
    )
    concurrent = replace(
        observed,
        source_id="src_018f0000-0000-7000-8000-000000000061",
        owning_bundle_id="bnd_018f0000-0000-7000-8000-000000000062",
    )
    repository.cache_source_identity_candidate(observed)

    assert repository.replace_source_identity_candidate(observed, replacement) is True
    assert repository.replace_source_identity_candidate(observed, concurrent) is False
    assert repository.read_source_identity_candidate(
        observed.connector_id,
        observed.canonical_identity,
    ) == replacement


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    shutil.copytree(FIXTURE_ROOT, root)
    shutil.rmtree(root / "raw" / "personal" / ".staging")
    for relative in (
        "raw/common",
        "raw/personal/.staging",
        "wiki/common",
        "wiki/personal",
        ".cache",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    return root


def test_iwiki_gateway_rebuilds_binding_from_real_committed_portable_truth(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    from app.runtime import FakeCallCounts, create_fake_runtime

    runtime = create_fake_runtime(tmp_path / "machine", calls=FakeCallCounts())
    request = VideoProduceRequest(
        request_schema_version=1,
        workspace_root=workspace_root,
        input_value="fixture://course",
        client_request_id="source-registry-rebuild",
    )
    result = runtime.wait_job(runtime.submit_video(request).job_id).result
    assert result is not None
    gateway = IWikiPortableGateway()

    bindings = gateway.iter_verified_source_bindings(workspace_root)

    assert bindings == (
        SourceIdentityBinding(
            connector_id="fixture",
            canonical_identity="fixture-video:course",
            source_id=result.source_id,
            owning_bundle_id=result.bundle_id,
            manifest_sha256=result.manifest_sha256,
        ),
    )
    assert gateway.verify_committed_source_binding(workspace_root, bindings[0])


def test_iwiki_gateway_treats_manifest_hash_mismatch_as_miss(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    from app.runtime import FakeCallCounts, create_fake_runtime

    runtime = create_fake_runtime(tmp_path / "machine", calls=FakeCallCounts())
    request = VideoProduceRequest(
        request_schema_version=1,
        workspace_root=workspace_root,
        input_value="fixture://course",
        client_request_id="source-registry-hash-mismatch",
    )
    result = runtime.wait_job(runtime.submit_video(request).job_id).result
    assert result is not None
    gateway = IWikiPortableGateway()
    wrong = SourceIdentityBinding(
        connector_id="fixture",
        canonical_identity="fixture-video:course",
        source_id=result.source_id,
        owning_bundle_id=result.bundle_id,
        manifest_sha256="sha256:" + "0" * 64,
    )

    assert gateway.verify_committed_source_binding(workspace_root, wrong) is False


@pytest.mark.parametrize("mutation", ("missing", "corrupt"))
def test_iwiki_gateway_treats_missing_or_corrupt_committed_bundle_as_miss(
    mutation: str,
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    from app.runtime import FakeCallCounts, create_fake_runtime

    runtime = create_fake_runtime(tmp_path / "machine", calls=FakeCallCounts())
    request = VideoProduceRequest(
        request_schema_version=1,
        workspace_root=workspace_root,
        input_value="fixture://course",
        client_request_id=f"source-registry-{mutation}",
    )
    result = runtime.wait_job(runtime.submit_video(request).job_id).result
    assert result is not None
    binding = IWikiPortableGateway().iter_verified_source_bindings(workspace_root)[0]
    manifest = (
        workspace_root
        / "raw"
        / "personal"
        / "bundles"
        / result.bundle_id
        / "bundle.json"
    )
    if mutation == "missing":
        manifest.unlink()
    else:
        manifest.write_bytes(b"{}\n")

    gateway = IWikiPortableGateway()
    assert gateway.verify_committed_source_binding(workspace_root, binding) is False
    assert gateway.iter_verified_source_bindings(workspace_root) == ()


def test_source_adapter_default_registry_is_lazy_and_excludes_unproven_modules() -> None:
    for module_name in tuple(sys.modules):
        if module_name.startswith("app.downloaders."):
            del sys.modules[module_name]

    from app.adapters.sources.legacy_video import default_connector_ids

    assert default_connector_ids() == (
        "bilibili",
        "douyin",
        "kuaishou",
        "local",
        "youtube",
    )
    assert "app.downloaders.xiaoyuzhoufm_download" not in sys.modules
    assert "app.downloaders.douyin_downloader" not in sys.modules

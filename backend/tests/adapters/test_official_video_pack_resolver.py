from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.adapters.video_packs.official_video_pack import (
    MEDIA_BASIC,
    official_video_pack_installed,
)
from app.adapters.video_packs.official_video_pack_installer import (
    install_official_video_pack,
)
from app.adapters.video_packs.official_video_pack_resolver import (
    OfficialVideoPackResolver,
)
from app.core.errors import DomainError
from app.runtime_paths import RuntimePaths
from tests.video_pack_support import trust_keys, write_pack_source


PLATFORM = "windows-x86_64"


def _paths(tmp_path: Path) -> RuntimePaths:
    return RuntimePaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "AllToNote",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "logs",
    )


def _installed(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    write_pack_source(source, MEDIA_BASIC, platform_tag=PLATFORM)
    paths = _paths(tmp_path)
    installed = install_official_video_pack(
        source,
        contract=MEDIA_BASIC,
        paths=paths,
        trusted_keys=trust_keys(),
        probe=lambda _verified, _stage: None,
        platform_tag=PLATFORM,
        environ={},
    )
    return paths, installed


def test_resolver_returns_only_verified_managed_entrypoints(tmp_path: Path) -> None:
    paths, installed = _installed(tmp_path)
    resolver = OfficialVideoPackResolver(
        paths,
        trusted_keys=trust_keys(),
        platform_tag=PLATFORM,
    )

    resolved = resolver.resolve_active(MEDIA_BASIC)

    assert resolved.pack_id == MEDIA_BASIC.pack_id
    assert resolved.pack_version == MEDIA_BASIC.pack_version
    assert resolved.manifest_sha256 == installed.manifest_sha256
    assert resolved.generation == installed.generation.resolve()
    assert set(resolved.entrypoints) == {"python", "ffmpeg", "ffprobe"}
    assert all(path.is_file() for path in resolved.entrypoints.values())


def test_exact_resolution_does_not_follow_changed_active_pointer(tmp_path: Path) -> None:
    paths, installed = _installed(tmp_path)
    pointer = installed.active_pointer
    payload = json.loads(pointer.read_text(encoding="utf-8"))
    payload["manifest_sha256"] = "sha256:" + "0" * 64
    pointer.write_text(
        json.dumps(payload, separators=(",", ":")),
        encoding="utf-8",
    )
    resolver = OfficialVideoPackResolver(
        paths,
        trusted_keys=trust_keys(),
        platform_tag=PLATFORM,
    )

    exact = resolver.resolve_exact(MEDIA_BASIC, installed.manifest_sha256)
    assert exact.generation == installed.generation.resolve()
    with pytest.raises(DomainError) as caught:
        resolver.resolve_active(MEDIA_BASIC)
    assert caught.value.code == "pack_active_invalid"


def test_missing_or_tampered_generation_fails_closed(tmp_path: Path) -> None:
    paths, installed = _installed(tmp_path)
    ffmpeg = installed.generation.joinpath(
        *MEDIA_BASIC.entrypoints(PLATFORM)["ffmpeg"].split("/")
    )
    ffmpeg.write_bytes(b"tampered")
    resolver = OfficialVideoPackResolver(
        paths,
        trusted_keys=trust_keys(),
        platform_tag=PLATFORM,
    )

    with pytest.raises(DomainError) as caught:
        resolver.resolve_exact(MEDIA_BASIC, installed.manifest_sha256)

    assert caught.value.code == "pack_generation_invalid"


def test_unknown_manifest_digest_is_not_substituted(tmp_path: Path) -> None:
    paths, _installed_result = _installed(tmp_path)
    resolver = OfficialVideoPackResolver(
        paths,
        trusted_keys=trust_keys(),
        platform_tag=PLATFORM,
    )

    with pytest.raises(DomainError) as caught:
        resolver.resolve_exact(MEDIA_BASIC, "sha256:" + "1" * 64)

    assert caught.value.code == "pack_generation_unavailable"


def test_resolver_keeps_read_only_compatibility_with_legacy_generation(
    tmp_path: Path,
) -> None:
    paths, installed = _installed(tmp_path)
    legacy_root = (
        paths.data_dir
        / "packs"
        / MEDIA_BASIC.pack_id
        / MEDIA_BASIC.pack_version
        / "installs"
    )
    legacy_root.mkdir()
    legacy_generation = legacy_root / installed.generation.name
    installed.generation.rename(legacy_generation)

    resolved = OfficialVideoPackResolver(
        paths,
        trusted_keys=trust_keys(),
        platform_tag=PLATFORM,
    ).resolve_active(MEDIA_BASIC)

    assert resolved.generation == legacy_generation.resolve()
    assert official_video_pack_installed(paths.data_dir, MEDIA_BASIC) is True


def test_invalid_new_generation_never_falls_back_to_valid_legacy(
    tmp_path: Path,
) -> None:
    paths, installed = _installed(tmp_path)
    legacy_root = (
        paths.data_dir
        / "packs"
        / MEDIA_BASIC.pack_id
        / MEDIA_BASIC.pack_version
        / "installs"
    )
    legacy_root.mkdir()
    installed.generation.rename(legacy_root / installed.generation.name)
    installed.generation.mkdir()

    with pytest.raises(DomainError) as caught:
        OfficialVideoPackResolver(
            paths,
            trusted_keys=trust_keys(),
            platform_tag=PLATFORM,
        ).resolve_active(MEDIA_BASIC)

    assert caught.value.code == "pack_active_invalid"
    assert official_video_pack_installed(paths.data_dir, MEDIA_BASIC) is False

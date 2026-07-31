from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.adapters.video_packs import official_video_pack_installer as installer
from app.adapters.video_packs.official_video_pack import (
    MEDIA_BASIC,
    TRANSCRIBE_CPU,
)
from app.adapters.video_packs.official_video_pack_installer import (
    install_official_video_pack,
)
from app.adapters.video_packs.official_video_pack_resolver import (
    OfficialVideoPackResolver,
)
from app.adapters.video_packs.official_video_pack_verifier import (
    verify_official_video_pack_generation,
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


@pytest.mark.parametrize("contract", (MEDIA_BASIC, TRANSCRIBE_CPU))
def test_install_activates_verified_immutable_generation(
    tmp_path: Path,
    contract,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    write_pack_source(source, contract, platform_tag=PLATFORM)
    paths = _paths(tmp_path)
    probes: list[str] = []

    result = install_official_video_pack(
        source,
        contract=contract,
        paths=paths,
        trusted_keys=trust_keys(),
        probe=lambda verified, _stage: probes.append(verified.pack_id),
        platform_tag=PLATFORM,
        environ={},
    )

    assert result.result == "installed"
    assert probes == [contract.pack_id]
    assert result.generation.name == result.manifest_sha256.removeprefix("sha256:")
    assert result.generation.parent == (
        paths.data_dir
        / "pack-store-v1"
        / ("m" if contract is MEDIA_BASIC else "t")
    )
    pointer = json.loads(result.active_pointer.read_text(encoding="utf-8"))
    assert pointer == {
        "schema_version": 1,
        "pack_id": contract.pack_id,
        "pack_version": contract.pack_version,
        "manifest_sha256": result.manifest_sha256,
    }
    verified = verify_official_video_pack_generation(
        result.generation,
        contract=contract,
        trusted_keys=trust_keys(),
        platform_tag=PLATFORM,
    )
    assert verified.manifest_sha256 == result.manifest_sha256


def test_install_is_idempotent_and_concurrent_safe(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    write_pack_source(source, MEDIA_BASIC, platform_tag=PLATFORM)
    paths = _paths(tmp_path)

    def install():
        return install_official_video_pack(
            source,
            contract=MEDIA_BASIC,
            paths=paths,
            trusted_keys=trust_keys(),
            probe=lambda _verified, _stage: None,
            platform_tag=PLATFORM,
            environ={},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _value: install(), range(2)))

    assert {item.result for item in results} == {"installed", "already_active"}
    assert results[0].manifest_sha256 == results[1].manifest_sha256
    installs = paths.data_dir / "pack-store-v1" / "m"
    assert not tuple(installs.rglob(".stage-*"))


def test_reinstall_reuses_verified_legacy_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    active_bytes = installed.active_pointer.read_bytes()

    repeated = install_official_video_pack(
        source,
        contract=MEDIA_BASIC,
        paths=paths,
        trusted_keys=trust_keys(),
        probe=lambda _verified, _stage: None,
        platform_tag=PLATFORM,
        environ={},
    )

    assert repeated.result == "already_active"
    assert repeated.generation == legacy_generation
    assert not installed.generation.exists()
    short_store = paths.data_dir / "pack-store-v1" / "m"
    original_sync = installer._sync_directory

    def fail_post_migration_sync(path: Path) -> None:
        if path == short_store:
            raise OSError("injected post-migration sync failure")
        original_sync(path)

    monkeypatch.setattr(installer, "_sync_directory", fail_post_migration_sync)

    migrated = install_official_video_pack(
        source,
        contract=MEDIA_BASIC,
        paths=paths,
        trusted_keys=trust_keys(),
        probe=lambda _verified, _stage: None,
        platform_tag=PLATFORM,
        environ={},
        repair=True,
    )

    assert migrated.result == "repaired"
    assert migrated.generation == installed.generation
    assert migrated.generation.parent == short_store
    assert migrated.generation.is_dir()
    assert legacy_generation.is_dir()
    assert migrated.active_pointer.read_bytes() == active_bytes
    resolved = OfficialVideoPackResolver(
        paths,
        trusted_keys=trust_keys(),
        platform_tag=PLATFORM,
    ).resolve_active(MEDIA_BASIC)
    assert resolved.generation == migrated.generation.resolve()


def test_probe_failure_does_not_publish_active_pointer(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    write_pack_source(source, TRANSCRIBE_CPU, platform_tag=PLATFORM)
    paths = _paths(tmp_path)

    with pytest.raises(RuntimeError, match="probe"):
        install_official_video_pack(
            source,
            contract=TRANSCRIBE_CPU,
            paths=paths,
            trusted_keys=trust_keys(),
            probe=lambda _verified, _stage: (_ for _ in ()).throw(
                RuntimeError("probe")
            ),
            platform_tag=PLATFORM,
            environ={},
        )

    pack_root = (
        paths.data_dir
        / "packs"
        / TRANSCRIBE_CPU.pack_id
        / TRANSCRIBE_CPU.pack_version
    )
    assert not (pack_root / "active.json").exists()
    assert not tuple((paths.data_dir / "pack-store-v1" / "t").iterdir())


def test_managed_generation_path_has_bounded_overhead_for_long_profiles(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    write_pack_source(source, TRANSCRIBE_CPU, platform_tag=PLATFORM)
    long_root = tmp_path.joinpath(*("Unicode profile with spaces",) * 4)
    paths = RuntimePaths(
        config_dir=long_root / "config",
        data_dir=long_root / "AllToNote",
        cache_dir=long_root / "cache",
        state_dir=long_root / "state",
        log_dir=long_root / "logs",
    )
    probe_roots: list[Path] = []

    result = install_official_video_pack(
        source,
        contract=TRANSCRIBE_CPU,
        paths=paths,
        trusted_keys=trust_keys(),
        probe=lambda _verified, stage: probe_roots.append(stage),
        platform_tag=PLATFORM,
        environ={},
    )

    store = paths.data_dir / "pack-store-v1" / "t"
    assert result.generation.parent == store
    assert probe_roots[0].parent == store
    legacy = (
        paths.data_dir
        / "packs"
        / TRANSCRIBE_CPU.pack_id
        / TRANSCRIBE_CPU.pack_version
        / "installs"
        / result.generation.name
    )
    assert len(str(result.generation)) < len(str(legacy))


def test_malformed_active_pointer_requires_explicit_repair(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    write_pack_source(source, MEDIA_BASIC, platform_tag=PLATFORM)
    paths = _paths(tmp_path)
    pack_root = (
        paths.data_dir / "packs" / MEDIA_BASIC.pack_id / MEDIA_BASIC.pack_version
    )
    pack_root.mkdir(parents=True)
    (pack_root / "active.json").write_text("{}", encoding="utf-8")

    with pytest.raises(DomainError) as caught:
        install_official_video_pack(
            source,
            contract=MEDIA_BASIC,
            paths=paths,
            trusted_keys=trust_keys(),
            probe=lambda _verified, _stage: None,
            platform_tag=PLATFORM,
            environ={},
        )
    assert caught.value.code == "pack_install_conflict"

    repaired = install_official_video_pack(
        source,
        contract=MEDIA_BASIC,
        paths=paths,
        trusted_keys=trust_keys(),
        probe=lambda _verified, _stage: None,
        platform_tag=PLATFORM,
        environ={},
        repair=True,
    )
    assert repaired.result == "repaired"


def test_active_development_override_blocks_managed_install(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    write_pack_source(source, MEDIA_BASIC, platform_tag=PLATFORM)

    with pytest.raises(DomainError) as caught:
        install_official_video_pack(
            source,
            contract=MEDIA_BASIC,
            paths=_paths(tmp_path),
            trusted_keys=trust_keys(),
            probe=lambda _verified, _stage: None,
            platform_tag=PLATFORM,
            environ={"ALLTONOTE_MEDIA_BASIC_ROOT": "C:\\developer-pack"},
        )

    assert caught.value.code == "pack_override_active"

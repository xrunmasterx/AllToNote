from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from filelock import Timeout

from app.adapters.documents import document_basic_pack_installer as installer
from app.adapters.documents.document_basic_pack import PACK_ID, PACK_VERSION
from app.adapters.documents.document_basic_pack_installer import (
    install_document_basic_pack,
)
from app.core.errors import DomainError, ErrorCategory
from app.runtime_paths import resolve_runtime_paths
from tests.document_pack_support import trust_keys, write_pack_source


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    write_pack_source(source)
    return source


def _install(tmp_path: Path, source: Path, **kwargs: object):
    paths = resolve_runtime_paths(local_data_parent=tmp_path / "local")
    return install_document_basic_pack(
        source,
        paths=paths,
        trusted_keys=trust_keys(),
        probe=lambda _python, _artifacts: None,
        **kwargs,
    )


def test_installer_publishes_verified_generation_then_active_pointer(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)

    result = _install(tmp_path, source)

    generation = result.generation
    assert result.result == "installed"
    assert generation.name == result.manifest_sha256.removeprefix("sha256:")
    assert (generation / "manifest.json").read_bytes() == (
        source / "manifest.json"
    ).read_bytes()
    receipt = json.loads((generation / "receipt.json").read_text(encoding="utf-8"))
    assert receipt == {
        "schema_version": 1,
        "pack_id": PACK_ID,
        "pack_version": PACK_VERSION,
        "manifest_sha256": result.manifest_sha256,
        "verified": True,
    }
    assert (generation / "receipt.json").read_bytes() == json.dumps(
        receipt,
        separators=(",", ":"),
    ).encode("utf-8")
    pointer = json.loads(result.active_pointer.read_text(encoding="utf-8"))
    assert pointer == {
        "schema_version": 1,
        "pack_id": PACK_ID,
        "pack_version": PACK_VERSION,
        "manifest_sha256": result.manifest_sha256,
    }
    assert result.active_pointer.read_bytes() == json.dumps(
        pointer,
        separators=(",", ":"),
    ).encode("utf-8")
    assert (generation.parent.parent / "install.lock").is_file()


def test_installer_is_idempotent_without_rewriting_active_pointer(tmp_path: Path) -> None:
    source = _source(tmp_path)
    first = _install(tmp_path, source)
    pointer_bytes = first.active_pointer.read_bytes()
    pointer_mtime = first.active_pointer.stat().st_mtime_ns

    second = _install(tmp_path, source)

    assert second.result == "already_active"
    assert second.generation == first.generation
    assert second.active_pointer.read_bytes() == pointer_bytes
    assert second.active_pointer.stat().st_mtime_ns == pointer_mtime


def test_installer_preserves_active_pointer_when_probe_fails(tmp_path: Path) -> None:
    source = _source(tmp_path)
    paths = resolve_runtime_paths(local_data_parent=tmp_path / "local")
    pack_root = paths.data_dir / "packs" / PACK_ID / PACK_VERSION
    pack_root.mkdir(parents=True)
    pointer = pack_root / "active.json"
    pointer.write_bytes(b"old-pointer")

    with pytest.raises(DomainError) as raised:
        install_document_basic_pack(
            source,
            paths=paths,
            trusted_keys=trust_keys(),
            probe=lambda _python, _artifacts: (_ for _ in ()).throw(
                DomainError(
                    "document_pack_invalid",
                    ErrorCategory.WORKSPACE_INCOMPATIBLE,
                    "probe failed",
                )
            ),
            repair=True,
        )

    assert raised.value.code == "document_pack_invalid"
    assert pointer.read_bytes() == b"old-pointer"


def test_installer_serializes_concurrent_same_source(tmp_path: Path) -> None:
    source = _source(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: _install(tmp_path, source), range(2)))

    assert sorted(result.result for result in results) == ["already_active", "installed"]
    assert results[0].generation == results[1].generation


def test_installer_rejects_source_inside_managed_pack_root(tmp_path: Path) -> None:
    paths = resolve_runtime_paths(local_data_parent=tmp_path / "local")
    source = paths.data_dir / "packs" / PACK_ID / PACK_VERSION / "source"
    write_pack_source(source)

    with pytest.raises(DomainError) as raised:
        install_document_basic_pack(
            source,
            paths=paths,
            trusted_keys=trust_keys(),
            probe=lambda _python, _artifacts: None,
        )

    assert raised.value.code == "pack_source_invalid"


def test_installer_requires_repair_for_malformed_active_pointer(tmp_path: Path) -> None:
    source = _source(tmp_path)
    paths = resolve_runtime_paths(local_data_parent=tmp_path / "local")
    pack_root = paths.data_dir / "packs" / PACK_ID / PACK_VERSION
    pack_root.mkdir(parents=True)
    (pack_root / "active.json").write_bytes(b"malformed")

    with pytest.raises(DomainError) as raised:
        install_document_basic_pack(
            source,
            paths=paths,
            trusted_keys=trust_keys(),
            probe=lambda _python, _artifacts: None,
        )
    assert raised.value.code == "pack_install_conflict"

    repaired = install_document_basic_pack(
        source,
        paths=paths,
        trusted_keys=trust_keys(),
        probe=lambda _python, _artifacts: None,
        repair=True,
    )
    assert repaired.result == "repaired"


def test_installer_never_overwrites_corrupt_existing_generation(tmp_path: Path) -> None:
    source = _source(tmp_path)
    first = _install(tmp_path, source)
    first.generation.joinpath("artifacts", "model.bin").write_bytes(b"corrupt")
    original = first.generation.joinpath("artifacts", "model.bin").read_bytes()

    with pytest.raises(DomainError) as raised:
        _install(tmp_path, source, repair=True)

    assert raised.value.code == "pack_install_conflict"
    assert first.generation.joinpath("artifacts", "model.bin").read_bytes() == original


def test_installer_maps_busy_lock_without_creating_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)

    def busy(*_args: object, **_kwargs: object) -> None:
        raise Timeout("held")

    monkeypatch.setattr(installer._PackFileLock, "acquire", busy)

    with pytest.raises(DomainError) as raised:
        _install(tmp_path, source)

    assert raised.value.code == "pack_install_busy"
    paths = resolve_runtime_paths(local_data_parent=tmp_path / "local")
    installs = paths.data_dir / "packs" / PACK_ID / PACK_VERSION / "installs"
    assert not tuple(installs.glob(".stage-*"))


def test_installer_rejects_hardlinked_lock_without_truncating_target(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    paths = resolve_runtime_paths(local_data_parent=tmp_path / "local")
    pack_root = paths.data_dir / "packs" / PACK_ID / PACK_VERSION
    pack_root.mkdir(parents=True)
    external = tmp_path / "external-lock-target"
    external.write_bytes(b"must-stay-intact")
    try:
        os.link(external, pack_root / "install.lock")
    except OSError:
        pytest.skip("hardlinks are unavailable on this host")

    with pytest.raises(DomainError) as raised:
        install_document_basic_pack(
            source,
            paths=paths,
            trusted_keys=trust_keys(),
            probe=lambda _python, _artifacts: None,
        )

    assert raised.value.code == "pack_install_conflict"
    assert external.read_bytes() == b"must-stay-intact"


def test_installer_preserves_inactive_generation_when_activation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)

    def fail_activation(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected activation failure")

    monkeypatch.setattr(installer, "_write_active_pointer", fail_activation)

    with pytest.raises(DomainError) as raised:
        _install(tmp_path, source)

    assert raised.value.code == "pack_install_failed"
    paths = resolve_runtime_paths(local_data_parent=tmp_path / "local")
    pack_root = paths.data_dir / "packs" / PACK_ID / PACK_VERSION
    digest = hashlib.sha256((source / "manifest.json").read_bytes()).hexdigest()
    assert not (pack_root / "active.json").exists()
    assert (pack_root / "installs" / digest / "receipt.json").is_file()


def test_installer_treats_replace_as_activation_commit_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    paths = resolve_runtime_paths(local_data_parent=tmp_path / "local")
    pack_root = paths.data_dir / "packs" / PACK_ID / PACK_VERSION
    original_sync = installer._sync_directory

    def fail_post_replace_sync(path: Path) -> None:
        if path == pack_root:
            raise OSError("injected post-replace sync failure")
        original_sync(path)

    monkeypatch.setattr(installer, "_sync_directory", fail_post_replace_sync)

    result = install_document_basic_pack(
        source,
        paths=paths,
        trusted_keys=trust_keys(),
        probe=lambda _python, _artifacts: None,
    )

    assert result.result == "installed"
    assert json.loads(result.active_pointer.read_text(encoding="utf-8"))[
        "manifest_sha256"
    ] == result.manifest_sha256


def test_installer_blocks_development_override_before_managed_write(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    paths = resolve_runtime_paths(local_data_parent=tmp_path / "local")

    with pytest.raises(DomainError) as raised:
        install_document_basic_pack(
            source,
            paths=paths,
            trusted_keys=trust_keys(),
            probe=lambda _python, _artifacts: None,
            environ={"ALLTONOTE_DOCUMENT_BASIC_PYTHON": "dev-python"},
        )

    assert raised.value.code == "pack_override_active"
    assert not paths.data_dir.exists()


def test_installer_repairs_valid_pointer_with_missing_generation(tmp_path: Path) -> None:
    source = _source(tmp_path)
    paths = resolve_runtime_paths(local_data_parent=tmp_path / "local")
    pack_root = paths.data_dir / "packs" / PACK_ID / PACK_VERSION
    pack_root.mkdir(parents=True)
    manifest_sha256 = "sha256:" + hashlib.sha256(
        (source / "manifest.json").read_bytes()
    ).hexdigest()
    pointer = {
        "schema_version": 1,
        "pack_id": PACK_ID,
        "pack_version": PACK_VERSION,
        "manifest_sha256": manifest_sha256,
    }
    (pack_root / "active.json").write_text(
        json.dumps(pointer, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(DomainError) as raised:
        install_document_basic_pack(
            source,
            paths=paths,
            trusted_keys=trust_keys(),
            probe=lambda _python, _artifacts: None,
        )
    assert raised.value.code == "pack_install_conflict"

    repaired = install_document_basic_pack(
        source,
        paths=paths,
        trusted_keys=trust_keys(),
        probe=lambda _python, _artifacts: None,
        repair=True,
    )
    assert repaired.result == "repaired"
    assert repaired.generation.is_dir()


def test_installer_rejects_different_active_digest_even_with_repair(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    paths = resolve_runtime_paths(local_data_parent=tmp_path / "local")
    pack_root = paths.data_dir / "packs" / PACK_ID / PACK_VERSION
    pack_root.mkdir(parents=True)
    pointer = {
        "schema_version": 1,
        "pack_id": PACK_ID,
        "pack_version": PACK_VERSION,
        "manifest_sha256": "sha256:" + "b" * 64,
    }
    pointer_path = pack_root / "active.json"
    pointer_path.write_text(
        json.dumps(pointer, separators=(",", ":")),
        encoding="utf-8",
    )
    before = pointer_path.read_bytes()

    with pytest.raises(DomainError) as raised:
        install_document_basic_pack(
            source,
            paths=paths,
            trusted_keys=trust_keys(),
            probe=lambda _python, _artifacts: None,
            repair=True,
        )

    assert raised.value.code == "pack_install_conflict"
    assert pointer_path.read_bytes() == before


@pytest.mark.parametrize("linked_kind", ("symlink", "hardlink"))
def test_installer_rejects_linked_source_file(
    tmp_path: Path,
    linked_kind: str,
) -> None:
    source = _source(tmp_path)
    model = source / "artifacts" / "model.bin"
    external = tmp_path / "external-model.bin"
    external.write_bytes(model.read_bytes())
    model.unlink()
    try:
        if linked_kind == "symlink":
            model.symlink_to(external)
        else:
            os.link(external, model)
    except OSError:
        pytest.skip(f"{linked_kind} is unavailable on this host")

    with pytest.raises(DomainError) as raised:
        _install(tmp_path, source)

    assert raised.value.code == "pack_archive_unsafe"


def test_installer_cleans_only_recognized_orphan_stage(tmp_path: Path) -> None:
    source = _source(tmp_path)
    paths = resolve_runtime_paths(local_data_parent=tmp_path / "local")
    installs = paths.data_dir / "packs" / PACK_ID / PACK_VERSION / "installs"
    recognized = installs / (".stage-" + "a" * 32)
    unknown = installs / ".stage-keep-me"
    recognized.mkdir(parents=True)
    unknown.mkdir()
    (recognized / "partial.bin").write_bytes(b"partial")
    (unknown / "marker.bin").write_bytes(b"keep")

    _install(tmp_path, source)

    assert not recognized.exists()
    assert (unknown / "marker.bin").read_bytes() == b"keep"


def test_installer_accepts_unicode_and_space_source_path(tmp_path: Path) -> None:
    source = tmp_path / "\u4e2d\u6587 signed Pack"
    write_pack_source(source)

    result = _install(tmp_path, source)

    assert result.result == "installed"

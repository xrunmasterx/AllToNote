from __future__ import annotations

from pathlib import Path


_STORE_NAMES = {
    "document-basic": "d",
    "media-basic": "m",
    "transcribe-cpu": "t",
}


def managed_pack_root(
    data_dir: Path,
    pack_id: str,
    pack_version: str,
) -> Path:
    return Path(data_dir) / "packs" / pack_id / pack_version


def managed_generation_root(data_dir: Path, pack_id: str) -> Path:
    try:
        store_name = _STORE_NAMES[pack_id]
    except KeyError as error:
        raise ValueError("Pack has no managed generation store") from error
    return Path(data_dir) / "pack-store-v1" / store_name


def legacy_generation_root(
    data_dir: Path,
    pack_id: str,
    pack_version: str,
) -> Path:
    return managed_pack_root(data_dir, pack_id, pack_version) / "installs"


__all__ = [
    "legacy_generation_root",
    "managed_generation_root",
    "managed_pack_root",
]

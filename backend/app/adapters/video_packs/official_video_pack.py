from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


_CONTROL_FILE_LIMIT = 16 * 1024
_ACTIVE_KEYS = frozenset(
    {"schema_version", "pack_id", "pack_version", "manifest_sha256"}
)
_SHA256_PATTERN = re.compile(r"sha256:([0-9a-f]{64})\Z")


@dataclass(frozen=True, slots=True)
class OfficialVideoPackContract:
    pack_id: str
    pack_version: str
    recipe_contracts: Mapping[str, tuple[int, ...]]
    capabilities: tuple[str, ...]
    windows_entrypoints: Mapping[str, str]
    posix_entrypoints: Mapping[str, str]
    required_payload_files: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not self.pack_id
            or not self.pack_version
            or not self.recipe_contracts
            or not self.capabilities
            or not self.windows_entrypoints
            or not self.posix_entrypoints
            or not self.required_payload_files
        ):
            raise ValueError("Official Video Pack contract must be complete")
        object.__setattr__(
            self,
            "recipe_contracts",
            MappingProxyType(dict(self.recipe_contracts)),
        )
        object.__setattr__(
            self,
            "windows_entrypoints",
            MappingProxyType(dict(self.windows_entrypoints)),
        )
        object.__setattr__(
            self,
            "posix_entrypoints",
            MappingProxyType(dict(self.posix_entrypoints)),
        )

    def entrypoints(self, platform_tag: str) -> dict[str, str]:
        selected = (
            self.windows_entrypoints
            if platform_tag.startswith("windows-")
            else self.posix_entrypoints
        )
        return dict(selected)

    def fixture_file_paths(self, platform_tag: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    *self.entrypoints(platform_tag).values(),
                    *self.required_payload_files,
                    f"licenses/{self.pack_id}.txt",
                    "sbom.cdx.json",
                }
            )
        )


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _same_open_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _read_regular_file(path: Path, limit: int) -> bytes:
    metadata = path.lstat()
    if (
        _is_link_or_reparse(metadata)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > limit
    ):
        raise OSError("unsafe_control_file")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if not _same_open_file(metadata, opened):
            raise OSError("control_file_changed")
        content = stream.read(limit + 1)
        finished = os.fstat(stream.fileno())
    current = path.lstat()
    if (
        len(content) > limit
        or not _same_open_file(opened, finished)
        or not _same_open_file(metadata, current)
    ):
        raise OSError("control_file_changed")
    return content


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _ordinary_directory(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and not _is_link_or_reparse(metadata)


def _ordinary_file(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and not _is_link_or_reparse(metadata)
    )


def _contained_payload_file(generation: Path, relative_path: str) -> bool:
    target = generation.joinpath(*relative_path.split("/"))
    current = generation
    for part in relative_path.split("/")[:-1]:
        current /= part
        if not _ordinary_directory(current):
            return False
    return _ordinary_file(target)


def official_video_pack_installed(
    data_dir: Path,
    contract: OfficialVideoPackContract,
) -> bool:
    """Return a bounded, non-executing readiness hint for Runtime info."""

    pack_root = Path(data_dir) / "packs" / contract.pack_id / contract.pack_version
    try:
        active_bytes = _read_regular_file(
            pack_root / "active.json", _CONTROL_FILE_LIMIT
        )
        active = json.loads(
            active_bytes.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non_finite_json")
            ),
        )
    except (OSError, UnicodeError, ValueError):
        return False
    manifest_sha256 = active.get("manifest_sha256") if type(active) is dict else None
    match = (
        _SHA256_PATTERN.fullmatch(manifest_sha256)
        if type(manifest_sha256) is str
        else None
    )
    if (
        type(active) is not dict
        or frozenset(active) != _ACTIVE_KEYS
        or type(active.get("schema_version")) is not int
        or active.get("schema_version") != 1
        or active.get("pack_id") != contract.pack_id
        or active.get("pack_version") != contract.pack_version
        or match is None
    ):
        return False
    installs_root = pack_root / "installs"
    generation = installs_root / match.group(1)
    controlled_directories = (
        Path(data_dir),
        Path(data_dir) / "packs",
        Path(data_dir) / "packs" / contract.pack_id,
        pack_root,
        installs_root,
        generation,
    )
    if not all(_ordinary_directory(path) for path in controlled_directories):
        return False
    try:
        resolved_data = Path(data_dir).resolve(strict=True)
        resolved_generation = generation.resolve(strict=True)
    except OSError:
        return False
    if not resolved_generation.is_relative_to(resolved_data):
        return False
    expected_receipt = json.dumps(
        {
            "schema_version": 1,
            "pack_id": contract.pack_id,
            "pack_version": contract.pack_version,
            "manifest_sha256": manifest_sha256,
            "verified": True,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        receipt = _read_regular_file(
            generation / "receipt.json", _CONTROL_FILE_LIMIT
        )
    except OSError:
        return False
    if receipt != expected_receipt or not _ordinary_file(generation / "manifest.json"):
        return False
    required_paths = {
        *contract.required_payload_files,
        *contract.entrypoints(_current_platform_tag()).values(),
    }
    return all(
        _contained_payload_file(generation, relative_path)
        for relative_path in required_paths
    )


def _current_platform_tag() -> str:
    return "windows-x86_64" if os.name == "nt" else "posix-x86_64"


_VIDEO_RECIPES = MappingProxyType(
    {
        "alltonote.video-course-note": (1,),
        "alltonote.video-producer": (2,),
    }
)

MEDIA_BASIC = OfficialVideoPackContract(
    pack_id="media-basic",
    pack_version="yt-dlp-2026.7.4-ffmpeg-8.1.2-r1",
    recipe_contracts=_VIDEO_RECIPES,
    capabilities=(
        "recipe.video.acquire.bilibili",
        "recipe.video.acquire.local",
        "tool.ffmpeg",
        "tool.ffprobe",
    ),
    windows_entrypoints={
        "python": "python/python.exe",
        "ffmpeg": "bin/ffmpeg.exe",
        "ffprobe": "bin/ffprobe.exe",
    },
    posix_entrypoints={
        "python": "python/bin/python",
        "ffmpeg": "bin/ffmpeg",
        "ffprobe": "bin/ffprobe",
    },
    required_payload_files=("media-basic-lock.json",),
)

TRANSCRIBE_CPU = OfficialVideoPackContract(
    pack_id="transcribe-cpu",
    pack_version="faster-whisper-1.1.1-small-536b0662-r1",
    recipe_contracts=_VIDEO_RECIPES,
    capabilities=("recipe.video.transcribe.cpu-int8",),
    windows_entrypoints={"python": "python/python.exe"},
    posix_entrypoints={"python": "python/bin/python"},
    required_payload_files=(
        "transcribe-cpu-lock.json",
        "models/small/config.json",
        "models/small/model.bin",
        "models/small/tokenizer.json",
        "models/small/vocabulary.txt",
    ),
)

OFFICIAL_VIDEO_PACKS = MappingProxyType(
    {
        MEDIA_BASIC.pack_id: MEDIA_BASIC,
        TRANSCRIBE_CPU.pack_id: TRANSCRIBE_CPU,
    }
)


def official_video_pack(pack_id: str) -> OfficialVideoPackContract:
    try:
        return OFFICIAL_VIDEO_PACKS[pack_id]
    except KeyError:
        raise ValueError("Unknown official Video Pack") from None


__all__ = [
    "MEDIA_BASIC",
    "OFFICIAL_VIDEO_PACKS",
    "OfficialVideoPackContract",
    "TRANSCRIBE_CPU",
    "official_video_pack_installed",
    "official_video_pack",
]

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


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
    "official_video_pack",
]

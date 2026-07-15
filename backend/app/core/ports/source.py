from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class MaterializationPolicy(StrEnum):
    REFERENCE_ONLY = "reference_only"
    EXTERNAL_LOCAL = "external_local"


class SubtitleAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"
    NOT_SUPPORTED = "not_supported"


@dataclass(frozen=True)
class LocalMachineBinding:
    binding_id: str
    machine_id: str
    content_sha256: str
    path: Path = field(repr=False, compare=False)


@dataclass(frozen=True)
class ResolvedVideoSource:
    connector_id: str
    connector_version: str
    platform: str
    canonical_identity_scheme: str
    stable_video_identity: str
    canonical_identity: str
    canonical_uri: str | None
    logical_reference: str | None
    materialization_policy: MaterializationPolicy
    content_sha256: str | None = None
    local_binding: LocalMachineBinding | None = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True)
class AcquiredVideoSource:
    source: ResolvedVideoSource
    title: str | None
    duration_ms: int | None
    cover_uri: str | None
    media_path: Path | None = field(repr=False)
    video_path: Path | None = field(repr=False)
    subtitle_availability: SubtitleAvailability
    opaque_subtitle: object | None = field(
        default=None,
        repr=False,
        compare=False,
    )


class CancellationTokenPort(Protocol):
    def raise_if_cancelled(self) -> None: ...


class VideoSourcePort(Protocol):
    """Boundary for video identity resolution and acquisition."""

    def resolve(self, input_value: str) -> ResolvedVideoSource: ...

    def acquire(
        self,
        source: ResolvedVideoSource,
        *,
        need_media: bool,
        output_dir: Path,
        token: CancellationTokenPort,
    ) -> AcquiredVideoSource: ...


__all__ = [
    "AcquiredVideoSource",
    "CancellationTokenPort",
    "LocalMachineBinding",
    "MaterializationPolicy",
    "ResolvedVideoSource",
    "SubtitleAvailability",
    "VideoSourcePort",
]

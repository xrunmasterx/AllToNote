from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from app.core.errors import DomainError, ErrorCategory, ErrorDetail


_SEGMENT_ID_PATTERN = re.compile(r"seg_[0-9]{6,}\Z")


class ScreenshotPolicy(StrEnum):
    OFF = "off"
    ON_DEMAND = "on_demand"


class QualityOverall(StrEnum):
    PASS = "pass"
    PASS_WITH_WARNINGS = "pass_with_warnings"
    FAIL = "fail"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _snapshot_mapping(
    value: Mapping[str, int | float | str],
) -> Mapping[str, int | float | str]:
    return MappingProxyType(dict(value))


def _require_text(value: str, code: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise DomainError(
            code,
            ErrorCategory.INVALID_REQUEST,
            f"{field_name} must not be empty",
            {"field": field_name},
        )


@dataclass(frozen=True)
class TranscriptSegment:
    segment_id: str
    start_ms: int
    end_ms: int
    text: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.segment_id, str)
            or _SEGMENT_ID_PATTERN.fullmatch(self.segment_id) is None
            or isinstance(self.start_ms, bool)
            or isinstance(self.end_ms, bool)
            or not isinstance(self.start_ms, int)
            or not isinstance(self.end_ms, int)
            or self.start_ms < 0
            or self.end_ms <= self.start_ms
            or not isinstance(self.text, str)
            or not self.text.strip()
        ):
            raise DomainError(
                "transcript_segment_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Transcript segment must have an ID, text, and a valid half-open range",
                {"segment_id": self.segment_id},
            )


@dataclass(frozen=True)
class TranscriptDocument:
    language: str
    segments: tuple[TranscriptSegment, ...]

    def __post_init__(self) -> None:
        _require_text(self.language, "transcript_language_invalid", "language")
        segments = tuple(self.segments)
        object.__setattr__(self, "segments", segments)
        if not segments:
            raise DomainError(
                "transcript_empty",
                ErrorCategory.INVALID_REQUEST,
                "Transcript must contain at least one segment",
            )
        segment_ids = [segment.segment_id for segment in segments]
        if len(segment_ids) != len(set(segment_ids)):
            raise DomainError(
                "transcript_segment_duplicate",
                ErrorCategory.INVALID_REQUEST,
                "Transcript segment IDs must be unique",
            )
        if any(
            current.start_ms < previous.start_ms
            for previous, current in zip(segments, segments[1:])
        ):
            raise DomainError(
                "transcript_order_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Transcript segments must be ordered by start time",
            )


@dataclass(frozen=True)
class ScreenshotRequest:
    segment_id: str
    offset_ms: int = 0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.segment_id, str)
            or _SEGMENT_ID_PATTERN.fullmatch(self.segment_id) is None
            or isinstance(self.offset_ms, bool)
            or not isinstance(self.offset_ms, int)
        ):
            raise DomainError(
                "screenshot_request_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Screenshot request must reference a segment and integer offset",
            )


@dataclass(frozen=True)
class GeneratedVideoDraft:
    markdown: str
    cited_segment_ids: tuple[str, ...]
    screenshot_requests: tuple[ScreenshotRequest, ...]
    model_identity: str
    usage: Mapping[str, int | float | str]
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.markdown, "generated_draft_invalid", "markdown")
        _require_text(self.model_identity, "generated_draft_invalid", "model_identity")
        cited_segment_ids = tuple(self.cited_segment_ids)
        screenshot_requests = tuple(self.screenshot_requests)
        warnings = tuple(self.warnings)
        if any(_SEGMENT_ID_PATTERN.fullmatch(value) is None for value in cited_segment_ids):
            raise DomainError(
                "generated_draft_citation_invalid",
                ErrorCategory.RECIPE_FAILED,
                "Generated draft contains an invalid transcript citation",
            )
        if len(cited_segment_ids) != len(set(cited_segment_ids)):
            raise DomainError(
                "generated_draft_citation_duplicate",
                ErrorCategory.RECIPE_FAILED,
                "Generated draft citations must be unique",
            )
        object.__setattr__(self, "cited_segment_ids", cited_segment_ids)
        object.__setattr__(self, "screenshot_requests", screenshot_requests)
        object.__setattr__(self, "usage", _snapshot_mapping(self.usage))
        object.__setattr__(self, "warnings", warnings)


@dataclass(frozen=True)
class VideoProduceRequest:
    request_schema_version: int
    workspace_root: Path
    input_value: str
    recipe_id: str = "alltonote.video-course-note"
    recipe_version: int = 1
    provider_profile: str = "default"
    model_override: str | None = None
    transcriber_profile: str = "default"
    output_language: str = "zh-CN"
    quality_preset: str = "balanced"
    style: str = "structured"
    screenshot_policy: ScreenshotPolicy = ScreenshotPolicy.OFF
    client_request_id: str | None = None
    principal: str = "local-user"
    provided_transcript: TranscriptDocument | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.request_schema_version, bool)
            or not isinstance(self.request_schema_version, int)
            or self.request_schema_version < 1
        ):
            raise DomainError(
                "request_schema_version_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Request schema version must be a positive integer",
            )
        if (
            isinstance(self.recipe_version, bool)
            or not isinstance(self.recipe_version, int)
            or self.recipe_version < 1
        ):
            raise DomainError(
                "recipe_version_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Recipe version must be a positive integer",
            )
        if not isinstance(self.workspace_root, Path):
            raise DomainError(
                "workspace_root_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Workspace root must be a Path",
            )
        if not isinstance(self.screenshot_policy, ScreenshotPolicy):
            raise DomainError(
                "screenshot_policy_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Screenshot policy must be a supported policy",
            )
        for field_name in (
            "input_value",
            "recipe_id",
            "provider_profile",
            "transcriber_profile",
            "output_language",
            "quality_preset",
            "style",
            "principal",
        ):
            _require_text(
                getattr(self, field_name), "video_produce_request_invalid", field_name
            )


@dataclass(frozen=True)
class VideoProduceResult:
    job_id: str
    run_id: str
    bundle_id: str
    manifest_sha256: str
    commit_sha256: str
    workspace_relative_bundle_path: str
    source_id: str
    source_revision_id: str
    primary_draft_artifact_id: str
    transcript_artifact_id: str
    evidence_set_artifact_id: str
    quality_report_artifact_id: str
    display_asset_ids: tuple[str, ...]
    quality_overall: QualityOverall
    publish_eligible: bool
    usage: Mapping[str, int | float | str]
    warnings: tuple[str, ...]
    idempotent: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "display_asset_ids", tuple(self.display_asset_ids))
        object.__setattr__(self, "usage", _snapshot_mapping(self.usage))
        object.__setattr__(self, "warnings", tuple(self.warnings))


@dataclass(frozen=True)
class JobSnapshot:
    job_id: str
    state: JobState
    active_attempt_id: str | None
    challenge_id: str | None
    retry_of_job_id: str | None
    result: VideoProduceResult | None
    error: ErrorDetail | None


@dataclass(frozen=True)
class RetryJobRequest:
    retry_request_schema_version: int
    client_request_id: str
    expected_original_job_state: JobState
    confirmed_unknown_operation_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "confirmed_unknown_operation_ids",
            tuple(self.confirmed_unknown_operation_ids),
        )

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Mapping

from app.core.config.model import JobConfigSnapshot
from app.core.errors import DomainError, ErrorCategory, ErrorDetail


_SEGMENT_ID_PATTERN = re.compile(r"seg_[0-9]{6,}\Z")
_ARTIFACT_ID_PATTERN = re.compile(
    r"art_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)


class ScreenshotPolicy(StrEnum):
    OFF = "off"
    ON_DEMAND = "on_demand"


class QualityOverall(StrEnum):
    PASS = "pass"
    PASS_WITH_WARNINGS = "pass_with_warnings"
    FAIL = "fail"


class VideoDocumentKind(StrEnum):
    KNOWLEDGE_NOTE = "knowledge-note"
    FAITHFUL_EDITION = "faithful-edition"


class FaithfulLanguagePolicy(StrEnum):
    PRESERVE_SOURCE = "preserve-source"
    TRANSLATE_TO_OUTPUT = "translate-to-output"


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
class ScreenshotPlanItem:
    ordinal: int
    segment_id: str
    segment_start_ms: int
    segment_end_ms: int
    timestamp_ms: int
    artifact_id: str
    relative_path: str

    def __post_init__(self) -> None:
        expected_path = f"assets/{self.artifact_id}.webp"
        if (
            type(self.ordinal) is not int
            or self.ordinal < 0
            or type(self.segment_id) is not str
            or _SEGMENT_ID_PATTERN.fullmatch(self.segment_id) is None
            or type(self.segment_start_ms) is not int
            or type(self.segment_end_ms) is not int
            or type(self.timestamp_ms) is not int
            or not self.segment_start_ms <= self.timestamp_ms < self.segment_end_ms
            or type(self.artifact_id) is not str
            or _ARTIFACT_ID_PATTERN.fullmatch(self.artifact_id) is None
            or type(self.relative_path) is not str
            or PurePosixPath(self.relative_path).as_posix() != self.relative_path
            or self.relative_path != expected_path
        ):
            raise DomainError(
                "screenshot_plan_invalid",
                ErrorCategory.INTERNAL,
                "Screenshot plan contains an invalid item",
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
class ResolvedVideoOutput:
    document_kind: VideoDocumentKind
    recipe_id: str
    recipe_version: int
    quality_preset: str

    def __post_init__(self) -> None:
        if not isinstance(self.document_kind, VideoDocumentKind):
            raise DomainError(
                "output_binding_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Output binding must use a supported document kind",
            )
        _require_text(self.recipe_id, "output_binding_invalid", "recipe_id")
        _require_text(
            self.quality_preset,
            "output_binding_invalid",
            "quality_preset",
        )
        if (
            isinstance(self.recipe_version, bool)
            or not isinstance(self.recipe_version, int)
            or self.recipe_version < 1
        ):
            raise DomainError(
                "output_binding_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Output binding recipe version must be a positive integer",
            )


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
    requested_outputs: tuple[VideoDocumentKind, ...] = (
        VideoDocumentKind.KNOWLEDGE_NOTE,
    )
    resolved_outputs: tuple[ResolvedVideoOutput, ...] | None = None
    faithful_language_policy: FaithfulLanguagePolicy = (
        FaithfulLanguagePolicy.PRESERVE_SOURCE
    )
    style: str = "structured"
    screenshot_policy: ScreenshotPolicy = ScreenshotPolicy.OFF
    client_request_id: str | None = None
    principal: str = "local-user"
    provided_transcript: TranscriptDocument | None = None
    config_snapshot: JobConfigSnapshot | None = field(
        default=None,
        repr=False,
        compare=False,
    )

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
        if self.config_snapshot is not None and not isinstance(
            self.config_snapshot, JobConfigSnapshot
        ):
            raise DomainError(
                "config_snapshot_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Job configuration snapshot is invalid",
            )
        try:
            requested_outputs = tuple(
                VideoDocumentKind(value) for value in self.requested_outputs
            )
        except (TypeError, ValueError):
            raise DomainError(
                "output_kind_unsupported",
                ErrorCategory.INVALID_REQUEST,
                "Requested outputs contain an unsupported document kind",
            ) from None
        if not requested_outputs:
            raise DomainError(
                "requested_outputs_empty",
                ErrorCategory.INVALID_REQUEST,
                "Requested outputs must contain at least one document kind",
            )
        requested_output_set = frozenset(requested_outputs)
        requested_outputs = tuple(
            kind for kind in VideoDocumentKind if kind in requested_output_set
        )
        if self.request_schema_version == 1 and requested_outputs != (
            VideoDocumentKind.KNOWLEDGE_NOTE,
        ):
            raise DomainError(
                "requested_outputs_requires_v2",
                ErrorCategory.INVALID_REQUEST,
                "Request schema v1 only supports the knowledge note output",
            )
        if not isinstance(self.faithful_language_policy, FaithfulLanguagePolicy):
            raise DomainError(
                "faithful_language_policy_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Faithful language policy must be a supported policy",
            )
        if (
            self.faithful_language_policy
            is FaithfulLanguagePolicy.TRANSLATE_TO_OUTPUT
            and VideoDocumentKind.FAITHFUL_EDITION not in requested_output_set
        ):
            raise DomainError(
                "faithful_language_policy_requires_faithful_output",
                ErrorCategory.INVALID_REQUEST,
                "Translation policy requires the faithful edition output",
            )
        object.__setattr__(self, "requested_outputs", requested_outputs)
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
        if self.resolved_outputs is None:
            recipe_bindings = {
                VideoDocumentKind.KNOWLEDGE_NOTE: (
                    "alltonote.video-course-note",
                    2,
                ),
                VideoDocumentKind.FAITHFUL_EDITION: (
                    "alltonote.video-faithful-edition",
                    1,
                ),
            }
            resolved_outputs = tuple(
                ResolvedVideoOutput(
                    document_kind,
                    (
                        self.recipe_id
                        if self.request_schema_version == 1
                        else recipe_bindings[document_kind][0]
                    ),
                    (
                        self.recipe_version
                        if self.request_schema_version == 1
                        else recipe_bindings[document_kind][1]
                    ),
                    self.quality_preset,
                )
                for document_kind in requested_outputs
            )
        else:
            resolved_outputs = tuple(self.resolved_outputs)
        if any(
            not isinstance(output, ResolvedVideoOutput)
            for output in resolved_outputs
        ):
            raise DomainError(
                "output_binding_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Output bindings must use the resolved output contract",
            )
        if (
            tuple(output.document_kind for output in resolved_outputs)
            != requested_outputs
            or any(
                output.quality_preset != self.quality_preset
                for output in resolved_outputs
            )
            or (
                self.request_schema_version == 1
                and (
                    resolved_outputs[0].recipe_id != self.recipe_id
                    or resolved_outputs[0].recipe_version != self.recipe_version
                )
            )
        ):
            raise DomainError(
                "output_binding_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Output bindings must match the normalized request outputs",
            )
        object.__setattr__(self, "resolved_outputs", resolved_outputs)


@dataclass(frozen=True)
class VideoProducedDocument:
    document_kind: VideoDocumentKind
    draft_artifact_id: str
    quality_report_artifact_id: str
    quality_overall: QualityOverall
    publish_eligible: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.document_kind, VideoDocumentKind)
            or type(self.draft_artifact_id) is not str
            or _ARTIFACT_ID_PATTERN.fullmatch(self.draft_artifact_id) is None
            or type(self.quality_report_artifact_id) is not str
            or _ARTIFACT_ID_PATTERN.fullmatch(self.quality_report_artifact_id) is None
            or self.draft_artifact_id == self.quality_report_artifact_id
            or not isinstance(self.quality_overall, QualityOverall)
            or type(self.publish_eligible) is not bool
        ):
            raise DomainError(
                "video_result_document_invalid",
                ErrorCategory.INTERNAL,
                "Video result document is invalid",
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
    documents: tuple[VideoProducedDocument, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "display_asset_ids", tuple(self.display_asset_ids))
        object.__setattr__(self, "usage", _snapshot_mapping(self.usage))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        documents = tuple(self.documents)
        if (
            any(not isinstance(document, VideoProducedDocument) for document in documents)
            or len({document.document_kind for document in documents}) != len(documents)
            or len({document.draft_artifact_id for document in documents}) != len(documents)
            or len({document.quality_report_artifact_id for document in documents})
            != len(documents)
        ):
            raise DomainError(
                "video_result_document_invalid",
                ErrorCategory.INTERNAL,
                "Video result documents are invalid",
            )
        if documents:
            primary = next(
                (
                    document
                    for document in documents
                    if document.draft_artifact_id
                    == self.primary_draft_artifact_id
                ),
                None,
            )
            if (
                primary is None
                or primary.quality_report_artifact_id
                != self.quality_report_artifact_id
                or primary.quality_overall is not self.quality_overall
                or primary.publish_eligible is not self.publish_eligible
            ):
                raise DomainError(
                    "video_result_document_invalid",
                    ErrorCategory.INTERNAL,
                    "Primary result projection is inconsistent",
                )
        object.__setattr__(self, "documents", documents)


@dataclass(frozen=True)
class JobSnapshot:
    job_id: str
    state: JobState
    cancellation_requested: bool
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

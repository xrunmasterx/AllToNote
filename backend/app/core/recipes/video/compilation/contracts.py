from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import StrEnum

from app.core.domain.video import ScreenshotRequest, TranscriptDocument
from app.core.errors import DomainError, ErrorCategory
from app.core.ports.model_executor import ModelExecutionBinding


class TranscriptBasis(StrEnum):
    UPLOADER_CAPTION = "uploader-caption"
    PLATFORM_CAPTION = "platform-caption"
    HUMAN_TRANSCRIPT = "human-transcript"
    ASR_TRANSCRIPT = "asr-transcript"
    UNKNOWN = "unknown"


class TranscriptQualityStatus(StrEnum):
    PASS = "pass"
    WARNING = "warning"
    FAIL = "fail"


class TranscriptCheckStatus(StrEnum):
    PASS = "pass"
    WARNING = "warning"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"


class CompilationQualityProfile(StrEnum):
    FAST = "fast"
    BALANCED = "balanced"
    THOROUGH = "thorough"


class CompilationTopology(StrEnum):
    DIRECT = "direct"
    MAP_COMPOSE = "map_compose"
    HIERARCHICAL_COMPOSE = "hierarchical_compose"
    PLANNED_SECTIONS = "planned_sections"


class CompilationWritingMode(StrEnum):
    WHOLE_ARTICLE = "whole_article"
    SECTION_BATCHES = "section_batches"


class CompilationEditorialMode(StrEnum):
    WHOLE_ARTICLE = "whole_article"
    BOUNDED_SECTIONS = "bounded_sections"


class KnowledgeItemKind(StrEnum):
    CONCEPT = "concept"
    CLAIM = "claim"
    PROCEDURE = "procedure"
    EXAMPLE = "example"
    CONSTRAINT = "constraint"
    WARNING = "warning"


class KnowledgeImportance(StrEnum):
    CORE = "core"
    SUPPORTING = "supporting"
    CONTEXT = "context"


class CoverageInputKind(StrEnum):
    SEGMENT = "segment"
    KNOWLEDGE_ITEM = "knowledge-item"


_SEGMENT_ID = re.compile(r"seg_[0-9]{6,}\Z")
_KNOWLEDGE_ITEM_ID = re.compile(r"ki_[0-9a-f]{64}\Z")


def _invalid(message: str) -> DomainError:
    return DomainError(
        "video_compilation_contract_invalid",
        ErrorCategory.INVALID_REQUEST,
        message,
    )


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _safe_ratio(value: object) -> bool:
    return type(value) is float and math.isfinite(value) and 0.0 <= value <= 1.0


def _sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


@dataclass(frozen=True)
class TranscriptConfidenceSummaryV1:
    minimum: float
    mean: float
    low_confidence_count: int

    def __post_init__(self) -> None:
        if (
            not _safe_ratio(self.minimum)
            or not _safe_ratio(self.mean)
            or not _nonnegative_int(self.low_confidence_count)
        ):
            raise _invalid("Transcript confidence summary is invalid")


@dataclass(frozen=True)
class TranscriptQualityCheckV1:
    check_id: str
    status: TranscriptCheckStatus

    def __post_init__(self) -> None:
        if (
            type(self.check_id) is not str
            or not self.check_id.strip()
            or not isinstance(self.status, TranscriptCheckStatus)
        ):
            raise _invalid("Transcript quality check is invalid")


@dataclass(frozen=True)
class TranscriptQualityInputV1:
    schema_version: int
    transcript: TranscriptDocument = field(repr=False)
    transcript_basis: TranscriptBasis
    source_duration_ms: int | None
    detected_languages: tuple[str, ...] = ()
    segment_confidences: tuple[float, ...] | None = field(default=None, repr=False)
    empty_segment_count: int = 0
    out_of_order_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.transcript, TranscriptDocument):
            raise _invalid("Transcript quality input is invalid")
        try:
            if isinstance(self.detected_languages, (str, bytes)):
                raise TypeError
            languages = tuple(sorted(self.detected_languages))
            confidences = (
                None
                if self.segment_confidences is None
                else tuple(self.segment_confidences)
            )
        except TypeError:
            raise _invalid("Transcript quality input is invalid") from None
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or not isinstance(self.transcript_basis, TranscriptBasis)
            or (
                self.source_duration_ms is not None
                and not _positive_int(self.source_duration_ms)
            )
            or any(type(language) is not str or not language.strip() for language in languages)
            or len(languages) != len(set(languages))
            or not _nonnegative_int(self.empty_segment_count)
            or not _nonnegative_int(self.out_of_order_count)
            or (
                confidences is not None
                and (
                    len(confidences) != len(self.transcript.segments)
                    or any(not _safe_ratio(value) for value in confidences)
                )
            )
        ):
            raise _invalid("Transcript quality input is invalid")
        object.__setattr__(self, "detected_languages", languages)
        object.__setattr__(self, "segment_confidences", confidences)


@dataclass(frozen=True)
class TranscriptQualityAssessmentV1:
    schema_version: int
    transcript_sha256: str
    status: TranscriptQualityStatus
    transcript_basis: TranscriptBasis
    source_language: str
    detected_languages: tuple[str, ...]
    duration_known: bool
    source_duration_ms: int | None
    transcript_start_ms: int
    transcript_end_ms: int
    coverage_ratio: float | None
    duplicate_ratio: float
    empty_segment_count: int
    out_of_order_count: int
    overlap_issue_count: int
    abnormal_gap_count: int
    abnormal_segment_count: int
    confidence_available: bool
    confidence_summary: TranscriptConfidenceSummaryV1 | None
    checks: tuple[TranscriptQualityCheckV1, ...]
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        try:
            if isinstance(self.detected_languages, (str, bytes)) or isinstance(
                self.warnings, (str, bytes)
            ):
                raise TypeError
            languages = tuple(sorted(self.detected_languages))
            checks = tuple(self.checks)
            warnings = tuple(self.warnings)
        except TypeError:
            raise _invalid("Transcript quality assessment is invalid") from None
        if not checks or any(
            not isinstance(check, TranscriptQualityCheckV1) for check in checks
        ):
            raise _invalid("Transcript quality assessment is invalid")
        derived_status = (
            TranscriptQualityStatus.FAIL
            if any(check.status is TranscriptCheckStatus.FAIL for check in checks)
            else TranscriptQualityStatus.WARNING
            if any(check.status is TranscriptCheckStatus.WARNING for check in checks)
            else TranscriptQualityStatus.PASS
        )
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or not _sha256(self.transcript_sha256)
            or self.status is not derived_status
            or not isinstance(self.transcript_basis, TranscriptBasis)
            or type(self.source_language) is not str
            or not self.source_language.strip()
            or len(languages) != len(set(languages))
            or type(self.duration_known) is not bool
            or self.duration_known != (self.source_duration_ms is not None)
            or (self.source_duration_ms is not None and not _positive_int(self.source_duration_ms))
            or not _nonnegative_int(self.transcript_start_ms)
            or not _positive_int(self.transcript_end_ms)
            or self.transcript_end_ms <= self.transcript_start_ms
            or (self.duration_known != (self.coverage_ratio is not None))
            or (self.coverage_ratio is not None and not _safe_ratio(self.coverage_ratio))
            or not _safe_ratio(self.duplicate_ratio)
            or any(
                not _nonnegative_int(value)
                for value in (
                    self.empty_segment_count,
                    self.out_of_order_count,
                    self.overlap_issue_count,
                    self.abnormal_gap_count,
                    self.abnormal_segment_count,
                )
            )
            or type(self.confidence_available) is not bool
            or self.confidence_available != (self.confidence_summary is not None)
            or not checks
            or len({check.check_id for check in checks}) != len(checks)
            or any(type(item) is not str or not item.strip() for item in warnings)
            or len(warnings) != len(set(warnings))
        ):
            raise _invalid("Transcript quality assessment is invalid")
        object.__setattr__(self, "detected_languages", languages)
        object.__setattr__(self, "checks", checks)
        object.__setattr__(self, "warnings", warnings)


@dataclass(frozen=True)
class KnowledgeMapParserLimitsV1:
    max_response_bytes: int
    max_items: int
    max_title_characters: int
    max_statement_characters: int
    max_segment_refs_per_item: int
    max_term_candidates: int
    max_term_characters: int
    max_warnings: int
    max_warning_characters: int

    def __post_init__(self) -> None:
        if any(not _positive_int(value) for value in vars(self).values()):
            raise _invalid("Knowledge Map parser limits are invalid")


@dataclass(frozen=True)
class KnowledgeItemV1:
    knowledge_item_id: str
    kind: KnowledgeItemKind
    title: str
    statement: str
    importance: KnowledgeImportance
    source_segment_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        try:
            source_segment_ids = tuple(self.source_segment_ids)
        except TypeError:
            raise _invalid("Knowledge item is invalid") from None
        if (
            type(self.knowledge_item_id) is not str
            or _KNOWLEDGE_ITEM_ID.fullmatch(self.knowledge_item_id) is None
            or not isinstance(self.kind, KnowledgeItemKind)
            or type(self.title) is not str
            or not self.title.strip()
            or type(self.statement) is not str
            or not self.statement.strip()
            or not isinstance(self.importance, KnowledgeImportance)
            or not source_segment_ids
            or any(
                type(segment_id) is not str
                or _SEGMENT_ID.fullmatch(segment_id) is None
                for segment_id in source_segment_ids
            )
            or len(source_segment_ids) != len(set(source_segment_ids))
        ):
            raise _invalid("Knowledge item is invalid")
        object.__setattr__(self, "source_segment_ids", source_segment_ids)


@dataclass(frozen=True)
class ChunkKnowledgeMapV1:
    schema_version: int
    chunk_ordinal: int
    chunk_sha256: str
    items: tuple[KnowledgeItemV1, ...]
    term_candidates: tuple[str, ...]
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        try:
            items = tuple(self.items)
            terms = tuple(self.term_candidates)
            warnings = tuple(self.warnings)
        except TypeError:
            raise _invalid("Chunk Knowledge Map is invalid") from None
        string_values = (*terms, *warnings)
        if (
            self.schema_version != 1
            or not _nonnegative_int(self.chunk_ordinal)
            or not _sha256(self.chunk_sha256)
            or not items
            or any(not isinstance(item, KnowledgeItemV1) for item in items)
            or len({item.knowledge_item_id for item in items}) != len(items)
            or any(type(value) is not str or not value.strip() for value in string_values)
            or len(terms) != len(set(terms))
            or len(warnings) != len(set(warnings))
        ):
            raise _invalid("Chunk Knowledge Map is invalid")
        object.__setattr__(self, "items", items)
        object.__setattr__(self, "term_candidates", terms)
        object.__setattr__(self, "warnings", warnings)


@dataclass(frozen=True)
class CoverageOmissionV1:
    input_id: str
    reason: str

    def __post_init__(self) -> None:
        if (
            type(self.input_id) is not str
            or not self.input_id.strip()
            or type(self.reason) is not str
            or not self.reason.strip()
        ):
            raise _invalid("Coverage omission is invalid")


@dataclass(frozen=True)
class CompositionCoverageLedgerV1:
    input_kind: CoverageInputKind
    covered_input_ids: tuple[str, ...]
    omissions: tuple[CoverageOmissionV1, ...]

    def __post_init__(self) -> None:
        try:
            covered = tuple(self.covered_input_ids)
            omissions = tuple(self.omissions)
        except TypeError:
            raise _invalid("Composition coverage ledger is invalid") from None
        if any(not isinstance(value, CoverageOmissionV1) for value in omissions):
            raise _invalid("Composition coverage ledger is invalid")
        omitted_ids = tuple(omission.input_id for omission in omissions)
        if (
            not isinstance(self.input_kind, CoverageInputKind)
            or any(type(value) is not str or not value.strip() for value in covered)
            or len(covered) != len(set(covered))
            or len(omitted_ids) != len(set(omitted_ids))
            or set(covered).intersection(omitted_ids)
        ):
            raise _invalid("Composition coverage ledger is invalid")
        object.__setattr__(self, "covered_input_ids", covered)
        object.__setattr__(self, "omissions", omissions)


@dataclass(frozen=True)
class ComposedKnowledgeDraftV1:
    markdown: str
    cited_segment_ids: tuple[str, ...]
    screenshot_requests: tuple[ScreenshotRequest, ...]
    coverage: CompositionCoverageLedgerV1
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        try:
            citations = tuple(self.cited_segment_ids)
            screenshots = tuple(self.screenshot_requests)
            warnings = tuple(self.warnings)
        except TypeError:
            raise _invalid("Composed Knowledge Draft is invalid") from None
        if (
            type(self.markdown) is not str
            or not self.markdown.strip()
            or any(
                type(value) is not str or _SEGMENT_ID.fullmatch(value) is None
                for value in citations
            )
            or len(citations) != len(set(citations))
            or any(not isinstance(value, ScreenshotRequest) for value in screenshots)
            or not isinstance(self.coverage, CompositionCoverageLedgerV1)
            or any(type(value) is not str or not value.strip() for value in warnings)
            or len(warnings) != len(set(warnings))
        ):
            raise _invalid("Composed Knowledge Draft is invalid")
        object.__setattr__(self, "cited_segment_ids", citations)
        object.__setattr__(self, "screenshot_requests", screenshots)
        object.__setattr__(self, "warnings", warnings)


@dataclass(frozen=True)
class ConsolidatedKnowledgeItemV1:
    item: KnowledgeItemV1
    merged_from: tuple[str, ...]

    def __post_init__(self) -> None:
        try:
            merged_from = tuple(self.merged_from)
        except TypeError:
            raise _invalid("Consolidated knowledge item is invalid") from None
        if (
            not isinstance(self.item, KnowledgeItemV1)
            or not merged_from
            or any(
                type(value) is not str or _KNOWLEDGE_ITEM_ID.fullmatch(value) is None
                for value in merged_from
            )
            or len(merged_from) != len(set(merged_from))
        ):
            raise _invalid("Consolidated knowledge item is invalid")
        object.__setattr__(self, "merged_from", merged_from)


@dataclass(frozen=True)
class ConsolidatedKnowledgeV1:
    schema_version: int
    batch_ordinal: int
    batch_sha256: str
    items: tuple[ConsolidatedKnowledgeItemV1, ...]
    omissions: tuple[CoverageOmissionV1, ...]
    term_candidates: tuple[str, ...]
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        try:
            items = tuple(self.items)
            omissions = tuple(self.omissions)
            terms = tuple(self.term_candidates)
            warnings = tuple(self.warnings)
        except TypeError:
            raise _invalid("Consolidated knowledge is invalid") from None
        if (
            self.schema_version != 1
            or not _nonnegative_int(self.batch_ordinal)
            or not _sha256(self.batch_sha256)
            or not items
            or any(not isinstance(value, ConsolidatedKnowledgeItemV1) for value in items)
            or any(not isinstance(value, CoverageOmissionV1) for value in omissions)
            or any(type(value) is not str or not value.strip() for value in (*terms, *warnings))
            or len(terms) != len(set(terms))
            or len(warnings) != len(set(warnings))
        ):
            raise _invalid("Consolidated knowledge is invalid")
        object.__setattr__(self, "items", items)
        object.__setattr__(self, "omissions", omissions)
        object.__setattr__(self, "term_candidates", terms)
        object.__setattr__(self, "warnings", warnings)


@dataclass(frozen=True)
class ComposerParserLimitsV1:
    max_response_bytes: int
    max_markdown_characters: int
    max_coverage_items: int
    max_omissions: int
    max_omission_reason_characters: int
    max_warnings: int
    max_warning_characters: int

    def __post_init__(self) -> None:
        if any(not _positive_int(value) for value in vars(self).values()):
            raise _invalid("Composer parser limits are invalid")


@dataclass(frozen=True)
class TranscriptChunkRefV1:
    ordinal: int
    start_segment_ordinal: int
    end_segment_ordinal_exclusive: int
    start_segment_id: str
    end_segment_id: str
    start_ms: int
    end_ms: int
    segment_count: int
    estimated_input_tokens: int
    encoded_input_bytes: int
    segment_ids_sha256: str

    def __post_init__(self) -> None:
        if (
            not _nonnegative_int(self.ordinal)
            or not _nonnegative_int(self.start_segment_ordinal)
            or not _positive_int(self.end_segment_ordinal_exclusive)
            or self.end_segment_ordinal_exclusive <= self.start_segment_ordinal
            or self.segment_count
            != self.end_segment_ordinal_exclusive - self.start_segment_ordinal
            or not _positive_int(self.segment_count)
            or not _positive_int(self.estimated_input_tokens)
            or not _positive_int(self.encoded_input_bytes)
            or type(self.start_segment_id) is not str
            or not self.start_segment_id.strip()
            or type(self.end_segment_id) is not str
            or not self.end_segment_id.strip()
            or not _nonnegative_int(self.start_ms)
            or not _positive_int(self.end_ms)
            or self.end_ms <= self.start_ms
            or not _sha256(self.segment_ids_sha256)
        ):
            raise _invalid("Transcript chunk reference is invalid")


@dataclass(frozen=True)
class VideoCompilationPlanningRequestV1:
    schema_version: int
    recipe_id: str
    recipe_version: int
    quality_profile: CompilationQualityProfile
    transcript: TranscriptDocument = field(repr=False)
    transcript_quality: TranscriptQualityAssessmentV1
    model_binding: ModelExecutionBinding = field(repr=False)
    stage_id: str
    stage_version: int
    prompt_id: str
    prompt_version: int
    prompt_overhead_tokens: int
    prompt_overhead_bytes: int
    reserved_output_tokens: int
    max_request_bytes: int
    max_chunk_duration_ms: int
    estimated_map_output_tokens_per_chunk: int
    map_output_byte_budget_per_chunk: int
    max_repair_attempts: int

    def __post_init__(self) -> None:
        text_values = (self.recipe_id, self.stage_id, self.prompt_id)
        positive_values = (
            self.schema_version,
            self.recipe_version,
            self.stage_version,
            self.prompt_version,
            self.prompt_overhead_tokens,
            self.prompt_overhead_bytes,
            self.reserved_output_tokens,
            self.max_request_bytes,
            self.max_chunk_duration_ms,
            self.estimated_map_output_tokens_per_chunk,
            self.map_output_byte_budget_per_chunk,
        )
        if (
            self.schema_version != 1
            or any(type(value) is not str or not value.strip() for value in text_values)
            or any(not _positive_int(value) for value in positive_values)
            or not isinstance(self.quality_profile, CompilationQualityProfile)
            or not isinstance(self.transcript, TranscriptDocument)
            or not isinstance(self.transcript_quality, TranscriptQualityAssessmentV1)
            or not isinstance(self.model_binding, ModelExecutionBinding)
            or self.reserved_output_tokens > self.model_binding.max_output_tokens
            or self.estimated_map_output_tokens_per_chunk
            > self.reserved_output_tokens
            or self.map_output_byte_budget_per_chunk > self.max_request_bytes
            or self.max_request_bytes <= self.prompt_overhead_bytes
            or type(self.max_repair_attempts) is not int
            or not 0 <= self.max_repair_attempts <= 1
        ):
            raise _invalid("Video compilation planning request is invalid")


@dataclass(frozen=True)
class VideoCompilationPlanV1:
    schema_version: int
    recipe_id: str
    recipe_version: int
    quality_profile: CompilationQualityProfile
    topology: CompilationTopology
    transcript_sha256: str
    model_binding_sha256: str
    stage_id: str
    stage_version: int
    prompt_id: str
    prompt_version: int
    transcript_chunks: tuple[TranscriptChunkRefV1, ...]
    token_estimator_id: str
    chunk_input_token_budget: int
    chunk_input_byte_budget: int
    extraction_concurrency: int
    expected_sequential_model_waves: int
    writing_mode: CompilationWritingMode
    editorial_mode: CompilationEditorialMode
    reviewer_enabled: bool
    max_repair_attempts: int

    def __post_init__(self) -> None:
        try:
            chunks = tuple(self.transcript_chunks)
        except TypeError:
            raise _invalid("Video compilation plan is invalid") from None
        if not chunks or any(
            not isinstance(chunk, TranscriptChunkRefV1) for chunk in chunks
        ):
            raise _invalid("Video compilation plan is invalid")
        contiguous = all(
            previous.end_segment_ordinal_exclusive == current.start_segment_ordinal
            for previous, current in zip(chunks, chunks[1:])
        )
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or type(self.recipe_id) is not str
            or not self.recipe_id.strip()
            or not _positive_int(self.recipe_version)
            or not isinstance(self.quality_profile, CompilationQualityProfile)
            or not isinstance(self.topology, CompilationTopology)
            or not _sha256(self.transcript_sha256)
            or not _sha256(self.model_binding_sha256)
            or type(self.stage_id) is not str
            or not self.stage_id.strip()
            or not _positive_int(self.stage_version)
            or type(self.prompt_id) is not str
            or not self.prompt_id.strip()
            or not _positive_int(self.prompt_version)
            or [chunk.ordinal for chunk in chunks] != list(range(len(chunks)))
            or chunks[0].start_segment_ordinal != 0
            or not contiguous
            or type(self.token_estimator_id) is not str
            or not self.token_estimator_id.strip()
            or not _positive_int(self.chunk_input_token_budget)
            or not _positive_int(self.chunk_input_byte_budget)
            or not _positive_int(self.extraction_concurrency)
            or self.extraction_concurrency > len(chunks)
            or not _positive_int(self.expected_sequential_model_waves)
            or not isinstance(self.writing_mode, CompilationWritingMode)
            or not isinstance(self.editorial_mode, CompilationEditorialMode)
            or type(self.reviewer_enabled) is not bool
            or type(self.max_repair_attempts) is not int
            or not 0 <= self.max_repair_attempts <= 1
            or any(
                chunk.estimated_input_tokens > self.chunk_input_token_budget
                or chunk.encoded_input_bytes > self.chunk_input_byte_budget
                for chunk in chunks
            )
        ):
            raise _invalid("Video compilation plan is invalid")
        object.__setattr__(self, "transcript_chunks", chunks)


__all__ = [
    "ChunkKnowledgeMapV1",
    "ComposedKnowledgeDraftV1",
    "ComposerParserLimitsV1",
    "CompilationEditorialMode",
    "CompilationQualityProfile",
    "CompilationTopology",
    "CompilationWritingMode",
    "CompositionCoverageLedgerV1",
    "ConsolidatedKnowledgeItemV1",
    "ConsolidatedKnowledgeV1",
    "CoverageInputKind",
    "CoverageOmissionV1",
    "KnowledgeImportance",
    "KnowledgeItemKind",
    "KnowledgeItemV1",
    "KnowledgeMapParserLimitsV1",
    "TranscriptBasis",
    "TranscriptCheckStatus",
    "TranscriptChunkRefV1",
    "TranscriptConfidenceSummaryV1",
    "TranscriptQualityAssessmentV1",
    "TranscriptQualityCheckV1",
    "TranscriptQualityInputV1",
    "TranscriptQualityStatus",
    "VideoCompilationPlanV1",
    "VideoCompilationPlanningRequestV1",
]

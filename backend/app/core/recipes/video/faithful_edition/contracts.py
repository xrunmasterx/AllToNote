from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from app.core.domain.video import FaithfulLanguagePolicy, TranscriptDocument
from app.core.domain.transcript import transcript_sha256
from app.core.errors import DomainError, ErrorCategory
from app.core.recipes.video.compilation.contracts import (
    CompilationQualityProfile,
    TranscriptBasis,
    TranscriptQualityAssessmentV1,
)
from app.core.ports.model_executor import ModelExecutionBinding


_SEGMENT_ID = re.compile(r"seg_[0-9]{6,}\Z")
_SECTION_ID = re.compile(r"fs_[0-9a-f]{64}\Z")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


def invalid_contract(message: str) -> DomainError:
    return DomainError(
        "faithful_edition_contract_invalid",
        ErrorCategory.INVALID_REQUEST,
        message,
    )


def _positive(value: object) -> bool:
    return type(value) is int and value > 0


def _nonnegative(value: object) -> bool:
    return type(value) is int and value >= 0


def _text(value: object) -> bool:
    return type(value) is str and bool(value.strip())


class FaithfulUncertaintyCategory(StrEnum):
    ASR_TERM = "asr-term"
    PERSON_OR_ORGANIZATION = "person-or-organization"
    NUMBER = "number"
    CODE_OR_COMMAND = "code-or-command"
    LANGUAGE = "language"
    UNCLEAR_AUDIO = "unclear-audio"
    OTHER = "other"


@dataclass(frozen=True)
class FaithfulEditionParserLimitsV1:
    max_response_bytes: int
    max_title_characters: int
    max_paragraphs: int
    max_paragraph_characters: int
    max_segment_refs_per_paragraph: int
    max_key_points: int
    max_uncertainties: int
    max_auxiliary_text_characters: int
    max_warnings: int

    def __post_init__(self) -> None:
        if any(not _positive(value) for value in vars(self).values()):
            raise invalid_contract("Faithful parser limits are invalid")


@dataclass(frozen=True)
class FaithfulEditionRequestV1:
    schema_version: int
    recipe_id: str
    recipe_version: int
    quality_profile: CompilationQualityProfile
    transcript: TranscriptDocument = field(repr=False)
    transcript_quality: TranscriptQualityAssessmentV1
    transcript_basis: TranscriptBasis
    source_title: str
    source_language: str
    language_policy: FaithfulLanguagePolicy
    target_language: str | None
    model_binding: ModelExecutionBinding = field(repr=False)
    max_request_bytes: int
    section_input_byte_budget: int
    reserved_output_tokens: int
    parser_limits: FaithfulEditionParserLimitsV1
    max_repair_attempts: int

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or not _text(self.recipe_id)
            or not _positive(self.recipe_version)
            or self.quality_profile is not CompilationQualityProfile.BALANCED
            or not isinstance(self.transcript, TranscriptDocument)
            or not isinstance(self.transcript_quality, TranscriptQualityAssessmentV1)
            or self.transcript_quality.transcript_sha256
            != transcript_sha256(self.transcript)
            or not isinstance(self.transcript_basis, TranscriptBasis)
            or not _text(self.source_title)
            or not _text(self.source_language)
            or self.source_language != self.transcript.language
            or self.transcript_basis is not self.transcript_quality.transcript_basis
            or not isinstance(self.language_policy, FaithfulLanguagePolicy)
            or (
                self.language_policy is FaithfulLanguagePolicy.PRESERVE_SOURCE
                and self.target_language is not None
            )
            or (
                self.language_policy is FaithfulLanguagePolicy.TRANSLATE_TO_OUTPUT
                and not _text(self.target_language)
            )
            or not isinstance(self.model_binding, ModelExecutionBinding)
            or not _positive(self.max_request_bytes)
            or not _positive(self.section_input_byte_budget)
            or self.section_input_byte_budget >= self.max_request_bytes
            or not _positive(self.reserved_output_tokens)
            or self.reserved_output_tokens > self.model_binding.max_output_tokens
            or not isinstance(self.parser_limits, FaithfulEditionParserLimitsV1)
            or self.parser_limits.max_response_bytes > self.max_request_bytes
            or type(self.max_repair_attempts) is not int
            or not 0 <= self.max_repair_attempts <= 1
        ):
            raise invalid_contract("Faithful edition request is invalid")


@dataclass(frozen=True)
class FaithfulSectionRefV1:
    section_id: str
    ordinal: int
    start_segment_ordinal: int
    end_segment_ordinal_exclusive: int
    start_segment_id: str
    end_segment_id: str
    start_ms: int
    end_ms: int
    editable_segment_count: int
    estimated_input_tokens: int
    encoded_input_bytes: int
    segment_ids_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.section_id) is not str
            or _SECTION_ID.fullmatch(self.section_id) is None
            or not _nonnegative(self.ordinal)
            or not _nonnegative(self.start_segment_ordinal)
            or not _positive(self.end_segment_ordinal_exclusive)
            or self.end_segment_ordinal_exclusive <= self.start_segment_ordinal
            or type(self.start_segment_id) is not str
            or _SEGMENT_ID.fullmatch(self.start_segment_id) is None
            or type(self.end_segment_id) is not str
            or _SEGMENT_ID.fullmatch(self.end_segment_id) is None
            or not _nonnegative(self.start_ms)
            or not _positive(self.end_ms)
            or self.end_ms <= self.start_ms
            or not _positive(self.editable_segment_count)
            or not _positive(self.estimated_input_tokens)
            or not _positive(self.encoded_input_bytes)
            or type(self.segment_ids_sha256) is not str
            or _SHA256.fullmatch(self.segment_ids_sha256) is None
        ):
            raise invalid_contract("Faithful section reference is invalid")


@dataclass(frozen=True)
class FaithfulEditionPlanV1:
    schema_version: int
    recipe_id: str
    recipe_version: int
    quality_profile: CompilationQualityProfile
    transcript_sha256: str
    transcript_basis: TranscriptBasis
    source_language: str
    language_policy: FaithfulLanguagePolicy
    target_language: str | None
    model_binding_sha256: str
    stage_version: int
    prompt_version: int
    sections: tuple[FaithfulSectionRefV1, ...]
    excluded_segment_ids: tuple[str, ...]
    max_concurrency: int
    max_repair_attempts: int

    def __post_init__(self) -> None:
        try:
            sections = tuple(self.sections)
            excluded = tuple(self.excluded_segment_ids)
        except TypeError:
            raise invalid_contract("Faithful plan is invalid") from None
        if (
            self.schema_version != 1
            or not _text(self.recipe_id)
            or not _positive(self.recipe_version)
            or self.quality_profile is not CompilationQualityProfile.BALANCED
            or type(self.transcript_sha256) is not str
            or _SHA256.fullmatch(self.transcript_sha256) is None
            or not isinstance(self.transcript_basis, TranscriptBasis)
            or not _text(self.source_language)
            or not isinstance(self.language_policy, FaithfulLanguagePolicy)
            or (
                self.language_policy is FaithfulLanguagePolicy.PRESERVE_SOURCE
                and self.target_language is not None
            )
            or (
                self.language_policy is FaithfulLanguagePolicy.TRANSLATE_TO_OUTPUT
                and not _text(self.target_language)
            )
            or type(self.model_binding_sha256) is not str
            or _SHA256.fullmatch(self.model_binding_sha256) is None
            or not _positive(self.stage_version)
            or not _positive(self.prompt_version)
            or not sections
            or any(not isinstance(value, FaithfulSectionRefV1) for value in sections)
            or [value.ordinal for value in sections] != list(range(len(sections)))
            or any(
                previous.end_segment_ordinal_exclusive
                > current.start_segment_ordinal
                for previous, current in zip(sections, sections[1:])
            )
            or any(
                type(value) is not str or _SEGMENT_ID.fullmatch(value) is None
                for value in excluded
            )
            or len(excluded) != len(set(excluded))
            or not _positive(self.max_concurrency)
            or self.max_concurrency > len(sections)
            or type(self.max_repair_attempts) is not int
            or not 0 <= self.max_repair_attempts <= 1
        ):
            raise invalid_contract("Faithful plan is invalid")
        object.__setattr__(self, "sections", sections)
        object.__setattr__(self, "excluded_segment_ids", excluded)


@dataclass(frozen=True)
class FaithfulParagraphV1:
    paragraph_ordinal: int
    text: str
    source_segment_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_sourced_text(
            self.paragraph_ordinal, self.text, self.source_segment_ids, "paragraph"
        )
        object.__setattr__(self, "source_segment_ids", tuple(self.source_segment_ids))


@dataclass(frozen=True)
class FaithfulAuxiliaryTextV1:
    text: str
    source_segment_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_sourced_text(0, self.text, self.source_segment_ids, "summary")
        object.__setattr__(self, "source_segment_ids", tuple(self.source_segment_ids))


@dataclass(frozen=True)
class FaithfulKeyPointV1:
    key_point_ordinal: int
    text: str
    source_segment_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_sourced_text(
            self.key_point_ordinal, self.text, self.source_segment_ids, "key point"
        )
        object.__setattr__(self, "source_segment_ids", tuple(self.source_segment_ids))


@dataclass(frozen=True)
class FaithfulUncertaintyV1:
    uncertainty_ordinal: int
    category: FaithfulUncertaintyCategory
    description: str
    source_segment_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_sourced_text(
            self.uncertainty_ordinal,
            self.description,
            self.source_segment_ids,
            "uncertainty",
        )
        if not isinstance(self.category, FaithfulUncertaintyCategory):
            raise invalid_contract("Faithful uncertainty category is invalid")
        object.__setattr__(self, "source_segment_ids", tuple(self.source_segment_ids))


def _validate_sourced_text(
    ordinal: object,
    value: object,
    source_segment_ids: object,
    field_name: str,
) -> None:
    try:
        sources = tuple(source_segment_ids)  # type: ignore[arg-type]
    except TypeError:
        raise invalid_contract(f"Faithful {field_name} is invalid") from None
    if (
        not _nonnegative(ordinal)
        or not _text(value)
        or not sources
        or any(
            type(source) is not str or _SEGMENT_ID.fullmatch(source) is None
            for source in sources
        )
        or len(sources) != len(set(sources))
    ):
        raise invalid_contract(f"Faithful {field_name} is invalid")


@dataclass(frozen=True)
class FaithfulEditionSectionV1:
    section_id: str
    ordinal: int
    title: str
    start_ms: int
    end_ms: int
    paragraphs: tuple[FaithfulParagraphV1, ...]
    summary: FaithfulAuxiliaryTextV1
    key_points: tuple[FaithfulKeyPointV1, ...]
    uncertainties: tuple[FaithfulUncertaintyV1, ...]
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        try:
            paragraphs = tuple(self.paragraphs)
            key_points = tuple(self.key_points)
            uncertainties = tuple(self.uncertainties)
            warnings = tuple(self.warnings)
        except TypeError:
            raise invalid_contract("Faithful section is invalid") from None
        if (
            type(self.section_id) is not str
            or _SECTION_ID.fullmatch(self.section_id) is None
            or not _nonnegative(self.ordinal)
            or not _text(self.title)
            or not _nonnegative(self.start_ms)
            or not _positive(self.end_ms)
            or self.end_ms <= self.start_ms
            or not paragraphs
            or any(not isinstance(value, FaithfulParagraphV1) for value in paragraphs)
            or [value.paragraph_ordinal for value in paragraphs]
            != list(range(len(paragraphs)))
            or not isinstance(self.summary, FaithfulAuxiliaryTextV1)
            or any(not isinstance(value, FaithfulKeyPointV1) for value in key_points)
            or [value.key_point_ordinal for value in key_points]
            != list(range(len(key_points)))
            or any(
                not isinstance(value, FaithfulUncertaintyV1)
                for value in uncertainties
            )
            or [value.uncertainty_ordinal for value in uncertainties]
            != list(range(len(uncertainties)))
            or any(not _text(value) for value in warnings)
            or len(warnings) != len(set(warnings))
        ):
            raise invalid_contract("Faithful section is invalid")
        object.__setattr__(self, "paragraphs", paragraphs)
        object.__setattr__(self, "key_points", key_points)
        object.__setattr__(self, "uncertainties", uncertainties)
        object.__setattr__(self, "warnings", warnings)


__all__ = [
    "FaithfulAuxiliaryTextV1",
    "FaithfulEditionParserLimitsV1",
    "FaithfulEditionPlanV1",
    "FaithfulEditionRequestV1",
    "FaithfulEditionSectionV1",
    "FaithfulKeyPointV1",
    "FaithfulParagraphV1",
    "FaithfulSectionRefV1",
    "FaithfulUncertaintyCategory",
    "FaithfulUncertaintyV1",
    "invalid_contract",
]

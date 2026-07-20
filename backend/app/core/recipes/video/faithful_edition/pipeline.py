from __future__ import annotations

import json
import re
from dataclasses import asdict

from app.core.domain.ids import sha256_digest
from app.core.domain.transcript import transcript_sha256
from app.core.domain.video import TranscriptSegment
from app.core.errors import DomainError, ErrorCategory
from app.core.portable.jsonio import encode_json
from app.core.recipes.video.faithful_edition.contracts import (
    FaithfulAuxiliaryTextV1,
    FaithfulEditionParserLimitsV1,
    FaithfulEditionPlanV1,
    FaithfulEditionRequestV1,
    FaithfulEditionSectionV1,
    FaithfulKeyPointV1,
    FaithfulParagraphV1,
    FaithfulSectionRefV1,
    FaithfulUncertaintyCategory,
    FaithfulUncertaintyV1,
)


_EXCLUDED_MARKER = re.compile(
    r"^\s*(?:\[(?:music|noise)\]|\((?:music|noise)\)|(?:music|noise))\s*$",
    re.IGNORECASE,
)
_SECTION_FIELDS = frozenset(
    {
        "schema_version",
        "section_id",
        "section_ordinal",
        "title",
        "paragraphs",
        "summary",
        "key_points",
        "uncertainties",
        "warnings",
    }
)
_PARAGRAPH_FIELDS = frozenset(
    {"paragraph_ordinal", "text", "source_segment_ids"}
)
_SUMMARY_FIELDS = frozenset({"text", "source_segment_ids"})
_KEY_POINT_FIELDS = frozenset(
    {"key_point_ordinal", "text", "source_segment_ids"}
)
_UNCERTAINTY_FIELDS = frozenset(
    {"uncertainty_ordinal", "category", "description", "source_segment_ids"}
)


def _response_error(message: str) -> DomainError:
    return DomainError(
        "faithful_section_response_invalid",
        ErrorCategory.RECIPE_FAILED,
        message,
    )


def _binding_sha256(request: FaithfulEditionRequestV1) -> str:
    return sha256_digest(encode_json(asdict(request.model_binding)))


def _segment_payload(segment: TranscriptSegment) -> dict[str, object]:
    return {
        "end_ms": segment.end_ms,
        "segment_id": segment.segment_id,
        "start_ms": segment.start_ms,
        "text": segment.text,
    }


def _is_excluded(
    segment: TranscriptSegment,
    previous: TranscriptSegment | None,
) -> bool:
    if _EXCLUDED_MARKER.fullmatch(segment.text):
        return True
    return previous is not None and " ".join(segment.text.split()).casefold() == " ".join(
        previous.text.split()
    ).casefold()


def plan_faithful_edition(
    request: FaithfulEditionRequestV1,
) -> FaithfulEditionPlanV1:
    if not isinstance(request, FaithfulEditionRequestV1):
        raise DomainError(
            "faithful_edition_contract_invalid",
            ErrorCategory.INVALID_REQUEST,
            "Faithful planning requires the frozen request contract",
        )
    transcript = request.transcript
    excluded: list[str] = []
    eligible: list[tuple[int, TranscriptSegment, int]] = []
    previous: TranscriptSegment | None = None
    for ordinal, segment in enumerate(transcript.segments):
        encoded_bytes = len(encode_json(_segment_payload(segment)))
        if _is_excluded(segment, previous):
            excluded.append(segment.segment_id)
        else:
            if encoded_bytes > request.section_input_byte_budget:
                raise DomainError(
                    "faithful_section_budget_exceeded",
                    ErrorCategory.POLICY_DENIED,
                    "A transcript segment exceeds the faithful section input budget",
                )
            eligible.append((ordinal, segment, encoded_bytes))
        previous = segment
    if not eligible:
        raise DomainError(
            "faithful_transcript_empty",
            ErrorCategory.INVALID_REQUEST,
            "Faithful editing requires at least one editable transcript segment",
        )

    batches: list[list[tuple[int, TranscriptSegment, int]]] = []
    current: list[tuple[int, TranscriptSegment, int]] = []
    current_bytes = 0
    for value in eligible:
        if current and current_bytes + value[2] > request.section_input_byte_budget:
            batches.append(current)
            current = []
            current_bytes = 0
        current.append(value)
        current_bytes += value[2]
    batches.append(current)

    transcript_digest = transcript_sha256(transcript)
    sections: list[FaithfulSectionRefV1] = []
    for section_ordinal, batch in enumerate(batches):
        segment_ids = tuple(value[1].segment_id for value in batch)
        first_ordinal, first_segment, _ = batch[0]
        last_ordinal, last_segment, _ = batch[-1]
        segment_digest = sha256_digest(encode_json(list(segment_ids)))
        section_id = "fs_" + sha256_digest(
            encode_json(
                {
                    "ordinal": section_ordinal,
                    "recipe": [request.recipe_id, request.recipe_version],
                    "segment_ids_sha256": segment_digest,
                    "transcript_sha256": transcript_digest,
                }
            )
        )[7:]
        sections.append(
            FaithfulSectionRefV1(
                section_id=section_id,
                ordinal=section_ordinal,
                start_segment_ordinal=first_ordinal,
                end_segment_ordinal_exclusive=last_ordinal + 1,
                start_segment_id=first_segment.segment_id,
                end_segment_id=last_segment.segment_id,
                start_ms=first_segment.start_ms,
                end_ms=last_segment.end_ms,
                editable_segment_count=len(batch),
                estimated_input_tokens=max(1, sum(value[2] for value in batch)),
                encoded_input_bytes=sum(value[2] for value in batch),
                segment_ids_sha256=segment_digest,
            )
        )
    return FaithfulEditionPlanV1(
        schema_version=1,
        recipe_id=request.recipe_id,
        recipe_version=request.recipe_version,
        quality_profile=request.quality_profile,
        transcript_sha256=transcript_digest,
        transcript_basis=request.transcript_basis,
        source_language=request.source_language,
        language_policy=request.language_policy,
        target_language=request.target_language,
        model_binding_sha256=_binding_sha256(request),
        stage_version=1,
        prompt_version=1,
        sections=tuple(sections),
        excluded_segment_ids=tuple(excluded),
        max_concurrency=min(request.model_binding.max_concurrency, len(sections)),
        max_repair_attempts=request.max_repair_attempts,
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _sourced_ids(
    value: object,
    *,
    fields: frozenset[str],
    allowed: frozenset[str],
) -> tuple[str, ...]:
    if type(value) is not dict or set(value) != fields:
        raise _response_error("Faithful sourced text fields are invalid")
    source_ids = value["source_segment_ids"]
    if (
        type(source_ids) is not list
        or not source_ids
        or any(type(item) is not str or item not in allowed for item in source_ids)
        or len(source_ids) != len(set(source_ids))
    ):
        raise _response_error("Faithful sourced text references are invalid")
    return tuple(source_ids)


def parse_faithful_section(
    response_text: str,
    *,
    section_ref: FaithfulSectionRefV1,
    allowed_segment_ids: tuple[str, ...],
    limits: FaithfulEditionParserLimitsV1,
) -> FaithfulEditionSectionV1:
    if (
        type(response_text) is not str
        or not isinstance(section_ref, FaithfulSectionRefV1)
        or not isinstance(limits, FaithfulEditionParserLimitsV1)
    ):
        raise _response_error("Faithful section parser context is invalid")
    if len(response_text.encode("utf-8")) > limits.max_response_bytes:
        raise _response_error("Faithful section response exceeds its byte budget")
    try:
        value = json.loads(
            response_text,
            object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except MemoryError:
        raise
    except (RecursionError, TypeError, ValueError):
        raise _response_error("Faithful section response is not strict JSON") from None
    allowed = frozenset(allowed_segment_ids)
    if (
        type(value) is not dict
        or set(value) != _SECTION_FIELDS
        or value["schema_version"] != 1
        or value["section_id"] != section_ref.section_id
        or value["section_ordinal"] != section_ref.ordinal
        or type(value["title"]) is not str
        or not value["title"].strip()
        or len(value["title"]) > limits.max_title_characters
    ):
        raise _response_error("Faithful section identity is invalid")

    paragraphs_value = value["paragraphs"]
    if (
        type(paragraphs_value) is not list
        or not 1 <= len(paragraphs_value) <= limits.max_paragraphs
    ):
        raise _response_error("Faithful paragraphs are invalid")
    paragraphs: list[FaithfulParagraphV1] = []
    body_ids: list[str] = []
    for ordinal, paragraph in enumerate(paragraphs_value):
        source_ids = _sourced_ids(
            paragraph, fields=_PARAGRAPH_FIELDS, allowed=allowed
        )
        if (
            paragraph["paragraph_ordinal"] != ordinal
            or type(paragraph["text"]) is not str
            or not paragraph["text"].strip()
            or len(paragraph["text"]) > limits.max_paragraph_characters
            or len(source_ids) > limits.max_segment_refs_per_paragraph
        ):
            raise _response_error("Faithful paragraph is invalid")
        body_ids.extend(source_ids)
        paragraphs.append(
            FaithfulParagraphV1(ordinal, paragraph["text"], source_ids)
        )
    if body_ids != list(allowed_segment_ids) or len(body_ids) != len(set(body_ids)):
        raise _response_error(
            "Faithful paragraph mapping must cover the section exactly in order"
        )

    summary_value = value["summary"]
    summary_ids = _sourced_ids(summary_value, fields=_SUMMARY_FIELDS, allowed=allowed)
    if (
        type(summary_value["text"]) is not str
        or not summary_value["text"].strip()
        or len(summary_value["text"]) > limits.max_auxiliary_text_characters
    ):
        raise _response_error("Faithful summary is invalid")
    summary = FaithfulAuxiliaryTextV1(summary_value["text"], summary_ids)

    key_points_value = value["key_points"]
    if type(key_points_value) is not list or len(key_points_value) > limits.max_key_points:
        raise _response_error("Faithful key points are invalid")
    key_points: list[FaithfulKeyPointV1] = []
    for ordinal, key_point in enumerate(key_points_value):
        source_ids = _sourced_ids(
            key_point, fields=_KEY_POINT_FIELDS, allowed=allowed
        )
        if (
            key_point["key_point_ordinal"] != ordinal
            or type(key_point["text"]) is not str
            or not key_point["text"].strip()
            or len(key_point["text"]) > limits.max_auxiliary_text_characters
        ):
            raise _response_error("Faithful key point is invalid")
        key_points.append(FaithfulKeyPointV1(ordinal, key_point["text"], source_ids))

    uncertainty_values = value["uncertainties"]
    if (
        type(uncertainty_values) is not list
        or len(uncertainty_values) > limits.max_uncertainties
    ):
        raise _response_error("Faithful uncertainties are invalid")
    uncertainties: list[FaithfulUncertaintyV1] = []
    for ordinal, uncertainty in enumerate(uncertainty_values):
        source_ids = _sourced_ids(
            uncertainty, fields=_UNCERTAINTY_FIELDS, allowed=allowed
        )
        try:
            category = FaithfulUncertaintyCategory(uncertainty["category"])
        except (TypeError, ValueError):
            raise _response_error("Faithful uncertainty category is invalid") from None
        if (
            uncertainty["uncertainty_ordinal"] != ordinal
            or type(uncertainty["description"]) is not str
            or not uncertainty["description"].strip()
            or len(uncertainty["description"])
            > limits.max_auxiliary_text_characters
        ):
            raise _response_error("Faithful uncertainty is invalid")
        uncertainties.append(
            FaithfulUncertaintyV1(
                ordinal, category, uncertainty["description"], source_ids
            )
        )

    warnings = value["warnings"]
    if (
        type(warnings) is not list
        or len(warnings) > limits.max_warnings
        or any(type(item) is not str or not item.strip() for item in warnings)
        or len(warnings) != len(set(warnings))
    ):
        raise _response_error("Faithful warnings are invalid")
    return FaithfulEditionSectionV1(
        section_id=section_ref.section_id,
        ordinal=section_ref.ordinal,
        title=value["title"],
        start_ms=section_ref.start_ms,
        end_ms=section_ref.end_ms,
        paragraphs=tuple(paragraphs),
        summary=summary,
        key_points=tuple(key_points),
        uncertainties=tuple(uncertainties),
        warnings=tuple(warnings),
    )


__all__ = ["parse_faithful_section", "plan_faithful_edition"]

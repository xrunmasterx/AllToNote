from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import asdict

from app.core.domain.ids import sha256_digest
from app.core.domain.transcript import transcript_sha256
from app.core.domain.video import TranscriptSegment
from app.core.errors import DomainError, ErrorCategory
from app.core.portable.jsonio import encode_json
from app.core.recipes.video.compilation.contracts import (
    ChunkKnowledgeMapV1,
    ComposedKnowledgeDraftV1,
    ComposerParserLimitsV1,
    CompilationEditorialMode,
    CompilationQualityProfile,
    CompilationTopology,
    CompilationWritingMode,
    CompositionCoverageLedgerV1,
    ConsolidatedKnowledgeItemV1,
    ConsolidatedKnowledgeV1,
    CoverageInputKind,
    CoverageOmissionV1,
    KnowledgeImportance,
    KnowledgeItemKind,
    KnowledgeItemV1,
    KnowledgeMapParserLimitsV1,
    TranscriptCheckStatus,
    TranscriptChunkRefV1,
    TranscriptConfidenceSummaryV1,
    TranscriptQualityAssessmentV1,
    TranscriptQualityCheckV1,
    TranscriptQualityInputV1,
    TranscriptQualityStatus,
    VideoCompilationPlanV1,
    VideoCompilationPlanningRequestV1,
)
from app.core.recipes.video.citation_parser import parse_model_output


_ABNORMAL_GAP_MS = 30_000
_ABNORMAL_SEGMENT_MS = 120_000
_ABNORMAL_SEGMENT_BYTES = 64 * 1024
_LOW_CONFIDENCE = 0.5
_TOKEN_ESTIMATOR_ID = "utf8-byte-upper-bound-v1"
_SEGMENT_ID = re.compile(r"seg_[0-9]{6,}\Z")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_KNOWLEDGE_MAP_JSON_DEPTH = 4
_KNOWLEDGE_MAP_FIELDS = frozenset(
    {"schema_version", "chunk_ordinal", "items", "term_candidates", "warnings"}
)
_KNOWLEDGE_ITEM_FIELDS = frozenset(
    {
        "item_ordinal",
        "kind",
        "title",
        "statement",
        "importance",
        "source_segment_ids",
    }
)
_COMPOSER_FIELDS = frozenset(
    {"schema_version", "markdown", "covered_input_ids", "omissions", "warnings"}
)
_OMISSION_FIELDS = frozenset({"input_id", "reason"})
_CONSOLIDATION_FIELDS = frozenset(
    {
        "schema_version",
        "batch_ordinal",
        "items",
        "term_candidates",
        "warnings",
        "lineage",
        "omissions",
    }
)
_LINEAGE_FIELDS = frozenset({"output_item_ordinal", "merged_from"})
_KNOWLEDGE_ITEM_ID = re.compile(r"ki_[0-9a-f]{64}\Z")


def _level(
    value: float,
    warning: float,
    failure: float,
    *,
    lower_is_worse: bool,
) -> TranscriptCheckStatus:
    if lower_is_worse:
        if value < failure:
            return TranscriptCheckStatus.FAIL
        if value < warning:
            return TranscriptCheckStatus.WARNING
    else:
        if value >= failure:
            return TranscriptCheckStatus.FAIL
        if value >= warning:
            return TranscriptCheckStatus.WARNING
    return TranscriptCheckStatus.PASS


def assess_transcript_quality(
    value: TranscriptQualityInputV1,
) -> TranscriptQualityAssessmentV1:
    if not isinstance(value, TranscriptQualityInputV1):
        raise DomainError(
            "transcript_quality_input_invalid",
            ErrorCategory.INVALID_REQUEST,
            "Transcript quality assessment requires the v1 input contract",
        )
    transcript = value.transcript
    segments = transcript.segments
    duplicate_count = 0
    overlap_count = 0
    gap_count = 0
    abnormal_count = 0
    previous: TranscriptSegment | None = None
    previous_text: str | None = None
    running_end_ms = 0
    transcript_end = 0
    covered_until_ms = 0
    covered_duration_ms = 0
    text_character_count = 0
    corrupt_character_count = 0
    for current in segments:
        normalized_text = " ".join(current.text.casefold().split())
        if (
            previous is not None
            and normalized_text == previous_text
            and current.start_ms < previous.end_ms
        ):
            duplicate_count += 1
        previous_text = normalized_text
        current_bytes = len(current.text.encode("utf-8"))
        text_character_count += len(current.text)
        corrupt_character_count += sum(
            character == "\ufffd"
            or (ord(character) < 32 and character not in "\t\n\r")
            or ord(character) == 127
            for character in current.text
        )
        if value.source_duration_ms is not None:
            clipped_start = min(current.start_ms, value.source_duration_ms)
            clipped_end = min(current.end_ms, value.source_duration_ms)
            if clipped_end > max(clipped_start, covered_until_ms):
                covered_duration_ms += clipped_end - max(
                    clipped_start, covered_until_ms
                )
            covered_until_ms = max(covered_until_ms, clipped_end)
        if (
            current.end_ms - current.start_ms > _ABNORMAL_SEGMENT_MS
            or current_bytes > _ABNORMAL_SEGMENT_BYTES
        ):
            abnormal_count += 1
        if previous is None:
            previous = current
            running_end_ms = current.end_ms
            transcript_end = current.end_ms
            continue
        overlap_ms = running_end_ms - current.start_ms
        if overlap_ms > 0 and (
            overlap_ms >= 2_000
            or overlap_ms * 2 >= current.end_ms - current.start_ms
        ):
            overlap_count += 1
        if current.start_ms - running_end_ms >= _ABNORMAL_GAP_MS:
            gap_count += 1
        previous = current
        running_end_ms = max(running_end_ms, current.end_ms)
        transcript_end = running_end_ms

    segment_count = len(segments)
    transcript_start = segments[0].start_ms
    coverage = None
    if value.source_duration_ms is not None:
        coverage = round(
            covered_duration_ms / value.source_duration_ms,
            6,
        )
    duplicate_ratio = round(duplicate_count / segment_count, 6)
    overlap_ratio = overlap_count / max(1, segment_count - 1)

    checks = [
        TranscriptQualityCheckV1(
            "normalization-observations",
            TranscriptCheckStatus.WARNING
            if value.empty_segment_count or value.out_of_order_count
            else TranscriptCheckStatus.PASS,
        ),
        TranscriptQualityCheckV1(
            "coverage",
            TranscriptCheckStatus.NOT_APPLICABLE
            if coverage is None
            else _level(coverage, 0.85, 0.5, lower_is_worse=True),
        ),
        TranscriptQualityCheckV1(
            "duplicates",
            _level(duplicate_ratio, 0.1, 0.5, lower_is_worse=False),
        ),
        TranscriptQualityCheckV1(
            "overlap",
            TranscriptCheckStatus.WARNING
            if overlap_count
            else TranscriptCheckStatus.PASS,
        ),
        TranscriptQualityCheckV1(
            "abnormal-gaps",
            TranscriptCheckStatus.WARNING if gap_count else TranscriptCheckStatus.PASS,
        ),
        TranscriptQualityCheckV1(
            "abnormal-segments",
            TranscriptCheckStatus.WARNING
            if abnormal_count
            else TranscriptCheckStatus.PASS,
        ),
        TranscriptQualityCheckV1(
            "languages",
            TranscriptCheckStatus.WARNING
            if len(value.detected_languages) > 1
            else TranscriptCheckStatus.PASS,
        ),
        TranscriptQualityCheckV1(
            "text-integrity",
            TranscriptCheckStatus.PASS
            if corrupt_character_count == 0
            else TranscriptCheckStatus.FAIL
            if corrupt_character_count
            >= max(3, math.ceil(text_character_count * 0.01))
            else TranscriptCheckStatus.WARNING,
        ),
    ]
    confidence_summary = None
    if value.segment_confidences is None:
        checks.append(
            TranscriptQualityCheckV1(
                "confidence", TranscriptCheckStatus.NOT_APPLICABLE
            )
        )
    else:
        mean = round(sum(value.segment_confidences) / segment_count, 6)
        confidence_summary = TranscriptConfidenceSummaryV1(
            minimum=min(value.segment_confidences),
            mean=mean,
            low_confidence_count=sum(
                confidence < _LOW_CONFIDENCE
                for confidence in value.segment_confidences
            ),
        )
        checks.append(
            TranscriptQualityCheckV1(
                "confidence",
                TranscriptCheckStatus.WARNING
                if mean < _LOW_CONFIDENCE
                else TranscriptCheckStatus.PASS,
            )
        )

    statuses = {check.status for check in checks}
    status = (
        TranscriptQualityStatus.FAIL
        if TranscriptCheckStatus.FAIL in statuses
        else TranscriptQualityStatus.WARNING
        if TranscriptCheckStatus.WARNING in statuses
        else TranscriptQualityStatus.PASS
    )
    warnings = tuple(
        check.check_id
        for check in checks
        if check.status in {TranscriptCheckStatus.WARNING, TranscriptCheckStatus.FAIL}
    )
    return TranscriptQualityAssessmentV1(
        schema_version=1,
        transcript_sha256=transcript_sha256(transcript),
        status=status,
        transcript_basis=value.transcript_basis,
        source_language=transcript.language,
        detected_languages=value.detected_languages,
        duration_known=value.source_duration_ms is not None,
        source_duration_ms=value.source_duration_ms,
        transcript_start_ms=transcript_start,
        transcript_end_ms=transcript_end,
        coverage_ratio=coverage,
        duplicate_ratio=duplicate_ratio,
        empty_segment_count=value.empty_segment_count,
        out_of_order_count=value.out_of_order_count,
        overlap_issue_count=overlap_count,
        abnormal_gap_count=gap_count,
        abnormal_segment_count=abnormal_count,
        confidence_available=value.segment_confidences is not None,
        confidence_summary=confidence_summary,
        checks=tuple(checks),
        warnings=warnings,
    )


def estimate_segment_input_cost(segment: TranscriptSegment) -> tuple[int, int]:
    payload = encode_json(
        {
            "end_ms": segment.end_ms,
            "segment_id": segment.segment_id,
            "start_ms": segment.start_ms,
            "text": segment.text,
        }
    )
    return len(payload), len(payload)


def _knowledge_map_error(code: str, message: str) -> DomainError:
    return DomainError(code, ErrorCategory.RECIPE_FAILED, message)


def _check_json_depth(value: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in value:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > _MAX_KNOWLEDGE_MAP_JSON_DEPTH:
                raise _knowledge_map_error(
                    "knowledge_map_response_budget_exceeded",
                    "Knowledge Map JSON nesting exceeds its fixed budget",
                )
        elif character in "]}":
            depth -= 1


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _bounded_model_text(value: object, limit: int) -> str:
    if type(value) is not str:
        raise _knowledge_map_error(
            "knowledge_map_response_invalid",
            "Knowledge Map text fields must be strings",
        )
    normalized = unicodedata.normalize("NFC", value.strip())
    try:
        normalized.encode("utf-8")
    except UnicodeError:
        raise _knowledge_map_error(
            "knowledge_map_response_invalid",
            "Knowledge Map text is not valid UTF-8",
        ) from None
    if not normalized:
        raise _knowledge_map_error(
            "knowledge_map_response_invalid",
            "Knowledge Map text fields must not be empty",
        )
    if len(normalized) > limit:
        raise _knowledge_map_error(
            "knowledge_map_response_budget_exceeded",
            "Knowledge Map text exceeds its frozen budget",
        )
    return normalized


def parse_chunk_knowledge_map(
    response_text: str,
    *,
    stage_id: str,
    stage_version: int,
    transcript_sha256: str,
    chunk_ordinal: int,
    chunk_sha256: str,
    allowed_segment_ids: tuple[str, ...],
    limits: KnowledgeMapParserLimitsV1,
) -> ChunkKnowledgeMapV1:
    try:
        allowed_ids = tuple(allowed_segment_ids)
    except TypeError:
        allowed_ids = ()
    if (
        type(response_text) is not str
        or type(stage_id) is not str
        or not stage_id.strip()
        or type(stage_version) is not int
        or stage_version < 1
        or type(transcript_sha256) is not str
        or _SHA256.fullmatch(transcript_sha256) is None
        or type(chunk_ordinal) is not int
        or chunk_ordinal < 0
        or type(chunk_sha256) is not str
        or _SHA256.fullmatch(chunk_sha256) is None
        or not allowed_ids
        or any(
            type(segment_id) is not str
            or _SEGMENT_ID.fullmatch(segment_id) is None
            for segment_id in allowed_ids
        )
        or len(allowed_ids) != len(set(allowed_ids))
        or not isinstance(limits, KnowledgeMapParserLimitsV1)
    ):
        raise DomainError(
            "knowledge_map_parse_context_invalid",
            ErrorCategory.INVALID_REQUEST,
            "Knowledge Map parsing requires a valid frozen context",
        )
    if len(response_text.encode("utf-8")) > limits.max_response_bytes:
        raise _knowledge_map_error(
            "knowledge_map_response_budget_exceeded",
            "Knowledge Map response exceeds its frozen byte budget",
        )
    try:
        response_bytes = len(response_text.encode("utf-8"))
    except UnicodeError:
        raise _knowledge_map_error(
            "knowledge_map_response_invalid",
            "Knowledge Map response is not valid UTF-8",
        ) from None
    if response_bytes > limits.max_response_bytes:
        raise _knowledge_map_error(
            "knowledge_map_response_budget_exceeded",
            "Knowledge Map response exceeds its frozen byte budget",
        )
    _check_json_depth(response_text)
    try:
        value = json.loads(
            response_text,
            object_pairs_hook=_unique_json_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except MemoryError:
        raise
    except (RecursionError, TypeError, ValueError):
        raise _knowledge_map_error(
            "knowledge_map_response_invalid",
            "Knowledge Map response is not valid strict JSON",
        ) from None
    if type(value) is not dict or set(value) != _KNOWLEDGE_MAP_FIELDS:
        raise _knowledge_map_error(
            "knowledge_map_response_invalid",
            "Knowledge Map response fields are invalid",
        )
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise _knowledge_map_error(
            "knowledge_map_response_invalid", "Knowledge Map schema is unsupported"
        )
    if type(value["chunk_ordinal"]) is not int:
        raise _knowledge_map_error(
            "knowledge_map_response_invalid", "Knowledge Map chunk ordinal is invalid"
        )
    if value["chunk_ordinal"] != chunk_ordinal:
        raise _knowledge_map_error(
            "knowledge_map_chunk_mismatch",
            "Knowledge Map response belongs to a different chunk",
        )
    raw_items = value["items"]
    raw_terms = value["term_candidates"]
    raw_warnings = value["warnings"]
    if type(raw_items) is not list or not raw_items:
        raise _knowledge_map_error(
            "knowledge_map_response_invalid", "Knowledge Map items are invalid"
        )
    if type(raw_terms) is not list or type(raw_warnings) is not list:
        raise _knowledge_map_error(
            "knowledge_map_response_invalid", "Knowledge Map arrays are invalid"
        )
    if (
        len(raw_items) > limits.max_items
        or len(raw_terms) > limits.max_term_candidates
        or len(raw_warnings) > limits.max_warnings
    ):
        raise _knowledge_map_error(
            "knowledge_map_response_budget_exceeded",
            "Knowledge Map arrays exceed their frozen budgets",
        )

    allowed_order = {segment_id: ordinal for ordinal, segment_id in enumerate(allowed_ids)}
    raw_item_ordinals = [
        raw_item.get("item_ordinal")
        for raw_item in raw_items
        if type(raw_item) is dict
    ]
    zero_based_ordinals = list(range(len(raw_items)))
    one_based_ordinals = list(range(1, len(raw_items) + 1))
    if any(type(value) is not int for value in raw_item_ordinals) or (
        raw_item_ordinals not in (zero_based_ordinals, one_based_ordinals)
    ):
        raise _knowledge_map_error(
            "knowledge_map_response_invalid",
            "Knowledge Map item ordinals are invalid",
        )
    items: list[KnowledgeItemV1] = []
    for item_ordinal, raw_item in enumerate(raw_items):
        if type(raw_item) is not dict or set(raw_item) != _KNOWLEDGE_ITEM_FIELDS:
            raise _knowledge_map_error(
                "knowledge_map_response_invalid", "Knowledge Map item fields are invalid"
            )
        try:
            kind = KnowledgeItemKind(raw_item["kind"])
            importance = KnowledgeImportance(raw_item["importance"])
        except (TypeError, ValueError):
            raise _knowledge_map_error(
                "knowledge_map_response_invalid", "Knowledge Map taxonomy is invalid"
            ) from None
        title = _bounded_model_text(raw_item["title"], limits.max_title_characters)
        statement = _bounded_model_text(
            raw_item["statement"], limits.max_statement_characters
        )
        raw_source_ids = raw_item["source_segment_ids"]
        if type(raw_source_ids) is not list or not raw_source_ids:
            raise _knowledge_map_error(
                "knowledge_map_response_invalid",
                "Knowledge Map item must cite source segments",
            )
        if len(raw_source_ids) > limits.max_segment_refs_per_item:
            raise _knowledge_map_error(
                "knowledge_map_response_budget_exceeded",
                "Knowledge Map segment references exceed their frozen budget",
            )
        if any(type(segment_id) is not str for segment_id in raw_source_ids):
            raise _knowledge_map_error(
                "knowledge_map_response_invalid",
                "Knowledge Map segment references are invalid",
            )
        if len(raw_source_ids) != len(set(raw_source_ids)):
            raise _knowledge_map_error(
                "knowledge_map_segment_duplicate",
                "Knowledge Map item repeats a source segment",
            )
        unknown_ids = set(raw_source_ids).difference(allowed_order)
        if unknown_ids:
            raise _knowledge_map_error(
                "knowledge_map_segment_unknown",
                "Knowledge Map item cites a segment outside its chunk",
            )
        source_ids = tuple(sorted(raw_source_ids, key=allowed_order.__getitem__))
        content = {
            "importance": importance.value,
            "kind": kind.value,
            "source_segment_ids": list(source_ids),
            "statement": statement,
            "title": title,
        }
        content_sha256 = sha256_digest(encode_json(content))
        identity = sha256_digest(
            encode_json(
                {
                    "chunk_ordinal": chunk_ordinal,
                    "content_sha256": content_sha256,
                    "item_ordinal": item_ordinal,
                    "stage_id": stage_id,
                    "stage_version": stage_version,
                    "transcript_sha256": transcript_sha256,
                }
            )
        )
        items.append(
            KnowledgeItemV1(
                knowledge_item_id=f"ki_{identity[7:]}",
                kind=kind,
                title=title,
                statement=statement,
                importance=importance,
                source_segment_ids=source_ids,
            )
        )

    terms = tuple(
        _bounded_model_text(term, limits.max_term_characters) for term in raw_terms
    )
    warnings = tuple(
        _bounded_model_text(warning, limits.max_warning_characters)
        for warning in raw_warnings
    )
    if len(terms) != len(set(terms)) or len(warnings) != len(set(warnings)):
        raise _knowledge_map_error(
            "knowledge_map_response_invalid",
            "Knowledge Map terms and warnings must be unique",
        )
    return ChunkKnowledgeMapV1(
        schema_version=1,
        chunk_ordinal=chunk_ordinal,
        chunk_sha256=chunk_sha256,
        items=tuple(items),
        term_candidates=terms,
        warnings=warnings,
    )


def _decode_recipe_object(
    response_text: str,
    *,
    max_response_bytes: int,
    expected_fields: frozenset[str],
) -> dict[str, object]:
    if type(response_text) is not str:
        raise _knowledge_map_error(
            "knowledge_recipe_response_invalid",
            "Knowledge recipe response must be text",
        )
    if len(response_text) > max_response_bytes:
        raise _knowledge_map_error(
            "knowledge_recipe_response_budget_exceeded",
            "Knowledge recipe response exceeds its byte budget",
        )
    try:
        if len(response_text.encode("utf-8")) > max_response_bytes:
            raise _knowledge_map_error(
                "knowledge_recipe_response_budget_exceeded",
                "Knowledge recipe response exceeds its byte budget",
            )
    except UnicodeError:
        raise _knowledge_map_error(
            "knowledge_recipe_response_invalid",
            "Knowledge recipe response is not valid UTF-8",
        ) from None
    _check_json_depth(response_text)
    try:
        value = json.loads(
            response_text,
            object_pairs_hook=_unique_json_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except MemoryError:
        raise
    except (RecursionError, TypeError, ValueError):
        raise _knowledge_map_error(
            "knowledge_recipe_response_invalid",
            "Knowledge recipe response is not valid strict JSON",
        ) from None
    if type(value) is not dict or set(value) != expected_fields:
        raise _knowledge_map_error(
            "knowledge_recipe_response_invalid",
            "Knowledge recipe response fields are invalid",
        )
    return value


def _parse_omissions(
    raw_omissions: object,
    *,
    allowed_ids: tuple[str, ...],
    limits: ComposerParserLimitsV1,
) -> tuple[CoverageOmissionV1, ...]:
    if type(raw_omissions) is not list:
        raise _knowledge_map_error(
            "knowledge_coverage_invalid", "Coverage omissions must be an array"
        )
    if len(raw_omissions) > limits.max_omissions:
        raise _knowledge_map_error(
            "knowledge_recipe_response_budget_exceeded",
            "Coverage omissions exceed their frozen budget",
        )
    allowed = frozenset(allowed_ids)
    omissions: list[CoverageOmissionV1] = []
    for raw_omission in raw_omissions:
        if type(raw_omission) is not dict or set(raw_omission) != _OMISSION_FIELDS:
            raise _knowledge_map_error(
                "knowledge_coverage_invalid", "Coverage omission fields are invalid"
            )
        input_id = raw_omission["input_id"]
        if type(input_id) is not str or input_id not in allowed:
            raise _knowledge_map_error(
                "knowledge_coverage_invalid", "Coverage omission input is unknown"
            )
        reason = _bounded_model_text(
            raw_omission["reason"], limits.max_omission_reason_characters
        )
        omissions.append(CoverageOmissionV1(input_id, reason))
    if len({value.input_id for value in omissions}) != len(omissions):
        raise _knowledge_map_error(
            "knowledge_coverage_invalid", "Coverage omissions must be unique"
        )
    return tuple(omissions)


def parse_composed_knowledge_draft(
    response_text: str,
    *,
    input_kind: CoverageInputKind,
    allowed_input_ids: tuple[str, ...],
    allowed_segment_ids: tuple[str, ...],
    allow_screenshots: bool,
    limits: ComposerParserLimitsV1,
) -> ComposedKnowledgeDraftV1:
    try:
        input_ids = tuple(allowed_input_ids)
        segment_ids = tuple(allowed_segment_ids)
    except TypeError:
        input_ids = ()
        segment_ids = ()
    if (
        not isinstance(input_kind, CoverageInputKind)
        or not input_ids
        or any(type(input_id) is not str or not input_id for input_id in input_ids)
        or len(input_ids) != len(set(input_ids))
        or not segment_ids
        or any(
            type(segment_id) is not str or _SEGMENT_ID.fullmatch(segment_id) is None
            for segment_id in segment_ids
        )
        or len(segment_ids) != len(set(segment_ids))
        or type(allow_screenshots) is not bool
        or not isinstance(limits, ComposerParserLimitsV1)
    ):
        raise DomainError(
            "knowledge_composer_context_invalid",
            ErrorCategory.INVALID_REQUEST,
            "Global Composer requires a valid frozen context",
        )
    value = _decode_recipe_object(
        response_text,
        max_response_bytes=limits.max_response_bytes,
        expected_fields=_COMPOSER_FIELDS,
    )
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise _knowledge_map_error(
            "knowledge_composer_response_invalid", "Composer schema is unsupported"
        )
    markdown = _bounded_model_text(
        value["markdown"], limits.max_markdown_characters
    )
    raw_covered = value["covered_input_ids"]
    if type(raw_covered) is not list or any(
        type(input_id) is not str for input_id in raw_covered
    ):
        raise _knowledge_map_error(
            "knowledge_coverage_invalid", "Covered inputs are invalid"
        )
    if len(raw_covered) > limits.max_coverage_items:
        raise _knowledge_map_error(
            "knowledge_recipe_response_budget_exceeded",
            "Covered inputs exceed their frozen budget",
        )
    if len(raw_covered) != len(set(raw_covered)):
        raise _knowledge_map_error(
            "knowledge_coverage_invalid", "Covered inputs must be unique"
        )
    allowed_order = {input_id: ordinal for ordinal, input_id in enumerate(input_ids)}
    if set(raw_covered).difference(allowed_order):
        raise _knowledge_map_error(
            "knowledge_coverage_invalid", "Composer covered an unknown input"
        )
    omissions = _parse_omissions(
        value["omissions"], allowed_ids=input_ids, limits=limits
    )
    omitted_ids = {omission.input_id for omission in omissions}
    if set(raw_covered).intersection(omitted_ids) or set(raw_covered).union(
        omitted_ids
    ) != set(input_ids):
        raise _knowledge_map_error(
            "knowledge_coverage_not_closed",
            "Every Composer input must be covered or explicitly omitted exactly once",
        )
    raw_warnings = value["warnings"]
    if type(raw_warnings) is not list:
        raise _knowledge_map_error(
            "knowledge_composer_response_invalid", "Composer warnings are invalid"
        )
    if len(raw_warnings) > limits.max_warnings:
        raise _knowledge_map_error(
            "knowledge_recipe_response_budget_exceeded",
            "Composer warnings exceed their frozen budget",
        )
    warnings = tuple(
        _bounded_model_text(warning, limits.max_warning_characters)
        for warning in raw_warnings
    )
    if len(warnings) != len(set(warnings)):
        raise _knowledge_map_error(
            "knowledge_composer_response_invalid", "Composer warnings must be unique"
        )
    parsed_markdown = parse_model_output(
        markdown,
        known_segment_ids=segment_ids,
        allow_screenshots=allow_screenshots,
    )
    covered = tuple(sorted(raw_covered, key=allowed_order.__getitem__))
    omission_order = {value: index for index, value in enumerate(input_ids)}
    ordered_omissions = tuple(
        sorted(omissions, key=lambda value: omission_order[value.input_id])
    )
    return ComposedKnowledgeDraftV1(
        markdown=parsed_markdown.markdown,
        cited_segment_ids=parsed_markdown.cited_segment_ids,
        screenshot_requests=parsed_markdown.screenshot_requests,
        coverage=CompositionCoverageLedgerV1(
            input_kind=input_kind,
            covered_input_ids=covered,
            omissions=ordered_omissions,
        ),
        warnings=warnings,
    )


def parse_consolidated_knowledge(
    response_text: str,
    *,
    stage_id: str,
    stage_version: int,
    transcript_sha256: str,
    batch_ordinal: int,
    batch_sha256: str,
    input_items: tuple[KnowledgeItemV1, ...],
    item_limits: KnowledgeMapParserLimitsV1,
    coverage_limits: ComposerParserLimitsV1,
) -> ConsolidatedKnowledgeV1:
    try:
        source_items = tuple(input_items)
    except TypeError:
        source_items = ()
    valid_source_items = bool(source_items) and all(
        isinstance(value, KnowledgeItemV1) for value in source_items
    )
    input_ids = (
        tuple(item.knowledge_item_id for item in source_items)
        if valid_source_items
        else ()
    )
    segment_ids = (
        tuple(
            dict.fromkeys(
                segment_id
                for item in source_items
                for segment_id in item.source_segment_ids
            )
        )
        if valid_source_items
        else ()
    )
    if (
        type(stage_id) is not str
        or not stage_id.strip()
        or type(stage_version) is not int
        or stage_version < 1
        or type(transcript_sha256) is not str
        or _SHA256.fullmatch(transcript_sha256) is None
        or type(batch_ordinal) is not int
        or batch_ordinal < 0
        or type(batch_sha256) is not str
        or _SHA256.fullmatch(batch_sha256) is None
        or not input_ids
        or not valid_source_items
        or len(input_ids) != len(set(input_ids))
        or any(
            type(value) is not str or _KNOWLEDGE_ITEM_ID.fullmatch(value) is None
            for value in input_ids
        )
        or not segment_ids
        or len(segment_ids) != len(set(segment_ids))
        or any(
            type(value) is not str or _SEGMENT_ID.fullmatch(value) is None
            for value in segment_ids
        )
        or not isinstance(item_limits, KnowledgeMapParserLimitsV1)
        or not isinstance(coverage_limits, ComposerParserLimitsV1)
    ):
        raise DomainError(
            "knowledge_consolidation_context_invalid",
            ErrorCategory.INVALID_REQUEST,
            "Knowledge consolidation requires valid input item identities",
        )
    value = _decode_recipe_object(
        response_text,
        max_response_bytes=min(
            item_limits.max_response_bytes, coverage_limits.max_response_bytes
        ),
        expected_fields=_CONSOLIDATION_FIELDS,
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or type(value["batch_ordinal"]) is not int
    ):
        raise _knowledge_map_error(
            "knowledge_consolidation_response_invalid",
            "Knowledge consolidation schema is invalid",
        )
    if value["batch_ordinal"] != batch_ordinal:
        raise _knowledge_map_error(
            "knowledge_consolidation_batch_mismatch",
            "Knowledge consolidation response belongs to another batch",
        )
    subset_response = encode_json(
        {
            "chunk_ordinal": batch_ordinal,
            "items": value["items"],
            "schema_version": 1,
            "term_candidates": value["term_candidates"],
            "warnings": value["warnings"],
        }
    ).decode("utf-8").rstrip("\n")
    parsed_map = parse_chunk_knowledge_map(
        subset_response,
        stage_id=stage_id,
        stage_version=stage_version,
        transcript_sha256=transcript_sha256,
        chunk_ordinal=batch_ordinal,
        chunk_sha256=batch_sha256,
        allowed_segment_ids=segment_ids,
        limits=item_limits,
    )
    raw_lineage = value["lineage"]
    if type(raw_lineage) is not list or len(raw_lineage) != len(parsed_map.items):
        raise _knowledge_map_error(
            "knowledge_consolidation_coverage_invalid",
            "Consolidated item lineage is invalid",
        )
    if len(input_ids) > coverage_limits.max_coverage_items:
        raise _knowledge_map_error(
            "knowledge_recipe_response_budget_exceeded",
            "Consolidation inputs exceed their frozen coverage budget",
        )
    allowed = frozenset(input_ids)
    consolidated_items: list[ConsolidatedKnowledgeItemV1] = []
    claimed: list[str] = []
    for ordinal, raw_lineage_item in enumerate(raw_lineage):
        if (
            type(raw_lineage_item) is not dict
            or set(raw_lineage_item) != _LINEAGE_FIELDS
            or type(raw_lineage_item["output_item_ordinal"]) is not int
            or raw_lineage_item["output_item_ordinal"] != ordinal
            or type(raw_lineage_item["merged_from"]) is not list
            or not raw_lineage_item["merged_from"]
            or any(
                type(input_id) is not str or input_id not in allowed
                for input_id in raw_lineage_item["merged_from"]
            )
        ):
            raise _knowledge_map_error(
                "knowledge_consolidation_coverage_invalid",
                "Consolidated item lineage is invalid",
            )
        merged_from = tuple(raw_lineage_item["merged_from"])
        supported_segments = {
            segment_id
            for source_item in source_items
            if source_item.knowledge_item_id in merged_from
            for segment_id in source_item.source_segment_ids
        }
        if set(parsed_map.items[ordinal].source_segment_ids).difference(
            supported_segments
        ):
            raise _knowledge_map_error(
                "knowledge_consolidation_evidence_invalid",
                "A consolidated item cites evidence outside its merged lineage",
            )
        claimed.extend(merged_from)
        consolidated_items.append(
            ConsolidatedKnowledgeItemV1(parsed_map.items[ordinal], merged_from)
        )
    omissions = _parse_omissions(
        value["omissions"], allowed_ids=input_ids, limits=coverage_limits
    )
    omitted_ids = [omission.input_id for omission in omissions]
    all_claims = (*claimed, *omitted_ids)
    if len(all_claims) != len(set(all_claims)) or set(all_claims) != allowed:
        raise _knowledge_map_error(
            "knowledge_consolidation_coverage_invalid",
            "Every consolidation input must have exactly one lineage outcome",
        )
    if len(input_ids) > 1 and len(consolidated_items) >= len(input_ids):
        raise _knowledge_map_error(
            "knowledge_consolidation_no_progress",
            "Knowledge consolidation must reduce its input cardinality",
        )
    return ConsolidatedKnowledgeV1(
        schema_version=1,
        batch_ordinal=batch_ordinal,
        batch_sha256=batch_sha256,
        items=tuple(consolidated_items),
        omissions=omissions,
        term_candidates=parsed_map.term_candidates,
        warnings=parsed_map.warnings,
    )


def _binding_sha256(request: VideoCompilationPlanningRequestV1) -> str:
    value = asdict(request.model_binding)
    return sha256_digest(encode_json(value))


def plan_video_compilation(
    request: VideoCompilationPlanningRequestV1,
) -> VideoCompilationPlanV1:
    if not isinstance(request, VideoCompilationPlanningRequestV1):
        raise DomainError(
            "video_compilation_plan_input_invalid",
            ErrorCategory.INVALID_REQUEST,
            "Compilation planning requires the v1 request contract",
        )
    if request.quality_profile is not CompilationQualityProfile.BALANCED:
        raise DomainError(
            "compilation_profile_unsupported",
            ErrorCategory.INVALID_REQUEST,
            "The first knowledge compiler supports only the balanced profile",
        )
    current_transcript_sha256 = transcript_sha256(request.transcript)
    if request.transcript_quality.transcript_sha256 != current_transcript_sha256:
        raise DomainError(
            "transcript_assessment_mismatch",
            ErrorCategory.CONFLICT,
            "Transcript quality assessment does not match the Transcript",
        )
    if request.transcript_quality.status is TranscriptQualityStatus.FAIL:
        raise DomainError(
            "transcript_quality_failed",
            ErrorCategory.RECIPE_FAILED,
            "Transcript quality is not sufficient for knowledge compilation",
        )

    safety_tokens = max(128, math.ceil(request.model_binding.context_window_tokens * 0.1))
    token_budget = (
        request.model_binding.context_window_tokens
        - request.reserved_output_tokens
        - safety_tokens
    )
    byte_budget = request.max_request_bytes
    if token_budget <= request.prompt_overhead_tokens:
        raise DomainError(
            "model_context_insufficient",
            ErrorCategory.INVALID_REQUEST,
            "Model context leaves no safe Transcript input budget",
        )

    chunks: list[TranscriptChunkRefV1] = []
    current_ids: list[str] = []
    current_start_ordinal = 0
    current_start_ms = 0
    current_end_ms = 0
    current_tokens = request.prompt_overhead_tokens
    current_bytes = request.prompt_overhead_bytes

    def flush() -> None:
        nonlocal current_ids, current_tokens, current_bytes
        if not current_ids:
            return
        end_ordinal = current_start_ordinal + len(current_ids)
        chunks.append(
            TranscriptChunkRefV1(
                ordinal=len(chunks),
                start_segment_ordinal=current_start_ordinal,
                end_segment_ordinal_exclusive=end_ordinal,
                start_segment_id=current_ids[0],
                end_segment_id=current_ids[-1],
                start_ms=current_start_ms,
                end_ms=current_end_ms,
                segment_count=len(current_ids),
                estimated_input_tokens=current_tokens,
                encoded_input_bytes=current_bytes,
                segment_ids_sha256=sha256_digest(encode_json(current_ids)),
            )
        )
        current_ids = []
        current_tokens = request.prompt_overhead_tokens
        current_bytes = request.prompt_overhead_bytes

    for ordinal, segment in enumerate(request.transcript.segments):
        segment_tokens, segment_bytes = estimate_segment_input_cost(segment)
        if (
            request.prompt_overhead_tokens + segment_tokens > token_budget
            or request.prompt_overhead_bytes + segment_bytes > byte_budget
        ):
            raise DomainError(
                "transcript_segment_budget_exceeded",
                ErrorCategory.RECIPE_FAILED,
                "A Transcript segment exceeds the safe model request budget",
                {"segment_id": segment.segment_id},
            )
        if current_ids and (
            current_tokens + segment_tokens > token_budget
            or current_bytes + segment_bytes > byte_budget
            or segment.end_ms - current_start_ms
            > request.max_chunk_duration_ms
        ):
            flush()
        if not current_ids:
            current_start_ordinal = ordinal
            current_start_ms = segment.start_ms
            current_end_ms = segment.end_ms
        else:
            current_end_ms = max(current_end_ms, segment.end_ms)
        current_ids.append(segment.segment_id)
        current_tokens += segment_tokens
        current_bytes += segment_bytes
    flush()

    aggregate_map_tokens = (
        len(chunks) * request.estimated_map_output_tokens_per_chunk
    )
    aggregate_map_bytes = (
        len(chunks) * request.map_output_byte_budget_per_chunk
    )
    composer_input_tokens = token_budget - request.prompt_overhead_tokens
    composer_input_bytes = byte_budget - request.prompt_overhead_bytes
    if len(chunks) == 1:
        topology = CompilationTopology.DIRECT
        sequential_waves = 1
    elif (
        aggregate_map_tokens <= composer_input_tokens
        and aggregate_map_bytes <= composer_input_bytes
    ):
        topology = CompilationTopology.MAP_COMPOSE
        sequential_waves = 2
    else:
        fan_in = min(
            composer_input_tokens
            // request.estimated_map_output_tokens_per_chunk,
            composer_input_bytes
            // request.map_output_byte_budget_per_chunk,
        )
        if fan_in < 2:
            raise DomainError(
                "model_context_insufficient",
                ErrorCategory.INVALID_REQUEST,
                "Model context cannot make progress consolidating Knowledge Maps",
            )
        topology = CompilationTopology.HIERARCHICAL_COMPOSE
        remaining = len(chunks)
        consolidation_waves = 0
        while (
            remaining * request.estimated_map_output_tokens_per_chunk
            > composer_input_tokens
            or remaining * request.map_output_byte_budget_per_chunk
            > composer_input_bytes
        ):
            remaining = math.ceil(remaining / fan_in)
            consolidation_waves += 1
        sequential_waves = 2 + consolidation_waves

    return VideoCompilationPlanV1(
        schema_version=1,
        recipe_id=request.recipe_id,
        recipe_version=request.recipe_version,
        quality_profile=request.quality_profile,
        topology=topology,
        transcript_sha256=current_transcript_sha256,
        model_binding_sha256=_binding_sha256(request),
        stage_id=request.stage_id,
        stage_version=request.stage_version,
        prompt_id=request.prompt_id,
        prompt_version=request.prompt_version,
        transcript_chunks=tuple(chunks),
        token_estimator_id=_TOKEN_ESTIMATOR_ID,
        chunk_input_token_budget=token_budget,
        chunk_input_byte_budget=byte_budget,
        extraction_concurrency=min(request.model_binding.max_concurrency, len(chunks)),
        expected_sequential_model_waves=sequential_waves,
        writing_mode=CompilationWritingMode.WHOLE_ARTICLE,
        editorial_mode=CompilationEditorialMode.WHOLE_ARTICLE,
        reviewer_enabled=False,
        max_repair_attempts=request.max_repair_attempts,
    )


__all__ = [
    "assess_transcript_quality",
    "estimate_segment_input_cost",
    "parse_chunk_knowledge_map",
    "parse_composed_knowledge_draft",
    "parse_consolidated_knowledge",
    "plan_video_compilation",
]

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from app.core.domain.ids import sha256_digest
from app.core.domain.video import FaithfulLanguagePolicy, QualityOverall, TranscriptDocument
from app.core.errors import DomainError
from app.core.portable.jsonio import encode_utf8_lf
from app.core.portable.markdown_safety import validate_markdown_safety
from app.core.recipes.video.faithful_edition.contracts import (
    FaithfulEditionPlanV1,
    FaithfulEditionSectionV1,
)


_NUMBER = re.compile(r"(?<![\w])[-+]?\d+(?:[.,]\d+)*(?:%|％)?")
_TECHNICAL = re.compile(
    r"(?<![A-Za-z0-9_])(?:/[A-Za-z0-9._/-]+|"
    r"[A-Za-z][A-Za-z0-9]*(?:[-_.:/][A-Za-z0-9]+)+|"
    r"UE5|API|MCP|LLM|FFmpeg)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_QUALIFIERS = (
    "not",
    "never",
    "no",
    "may",
    "might",
    "must",
    "only",
    "unless",
    "however",
    "but",
    "except",
    "不",
    "不能",
    "可能",
    "也许",
    "必须",
    "仅",
    "除非",
    "但是",
    "不过",
)


class QualityCheckMethod(StrEnum):
    DETERMINISTIC = "deterministic"
    MODEL = "model"
    HUMAN = "human"


class QualityCheckStatus(StrEnum):
    PASS = "pass"
    WARNING = "warning"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class FaithfulQualityCheckV1:
    check_id: str
    method: QualityCheckMethod
    status: QualityCheckStatus
    severity: str
    scope: str
    safe_details: str
    failed_section_ordinals: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "failed_section_ordinals",
            tuple(sorted(set(self.failed_section_ordinals))),
        )


@dataclass(frozen=True)
class FaithfulTextMetricsV1:
    body_segment_reference_coverage_ratio: float
    order_violation_count: int
    unknown_reference_count: int
    duplicate_assignment_count: int
    source_character_count: int
    target_character_count: int
    length_ratio: float
    number_mismatch_count: int
    technical_token_mismatch_count: int
    qualifier_warning_count: int
    uncertainty_count: int
    anchor_warning_count: int


@dataclass(frozen=True)
class FaithfulTextAssessmentV1:
    transcript_sha256: str
    draft_sha256: str
    language_policy: FaithfulLanguagePolicy
    overall: QualityOverall
    checks: tuple[FaithfulQualityCheckV1, ...]
    metrics: FaithfulTextMetricsV1
    failed_section_ordinals: tuple[int, ...]
    repairable: bool


@dataclass(frozen=True)
class FaithfulEditionCandidateV1:
    transcript: TranscriptDocument
    plan: FaithfulEditionPlanV1
    sections: tuple[FaithfulEditionSectionV1, ...]
    markdown: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "sections", tuple(self.sections))


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _anchors(pattern: re.Pattern[str], value: str) -> tuple[str, ...]:
    return tuple(match.group(0).casefold() for match in pattern.finditer(value))


def _qualifier_count(value: str) -> dict[str, int]:
    normalized = _normalize(value)
    return {
        qualifier: (
            len(
                re.findall(
                    rf"(?<![a-z0-9_]){re.escape(qualifier)}(?![a-z0-9_])",
                    normalized,
                )
            )
            if qualifier.isascii()
            else normalized.count(qualifier)
        )
        for qualifier in _QUALIFIERS
        if qualifier in normalized
    }


def _check(
    check_id: str,
    status: QualityCheckStatus,
    details: str,
    *,
    severity: str = "error",
    scope: str = "document",
    failed_sections: tuple[int, ...] = (),
) -> FaithfulQualityCheckV1:
    return FaithfulQualityCheckV1(
        check_id=check_id,
        method=QualityCheckMethod.DETERMINISTIC,
        status=status,
        severity=severity,
        scope=scope,
        safe_details=details,
        failed_section_ordinals=failed_sections,
    )


def _mismatches(
    candidate: FaithfulEditionCandidateV1,
) -> tuple[int, int, int, tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
    by_id = {segment.segment_id: segment for segment in candidate.transcript.segments}
    number_count = 0
    technical_count = 0
    qualifier_count = 0
    number_sections: set[int] = set()
    technical_sections: set[int] = set()
    qualifier_sections: set[int] = set()
    for section in candidate.sections:
        for paragraph in section.paragraphs:
            source = " ".join(by_id[value].text for value in paragraph.source_segment_ids)
            if sorted(_anchors(_NUMBER, source)) != sorted(
                _anchors(_NUMBER, paragraph.text)
            ):
                number_count += 1
                number_sections.add(section.ordinal)
            if sorted(_anchors(_TECHNICAL, source)) != sorted(
                _anchors(_TECHNICAL, paragraph.text)
            ):
                technical_count += 1
                technical_sections.add(section.ordinal)
            if (
                candidate.plan.language_policy
                is FaithfulLanguagePolicy.PRESERVE_SOURCE
                and _qualifier_count(source) != _qualifier_count(paragraph.text)
            ):
                qualifier_count += 1
                qualifier_sections.add(section.ordinal)
    return (
        number_count,
        technical_count,
        qualifier_count,
        tuple(sorted(number_sections)),
        tuple(sorted(technical_sections)),
        tuple(sorted(qualifier_sections)),
    )


def assess_faithful_edition(
    candidate: FaithfulEditionCandidateV1,
) -> FaithfulTextAssessmentV1:
    if not isinstance(candidate, FaithfulEditionCandidateV1):
        raise TypeError("candidate must use the faithful quality contract")
    transcript_ids = tuple(segment.segment_id for segment in candidate.transcript.segments)
    excluded = frozenset(candidate.plan.excluded_segment_ids)
    expected_ids = tuple(value for value in transcript_ids if value not in excluded)
    index = {value: ordinal for ordinal, value in enumerate(expected_ids)}
    body_ids = tuple(
        source_id
        for section in candidate.sections
        for paragraph in section.paragraphs
        for source_id in paragraph.source_segment_ids
    )
    unknown = tuple(value for value in body_ids if value not in index)
    known = tuple(value for value in body_ids if value in index)
    duplicate_count = len(known) - len(set(known))
    order_count = sum(
        index[current] < index[previous]
        for previous, current in zip(known, known[1:])
    )
    coverage_ratio = len(set(known)) / len(expected_ids) if expected_ids else 0.0
    coverage_ok = body_ids == expected_ids

    (
        number_count,
        technical_count,
        qualifier_count,
        number_sections,
        technical_sections,
        qualifier_sections,
    ) = _mismatches(candidate)
    source_characters = sum(len(segment.text) for segment in candidate.transcript.segments)
    target_characters = sum(
        len(paragraph.text)
        for section in candidate.sections
        for paragraph in section.paragraphs
    )
    length_ratio = target_characters / source_characters if source_characters else 0.0
    uncertainty_count = sum(len(section.uncertainties) for section in candidate.sections)

    try:
        validate_markdown_safety(
            candidate.markdown,
            bundle_relative_path="drafts/faithful-edition.md",
        )
        markdown_safe = True
    except DomainError:
        markdown_safe = False
    translation_label = (
        candidate.plan.language_policy is FaithfulLanguagePolicy.PRESERVE_SOURCE
        or (
            "翻译型高保真精编稿" in candidate.markdown
            and f"源语言：{candidate.plan.source_language}" in candidate.markdown
            and f"目标语言：{candidate.plan.target_language}" in candidate.markdown
        )
    )
    regions_separated = all(
        heading in candidate.markdown
        for heading in (
            "## 精编正文",
            "#### AI 章节摘要",
            "#### AI 关键点",
            "#### 待复核项",
        )
    )
    length_status = (
        QualityCheckStatus.NOT_APPLICABLE
        if candidate.plan.language_policy
        is FaithfulLanguagePolicy.TRANSLATE_TO_OUTPUT
        else QualityCheckStatus.PASS
        if 0.45 <= length_ratio <= 1.8
        else QualityCheckStatus.WARNING
    )
    qualifier_status = (
        QualityCheckStatus.NOT_APPLICABLE
        if candidate.plan.language_policy
        is FaithfulLanguagePolicy.TRANSLATE_TO_OUTPUT
        else QualityCheckStatus.PASS
        if qualifier_count == 0
        else QualityCheckStatus.WARNING
    )
    checks = (
        _check(
            "body_segment_mapping",
            QualityCheckStatus.PASS if coverage_ok else QualityCheckStatus.FAIL,
            "Body segment references must cover every editable segment once in order",
        ),
        _check(
            "numeric_anchors",
            QualityCheckStatus.PASS if number_count == 0 else QualityCheckStatus.FAIL,
            "Numbers, percentages, dates, and versions must be preserved",
            failed_sections=number_sections,
        ),
        _check(
            "technical_tokens",
            QualityCheckStatus.PASS
            if technical_count == 0
            else QualityCheckStatus.FAIL,
            "Technical tokens, paths, and commands must be preserved",
            failed_sections=technical_sections,
        ),
        _check(
            "qualifier_risk",
            qualifier_status,
            "Negation, uncertainty, scope, and correction markers require preservation",
            severity="warning",
            failed_sections=qualifier_sections,
        ),
        _check(
            "length_change",
            length_status,
            "Length ratio is observational for translation and bounded for source-language editing",
            severity="warning",
        ),
        _check(
            "translation_label",
            QualityCheckStatus.PASS if translation_label else QualityCheckStatus.FAIL,
            "Translated faithful editions must declare source and target languages",
        ),
        _check(
            "semantic_regions",
            QualityCheckStatus.PASS if regions_separated else QualityCheckStatus.FAIL,
            "Faithful body, AI summary, key points, and uncertainties must remain separate",
        ),
        _check(
            "markdown_safety",
            QualityCheckStatus.PASS if markdown_safe else QualityCheckStatus.FAIL,
            "Faithful edition Markdown must be safe",
        ),
    )
    failed_sections = tuple(
        sorted(
            {
                ordinal
                for check in checks
                if check.status is QualityCheckStatus.FAIL
                for ordinal in check.failed_section_ordinals
            }
        )
    )
    overall = (
        QualityOverall.FAIL
        if any(check.status is QualityCheckStatus.FAIL for check in checks)
        else QualityOverall.PASS_WITH_WARNINGS
        if any(check.status is QualityCheckStatus.WARNING for check in checks)
        else QualityOverall.PASS
    )
    metrics = FaithfulTextMetricsV1(
        body_segment_reference_coverage_ratio=coverage_ratio,
        order_violation_count=order_count,
        unknown_reference_count=len(unknown),
        duplicate_assignment_count=duplicate_count,
        source_character_count=source_characters,
        target_character_count=target_characters,
        length_ratio=length_ratio,
        number_mismatch_count=number_count,
        technical_token_mismatch_count=technical_count,
        qualifier_warning_count=qualifier_count,
        uncertainty_count=uncertainty_count,
        anchor_warning_count=number_count + technical_count + qualifier_count,
    )
    return FaithfulTextAssessmentV1(
        transcript_sha256=candidate.plan.transcript_sha256,
        draft_sha256=sha256_digest(encode_utf8_lf(candidate.markdown)),
        language_policy=candidate.plan.language_policy,
        overall=overall,
        checks=checks,
        metrics=metrics,
        failed_section_ordinals=failed_sections,
        repairable=bool(failed_sections),
    )


__all__ = [
    "FaithfulEditionCandidateV1",
    "FaithfulQualityCheckV1",
    "FaithfulTextAssessmentV1",
    "FaithfulTextMetricsV1",
    "QualityCheckMethod",
    "QualityCheckStatus",
    "assess_faithful_edition",
]

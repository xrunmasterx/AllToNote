from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from app.core.domain.ids import sha256_digest
from app.core.domain.video import QualityOverall
from app.core.errors import DomainError, ErrorCategory, ErrorDetail
from app.core.portable.jsonio import encode_utf8_lf
from app.core.portable.markdown_safety import (
    is_backslash_escaped,
    markdown_visible_lines,
    validate_markdown_safety,
)


_SEGMENT_ID = re.compile(r"seg_[0-9]{6,}\Z")
_SEGMENT_CONTROL = re.compile(r"\[\^(seg_[0-9]{6,})\]\Z")
_FOOTNOTE_DEFINITION = re.compile(r"^ {0,3}\[\^[^\]\r\n]*\]:")
_ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_SETEXT_UNDERLINE = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_TRAILING_ATX_MARKERS = re.compile(r"[ \t]+#+[ \t]*$")
_PLACEHOLDER = re.compile(
    r"\{\{[^{}]+\}\}|\b(?:TODO|TBD|PLACEHOLDER)\b",
    re.IGNORECASE,
)
_CHECK_ORDER = (
    "unique_h1",
    "heading_hierarchy",
    "duplicate_headings",
    "segment_citations",
    "substantive_h2_citations",
    "coverage_ledger",
    "placeholders",
    "markdown_safety",
)


def _invalid_input() -> DomainError:
    return DomainError(
        "knowledge_note_quality_input_invalid",
        ErrorCategory.INVALID_REQUEST,
        "Knowledge note quality input is invalid",
    )


@dataclass(frozen=True)
class CoverageOmissionV1:
    input_id: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.input_id, str) or not isinstance(
            self.reason, str
        ):
            raise _invalid_input()


@dataclass(frozen=True)
class KnowledgeNoteCandidateV1:
    markdown: str
    allowed_segment_ids: tuple[str, ...]
    required_coverage_input_ids: tuple[str, ...]
    covered_coverage_input_ids: tuple[str, ...]
    omissions: tuple[CoverageOmissionV1, ...]

    def __post_init__(self) -> None:
        try:
            allowed = tuple(self.allowed_segment_ids)
            required = tuple(self.required_coverage_input_ids)
            covered = tuple(self.covered_coverage_input_ids)
            omissions = tuple(self.omissions)
        except TypeError:
            raise _invalid_input() from None
        if (
            not isinstance(self.markdown, str)
            or any(
                not isinstance(value, str) or _SEGMENT_ID.fullmatch(value) is None
                for value in allowed
            )
            or len(set(allowed)) != len(allowed)
            or any(not isinstance(value, str) or not value for value in required)
            or len(set(required)) != len(required)
            or any(not isinstance(value, str) or not value for value in covered)
            or any(not isinstance(value, CoverageOmissionV1) for value in omissions)
        ):
            raise _invalid_input()
        object.__setattr__(self, "allowed_segment_ids", allowed)
        object.__setattr__(self, "required_coverage_input_ids", required)
        object.__setattr__(self, "covered_coverage_input_ids", covered)
        object.__setattr__(self, "omissions", omissions)


@dataclass(frozen=True)
class KnowledgeNoteQualityCheckV1:
    check_id: str
    status: str
    reason: str | None = None
    line_numbers: tuple[int, ...] = ()
    related_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "line_numbers", tuple(self.line_numbers))
        object.__setattr__(self, "related_ids", tuple(self.related_ids))


@dataclass(frozen=True)
class KnowledgeNoteTextAssessmentV1:
    subject_sha256: str
    overall: QualityOverall
    checks: tuple[KnowledgeNoteQualityCheckV1, ...]
    cited_segment_ids: tuple[str, ...]

    @property
    def failed_checks(self) -> tuple[KnowledgeNoteQualityCheckV1, ...]:
        return tuple(check for check in self.checks if check.status == "fail")


@dataclass(frozen=True)
class KnowledgeNoteRepairRequestV1:
    subject_sha256: str
    failed_checks: tuple[KnowledgeNoteQualityCheckV1, ...]
    markdown_excerpt: str
    related_coverage_input_ids: tuple[str, ...]
    related_segment_ids: tuple[str, ...]


@dataclass(frozen=True)
class KnowledgeNoteQualityOutcomeV1:
    final_candidate: KnowledgeNoteCandidateV1
    assessment: KnowledgeNoteTextAssessmentV1
    overall: QualityOverall
    publish_eligible: bool
    repair_attempts: int
    execution_error: ErrorDetail | None


@dataclass(frozen=True)
class _Heading:
    level: int
    title: str
    line_number: int
    end_line_number: int


@dataclass(frozen=True)
class _CitationScan:
    cited_segment_ids: tuple[str, ...]
    citations_by_line: tuple[tuple[str, ...], ...]
    invalid_lines: tuple[int, ...]
    related_ids: tuple[str, ...]


def _check(
    check_id: str,
    valid: bool,
    reason: str,
    *,
    line_numbers: tuple[int, ...] = (),
    related_ids: tuple[str, ...] = (),
) -> KnowledgeNoteQualityCheckV1:
    return KnowledgeNoteQualityCheckV1(
        check_id=check_id,
        status="pass" if valid else "fail",
        reason=None if valid else reason,
        line_numbers=() if valid else tuple(sorted(set(line_numbers))),
        related_ids=() if valid else tuple(sorted(set(related_ids))),
    )


def _heading_title(value: str) -> str:
    return _TRAILING_ATX_MARKERS.sub("", value).strip()


def _normalized_heading(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split()).casefold()


def _find_headings(lines: tuple[str, ...]) -> tuple[_Heading, ...]:
    headings: list[_Heading] = []
    occupied: set[int] = set()
    for index, line in enumerate(lines):
        match = _ATX_HEADING.fullmatch(line)
        if match is None:
            continue
        headings.append(
            _Heading(
                level=len(match.group(1)),
                title=_heading_title(match.group(2) or ""),
                line_number=index + 1,
                end_line_number=index + 1,
            )
        )
        occupied.add(index)

    for index in range(1, len(lines)):
        match = _SETEXT_UNDERLINE.fullmatch(lines[index])
        if (
            match is None
            or index in occupied
            or index - 1 in occupied
            or not lines[index - 1].strip()
        ):
            continue
        headings.append(
            _Heading(
                level=1 if match.group(1).startswith("=") else 2,
                title=lines[index - 1].strip(),
                line_number=index,
                end_line_number=index + 1,
            )
        )
        occupied.update((index - 1, index))
    return tuple(sorted(headings, key=lambda heading: heading.line_number))


def _scan_citations(
    lines: tuple[str, ...], allowed_segment_ids: tuple[str, ...]
) -> _CitationScan:
    allowed = frozenset(allowed_segment_ids)
    citations: list[str] = []
    seen: set[str] = set()
    by_line: list[tuple[str, ...]] = []
    invalid_lines: set[int] = set()
    related: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        line_ids: list[str] = []
        is_definition = _FOOTNOTE_DEFINITION.match(line) is not None
        if is_definition:
            invalid_lines.add(line_number)
        cursor = 0
        while True:
            start = line.find("[^", cursor)
            if start < 0:
                break
            if is_backslash_escaped(line, start):
                cursor = start + 2
                continue
            closing = line.find("]", start + 2)
            if closing < 0:
                invalid_lines.add(line_number)
                break
            control = line[start : closing + 1]
            match = _SEGMENT_CONTROL.fullmatch(control)
            if match is None:
                invalid_lines.add(line_number)
            else:
                segment_id = match.group(1)
                related.add(segment_id)
                if segment_id not in allowed:
                    invalid_lines.add(line_number)
                elif not is_definition:
                    line_ids.append(segment_id)
                if (
                    segment_id in allowed
                    and not is_definition
                    and segment_id not in seen
                ):
                    seen.add(segment_id)
                    citations.append(segment_id)
            cursor = closing + 1
        by_line.append(tuple(line_ids))
    if not citations:
        invalid_lines.add(1)
    return _CitationScan(
        cited_segment_ids=tuple(citations),
        citations_by_line=tuple(by_line),
        invalid_lines=tuple(sorted(invalid_lines)),
        related_ids=tuple(sorted(related)),
    )


def _heading_checks(
    headings: tuple[_Heading, ...],
) -> tuple[KnowledgeNoteQualityCheckV1, ...]:
    h1_lines = tuple(heading.line_number for heading in headings if heading.level == 1)
    hierarchy_lines: list[int] = []
    previous_level: int | None = None
    for heading in headings:
        if not heading.title or (
            previous_level is None and heading.level != 1
        ) or (
            previous_level is not None and heading.level > previous_level + 1
        ):
            hierarchy_lines.append(heading.line_number)
        previous_level = heading.level

    by_title: dict[str, list[int]] = {}
    for heading in headings:
        key = _normalized_heading(heading.title)
        if key:
            by_title.setdefault(key, []).append(heading.line_number)
    duplicate_lines = tuple(
        line
        for lines in by_title.values()
        if len(lines) > 1
        for line in lines
    )
    return (
        _check(
            "unique_h1",
            len(h1_lines) == 1,
            "Knowledge note must contain exactly one H1",
            line_numbers=h1_lines or (1,),
        ),
        _check(
            "heading_hierarchy",
            not hierarchy_lines,
            "Heading levels must begin at H1 and must not skip a level",
            line_numbers=tuple(hierarchy_lines),
        ),
        _check(
            "duplicate_headings",
            not duplicate_lines,
            "Knowledge note contains duplicate heading titles",
            line_numbers=duplicate_lines,
        ),
    )


def _h2_citation_check(
    lines: tuple[str, ...],
    headings: tuple[_Heading, ...],
    citation_scan: _CitationScan,
) -> KnowledgeNoteQualityCheckV1:
    heading_lines = {
        line_number
        for heading in headings
        for line_number in range(heading.line_number, heading.end_line_number + 1)
    }
    failed_lines: list[int] = []
    for index, heading in enumerate(headings):
        if heading.level != 2:
            continue
        end_line = len(lines) + 1
        for next_heading in headings[index + 1 :]:
            if next_heading.level <= 2:
                end_line = next_heading.line_number
                break
        section_lines = range(heading.end_line_number + 1, end_line)
        substantive = False
        cited = False
        for line_number in section_lines:
            if line_number in heading_lines:
                continue
            line = lines[line_number - 1]
            without_controls = re.sub(r"\[\^[^\]]*\]", "", line)
            substantive = substantive or bool(without_controls.strip())
            cited = cited or bool(citation_scan.citations_by_line[line_number - 1])
        if substantive and not cited:
            failed_lines.extend(range(heading.line_number, end_line))
    return _check(
        "substantive_h2_citations",
        not failed_lines,
        "Each substantive H2 section must cite at least one allowed segment",
        line_numbers=tuple(failed_lines),
    )


def _coverage_check(
    candidate: KnowledgeNoteCandidateV1,
) -> KnowledgeNoteQualityCheckV1:
    required = set(candidate.required_coverage_input_ids)
    covered_values = candidate.covered_coverage_input_ids
    omitted_values = tuple(omission.input_id for omission in candidate.omissions)
    covered = set(covered_values)
    omitted = set(omitted_values)
    reasons_valid = all(omission.reason.strip() for omission in candidate.omissions)
    valid = (
        len(covered) == len(covered_values)
        and len(omitted) == len(omitted_values)
        and not covered.intersection(omitted)
        and covered.union(omitted) == required
        and reasons_valid
    )
    related = tuple(sorted(required.union(covered, omitted)))
    return _check(
        "coverage_ledger",
        valid,
        "Every required knowledge item must be covered once or omitted with a reason",
        related_ids=related,
    )


def assess_knowledge_note(
    candidate: KnowledgeNoteCandidateV1,
) -> KnowledgeNoteTextAssessmentV1:
    if not isinstance(candidate, KnowledgeNoteCandidateV1):
        raise _invalid_input()
    visible_lines = markdown_visible_lines(candidate.markdown)
    headings = _find_headings(visible_lines)
    citation_scan = _scan_citations(visible_lines, candidate.allowed_segment_ids)
    visible_text = "\n".join(visible_lines)
    placeholder_lines = tuple(
        line_number
        for line_number, line in enumerate(visible_lines, start=1)
        if _PLACEHOLDER.search(line) is not None
    )
    try:
        validate_markdown_safety(
            candidate.markdown,
            bundle_relative_path="drafts/knowledge-note.md",
        )
        markdown_safe = True
    except DomainError:
        markdown_safe = False

    checks = (
        *_heading_checks(headings),
        _check(
            "segment_citations",
            not citation_scan.invalid_lines,
            "Segment citations must be well formed, inline, and inside the allow-set",
            line_numbers=citation_scan.invalid_lines,
            related_ids=citation_scan.related_ids,
        ),
        _h2_citation_check(visible_lines, headings, citation_scan),
        _coverage_check(candidate),
        _check(
            "placeholders",
            _PLACEHOLDER.search(visible_text) is None,
            "Knowledge note contains an unresolved placeholder",
            line_numbers=placeholder_lines,
        ),
        _check(
            "markdown_safety",
            markdown_safe,
            "Knowledge note Markdown is unsafe",
            line_numbers=tuple(range(1, len(visible_lines) + 1)),
        ),
    )
    if tuple(check.check_id for check in checks) != _CHECK_ORDER:
        raise RuntimeError("knowledge note quality check order changed")
    overall = (
        QualityOverall.FAIL
        if any(check.status == "fail" for check in checks)
        else QualityOverall.PASS
    )
    return KnowledgeNoteTextAssessmentV1(
        subject_sha256=sha256_digest(encode_utf8_lf(candidate.markdown)),
        overall=overall,
        checks=checks,
        cited_segment_ids=citation_scan.cited_segment_ids,
    )


def _repair_request(
    candidate: KnowledgeNoteCandidateV1,
    assessment: KnowledgeNoteTextAssessmentV1,
) -> KnowledgeNoteRepairRequestV1:
    failed_checks = assessment.failed_checks
    line_numbers = sorted(
        {line for check in failed_checks for line in check.line_numbers}
    )
    source_lines = candidate.markdown.splitlines()
    excerpt = "\n".join(
        f"{line_number}: {source_lines[line_number - 1]}"
        for line_number in line_numbers
        if line_number <= len(source_lines)
    )
    coverage_ids = {
        value
        for check in failed_checks
        if check.check_id == "coverage_ledger"
        for value in check.related_ids
    }
    segment_ids = {
        value
        for check in failed_checks
        if check.check_id in {"segment_citations", "substantive_h2_citations"}
        for value in check.related_ids
    }
    return KnowledgeNoteRepairRequestV1(
        subject_sha256=assessment.subject_sha256,
        failed_checks=failed_checks,
        markdown_excerpt=excerpt,
        related_coverage_input_ids=tuple(sorted(coverage_ids)),
        related_segment_ids=tuple(sorted(segment_ids)),
    )


def evaluate_knowledge_note(
    candidate: KnowledgeNoteCandidateV1,
    *,
    repair: Callable[
        [KnowledgeNoteRepairRequestV1], KnowledgeNoteCandidateV1
    ]
    | None = None,
) -> KnowledgeNoteQualityOutcomeV1:
    assessment = assess_knowledge_note(candidate)
    final_candidate = candidate
    repair_attempts = 0
    execution_error: ErrorDetail | None = None
    if assessment.overall is QualityOverall.FAIL and repair is not None:
        repair_attempts = 1
        try:
            repaired = repair(_repair_request(candidate, assessment))
            if not isinstance(repaired, KnowledgeNoteCandidateV1):
                raise TypeError("repair returned an invalid candidate")
            if (
                repaired.allowed_segment_ids != candidate.allowed_segment_ids
                or repaired.required_coverage_input_ids
                != candidate.required_coverage_input_ids
            ):
                raise ValueError("repair changed trusted quality context")
            final_candidate = repaired
            assessment = assess_knowledge_note(repaired)
        except MemoryError:
            raise
        except DomainError:
            raise
        except Exception:
            execution_error = ErrorDetail(
                code="knowledge_note_repair_failed",
                category=ErrorCategory.RECIPE_FAILED,
                message="Knowledge note quality repair failed",
            )
    publish_eligible = (
        assessment.overall is not QualityOverall.FAIL and execution_error is None
    )
    return KnowledgeNoteQualityOutcomeV1(
        final_candidate=final_candidate,
        assessment=assessment,
        overall=assessment.overall,
        publish_eligible=publish_eligible,
        repair_attempts=repair_attempts,
        execution_error=execution_error,
    )


__all__ = [
    "CoverageOmissionV1",
    "KnowledgeNoteCandidateV1",
    "KnowledgeNoteQualityCheckV1",
    "KnowledgeNoteQualityOutcomeV1",
    "KnowledgeNoteRepairRequestV1",
    "KnowledgeNoteTextAssessmentV1",
    "assess_knowledge_note",
    "evaluate_knowledge_note",
]

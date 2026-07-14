from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

from app.core.domain.ids import sha256_digest
from app.core.domain.video import GeneratedVideoDraft, QualityOverall
from app.core.errors import DomainError, ErrorCategory, ErrorDetail
from app.core.portable.artifacts import PortableArtifactRef, build_transcript
from app.core.portable.evidence import EvidenceSet
from app.core.portable.jsonio import encode_json, encode_utf8_lf
from app.core.portable.markdown_safety import (
    _analyze_markdown,
    validate_markdown_safety,
)


_PLACEHOLDER = re.compile(
    r"\{\{[^{}]+\}\}|\b(?:TODO|TBD|PLACEHOLDER)\b",
    re.IGNORECASE,
)
_EVIDENCE_RECORD_FIELDS = {
    "evidence_ref_schema_version",
    "evidence_id",
    "source_revision_ref",
    "target_artifact_ref",
    "locator",
    "excerpt_sha256",
    "extensions",
}
_REPAIRABLE_CHECKS = frozenset(
    {"citation_integrity", "h2_evidence", "draft_placeholders"}
)


@dataclass(frozen=True)
class QualityCheck:
    check_id: str
    status: str
    reason: str | None = None


@dataclass(frozen=True)
class QualityReport:
    subject_sha256: str
    overall: QualityOverall
    checks: tuple[QualityCheck, ...]
    evidence_ids: tuple[str, ...]
    payload: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "checks", tuple(self.checks))
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        object.__setattr__(self, "payload", bytes(self.payload))


@dataclass(frozen=True)
class QualityOutcome:
    final_draft: bytes
    report: QualityReport
    overall: QualityOverall
    publish_eligible: bool
    repair_attempts: int
    execution_error: ErrorDetail | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "final_draft", bytes(self.final_draft))


@dataclass(frozen=True)
class _Assessment:
    draft_bytes: bytes
    checks: tuple[QualityCheck, ...]
    used_evidence_ids: tuple[str, ...]

    @property
    def failed_check_ids(self) -> frozenset[str]:
        return frozenset(
            check.check_id for check in self.checks if check.status == "fail"
        )


def _check(check_id: str, valid: bool, reason: str) -> QualityCheck:
    return QualityCheck(
        check_id=check_id,
        status="pass" if valid else "fail",
        reason=None if valid else reason,
    )


def _load_evidence_records(evidence_set: EvidenceSet) -> list[object] | None:
    payload = evidence_set.payload
    if not isinstance(payload, bytes) or not payload.endswith(b"\n") or b"\r" in payload:
        return None
    try:
        text = payload.decode("utf-8")
        lines = text.splitlines()
        if not lines:
            return None
        return [
            json.loads(
                line,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
            for line in lines
        ]
    except (UnicodeError, ValueError):
        return None


def _evidence_is_valid(evidence_set: EvidenceSet) -> bool:
    records = _load_evidence_records(evidence_set)
    if records is None or not records or not isinstance(records[0], dict):
        return False
    try:
        expected_ids = evidence_set.evidence_ids
        transcript_digest = sha256_digest(
            build_transcript(
                evidence_set.source_revision_id,
                evidence_set.transcript.language,
                evidence_set.transcript.segments,
            )
        )
    except (AttributeError, DomainError, KeyError, TypeError):
        return False
    if transcript_digest != evidence_set.target_artifact_ref.sha256:
        return False
    header = records[0]
    if header != {
        "record_type": "evidence_set_header",
        "evidence_set_schema_version": 1,
        "bundle_id": evidence_set.target_artifact_ref.bundle_id,
        "record_count": len(evidence_set.segments),
    }:
        return False
    if len(records) != len(evidence_set.segments) + 1:
        return False

    target_ref = {
        "bundle_id": evidence_set.target_artifact_ref.bundle_id,
        "artifact_id": evidence_set.target_artifact_ref.artifact_id,
        "sha256": evidence_set.target_artifact_ref.sha256,
    }
    for record, segment, evidence_id in zip(
        records[1:],
        evidence_set.segments,
        expected_ids,
    ):
        if not isinstance(record, dict) or set(record) != _EVIDENCE_RECORD_FIELDS:
            return False
        if (
            record["evidence_ref_schema_version"] != 1
            or record["evidence_id"] != evidence_id
            or record["source_revision_ref"]
            != {
                "bundle_id": evidence_set.target_artifact_ref.bundle_id,
                "source_revision_id": evidence_set.source_revision_id,
            }
            or record["target_artifact_ref"] != target_ref
            or record["locator"]
            != {
                "scheme": "video-time-range.v1",
                "start_ms": segment.start_ms,
                "end_ms": segment.end_ms,
            }
            or record["excerpt_sha256"] != sha256_digest(segment.text)
            or record["extensions"] != {}
        ):
            return False
    return True


def _citation_checks(
    draft: GeneratedVideoDraft,
    evidence_set: EvidenceSet,
) -> tuple[bool, bool, tuple[str, ...]]:
    analysis = _analyze_markdown(draft.markdown)
    known_evidence = set(evidence_set.evidence_ids)
    citations = set(analysis.citation_ids)
    definitions = set(analysis.definition_ids)
    mapped_draft_citations = {
        evidence_set.citation_map[segment_id]
        for segment_id in draft.cited_segment_ids
        if segment_id in evidence_set.citation_map
    }
    citations_valid = (
        not analysis.has_segment_citation
        and not analysis.duplicate_definition_ids
        and citations == definitions
        and citations <= known_evidence
        and len(mapped_draft_citations) == len(draft.cited_segment_ids)
        and citations == mapped_draft_citations
    )
    h2_valid = bool(analysis.substantive_h2_citations) and all(
        bool(section_citations)
        and set(section_citations) <= known_evidence
        and set(section_citations) <= citations
        for section_citations in analysis.substantive_h2_citations
    )
    used_evidence_ids = tuple(
        evidence_id for evidence_id in evidence_set.evidence_ids if evidence_id in citations
    )
    return citations_valid, h2_valid, used_evidence_ids


def _assess(
    draft: GeneratedVideoDraft,
    evidence_set: EvidenceSet,
) -> _Assessment:
    draft_bytes = encode_utf8_lf(draft.markdown)
    try:
        validate_markdown_safety(
            draft.markdown,
            bundle_relative_path="drafts/note.md",
        )
        markdown_safe = True
    except DomainError:
        markdown_safe = False

    citations_valid, h2_valid, used_evidence_ids = _citation_checks(
        draft,
        evidence_set,
    )
    checks = (
        _check(
            "evidence_integrity",
            _evidence_is_valid(evidence_set),
            "Evidence locator or digest is invalid",
        ),
        _check(
            "markdown_safety",
            markdown_safe,
            "Draft Markdown is unsafe",
        ),
        _check(
            "citation_integrity",
            citations_valid,
            "Draft evidence citations and definitions are inconsistent",
        ),
        _check(
            "h2_evidence",
            h2_valid,
            "Each substantive H2 section requires evidence",
        ),
        _check(
            "draft_placeholders",
            _PLACEHOLDER.search(draft.markdown) is None,
            "Draft contains an unresolved placeholder",
        ),
    )
    return _Assessment(
        draft_bytes=draft_bytes,
        checks=checks,
        used_evidence_ids=used_evidence_ids,
    )


def _make_report(
    assessment: _Assessment,
    *,
    draft_bundle_id: str,
    draft_artifact_id: str,
    repair_attempts: int,
) -> QualityReport:
    subject_sha256 = sha256_digest(assessment.draft_bytes)
    subject = PortableArtifactRef(
        draft_bundle_id,
        draft_artifact_id,
        subject_sha256,
    )
    overall = (
        QualityOverall.FAIL
        if assessment.failed_check_ids
        else QualityOverall.PASS
    )
    check_documents: list[dict[str, object]] = []
    for check in assessment.checks:
        document: dict[str, object] = {
            "id": check.check_id,
            "status": check.status,
        }
        if check.reason is not None:
            document["reason"] = check.reason
        check_documents.append(document)
    payload = encode_json(
        {
            "quality_report_schema_version": 1,
            "subject": {
                "bundle_id": subject.bundle_id,
                "artifact_id": subject.artifact_id,
                "sha256": subject.sha256,
            },
            "profile": {"id": "video-note-default", "version": 1},
            "overall": overall.value,
            "checks": check_documents,
            "method": {"kind": "deterministic"},
            "metrics": {"quality_repair_attempts": repair_attempts},
            "messages": sorted(assessment.failed_check_ids),
            "evidence_ids": list(assessment.used_evidence_ids),
        }
    )
    return QualityReport(
        subject_sha256=subject_sha256,
        overall=overall,
        checks=assessment.checks,
        evidence_ids=assessment.used_evidence_ids,
        payload=payload,
    )


def evaluate_video_draft(
    draft: GeneratedVideoDraft,
    evidence_set: EvidenceSet,
    *,
    draft_bundle_id: str,
    draft_artifact_id: str,
    repair: Callable[[GeneratedVideoDraft], GeneratedVideoDraft] | None = None,
) -> QualityOutcome:
    assessment = _assess(draft, evidence_set)
    repair_attempts = 0
    execution_error: ErrorDetail | None = None
    final_draft = draft

    if (
        assessment.failed_check_ids
        and assessment.failed_check_ids <= _REPAIRABLE_CHECKS
        and repair is not None
    ):
        repair_attempts = 1
        try:
            repaired = repair(draft)
            if not isinstance(repaired, GeneratedVideoDraft):
                raise TypeError("repair returned an invalid draft")
            final_draft = repaired
            assessment = _assess(repaired, evidence_set)
        except Exception:
            execution_error = ErrorDetail(
                code="quality_repair_failed",
                category=ErrorCategory.RECIPE_FAILED,
                message="Draft quality repair failed",
            )

    report = _make_report(
        assessment,
        draft_bundle_id=draft_bundle_id,
        draft_artifact_id=draft_artifact_id,
        repair_attempts=repair_attempts,
    )
    publish_eligible = (
        report.overall is not QualityOverall.FAIL and execution_error is None
    )
    return QualityOutcome(
        final_draft=assessment.draft_bytes,
        report=report,
        overall=report.overall,
        publish_eligible=publish_eligible,
        repair_attempts=repair_attempts,
        execution_error=execution_error,
    )

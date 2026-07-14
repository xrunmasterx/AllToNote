from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from app.core.domain.ids import sha256_digest
from app.core.domain.video import TranscriptDocument, TranscriptSegment
from app.core.errors import DomainError, ErrorCategory
from app.core.portable.artifacts import (
    PortableArtifactRef,
    build_transcript,
    require_bundle_id,
    require_revision_id,
)
from app.core.portable.jsonio import encode_ndjson


_EVIDENCE_ID = re.compile(
    r"ev_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_VALID_SEGMENT_CITATION = re.compile(r"(?<!\\)\[\^(seg_[0-9]{6,})\]")
_ANY_SEGMENT_CITATION = re.compile(r"(?<!\\)\[\^(seg_[^\]\r\n]*)\]")
_SEGMENT_CITATION_PREFIX = re.compile(r"(?<!\\)\[\^seg_")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


@dataclass(frozen=True)
class EvidenceSet:
    payload: bytes
    citation_map: Mapping[str, str]
    transcript: TranscriptDocument
    source_revision_id: str
    target_artifact_ref: PortableArtifactRef

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", bytes(self.payload))
        object.__setattr__(
            self,
            "citation_map",
            MappingProxyType(dict(self.citation_map)),
        )

    @property
    def segments(self) -> tuple[TranscriptSegment, ...]:
        return self.transcript.segments

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(self.citation_map[segment.segment_id] for segment in self.segments)


def _evidence_error(code: str, message: str) -> DomainError:
    return DomainError(code, ErrorCategory.INVALID_REQUEST, message)


def build_evidence_set(
    bundle_id: str,
    source_revision_id: str,
    transcript_artifact_ref: PortableArtifactRef,
    transcript: TranscriptDocument,
    evidence_ids_by_segment: Mapping[str, str],
) -> EvidenceSet:
    require_bundle_id(bundle_id)
    require_revision_id(source_revision_id)
    if not isinstance(transcript, TranscriptDocument):
        raise _evidence_error(
            "evidence_transcript_invalid",
            "Evidence input must be a transcript document",
        )
    document = TranscriptDocument(
        language=transcript.language,
        segments=transcript.segments,
    )
    citation_map = dict(evidence_ids_by_segment)
    segment_ids = {segment.segment_id for segment in document.segments}
    if set(citation_map) != segment_ids:
        raise _evidence_error(
            "evidence_segment_mapping_incomplete",
            "Evidence IDs must exactly cover transcript segments",
        )
    if any(
        not isinstance(value, str) or _EVIDENCE_ID.fullmatch(value) is None
        for value in citation_map.values()
    ):
        raise _evidence_error(
            "evidence_id_invalid",
            "Evidence ID is invalid",
        )
    if len(citation_map.values()) != len(set(citation_map.values())):
        raise _evidence_error(
            "evidence_id_duplicate",
            "Evidence IDs must be unique",
        )
    if transcript_artifact_ref.bundle_id != bundle_id:
        raise _evidence_error(
            "evidence_target_invalid",
            "Transcript artifact must belong to the evidence Bundle",
        )
    if transcript_artifact_ref.sha256 != sha256_digest(
        build_transcript(
            source_revision_id,
            document.language,
            document.segments,
        )
    ):
        raise _evidence_error(
            "evidence_target_hash_mismatch",
            "Transcript artifact digest does not match the transcript",
        )
    records: list[dict[str, object]] = [
        {
            "record_type": "evidence_set_header",
            "evidence_set_schema_version": 1,
            "bundle_id": bundle_id,
            "record_count": len(document.segments),
        }
    ]
    records.extend(
        {
            "evidence_ref_schema_version": 1,
            "evidence_id": citation_map[segment.segment_id],
            "source_revision_ref": {
                "bundle_id": bundle_id,
                "source_revision_id": source_revision_id,
            },
            "target_artifact_ref": {
                "bundle_id": transcript_artifact_ref.bundle_id,
                "artifact_id": transcript_artifact_ref.artifact_id,
                "sha256": transcript_artifact_ref.sha256,
            },
            "locator": {
                "scheme": "video-time-range.v1",
                "start_ms": segment.start_ms,
                "end_ms": segment.end_ms,
            },
            "excerpt_sha256": sha256_digest(segment.text),
            "extensions": {},
        }
        for segment in document.segments
    )
    return EvidenceSet(
        payload=encode_ndjson(records),
        citation_map=citation_map,
        transcript=document,
        source_revision_id=source_revision_id,
        target_artifact_ref=transcript_artifact_ref,
    )


def _rewrite_visible_text(text: str, citation_map: Mapping[str, str]) -> str:
    for match in _ANY_SEGMENT_CITATION.finditer(text):
        if _VALID_SEGMENT_CITATION.fullmatch(match.group(0)) is None:
            raise _evidence_error(
                "draft_segment_citation_invalid",
                "Draft contains an invalid transcript citation",
            )

    def replace(match: re.Match[str]) -> str:
        evidence_id = citation_map.get(match.group(1))
        if evidence_id is None:
            raise _evidence_error(
                "draft_segment_citation_invalid",
                "Draft references an unknown transcript segment",
            )
        return f"[^{evidence_id}]"

    rewritten = _VALID_SEGMENT_CITATION.sub(replace, text)
    if _SEGMENT_CITATION_PREFIX.search(rewritten) is not None:
        raise _evidence_error(
            "draft_segment_citation_invalid",
            "Draft contains a malformed transcript citation",
        )
    return rewritten


def _rewrite_inline_code(line: str, citation_map: Mapping[str, str]) -> str:
    output: list[str] = []
    cursor = 0
    while cursor < len(line):
        marker = line.find("`", cursor)
        if marker < 0:
            output.append(_rewrite_visible_text(line[cursor:], citation_map))
            break
        run_end = marker
        while run_end < len(line) and line[run_end] == "`":
            run_end += 1
        marker_text = line[marker:run_end]
        closing = line.find(marker_text, run_end)
        if closing < 0:
            output.append(_rewrite_visible_text(line[cursor:], citation_map))
            break
        output.append(_rewrite_visible_text(line[cursor:marker], citation_map))
        closing_end = closing + len(marker_text)
        output.append(line[marker:closing_end])
        cursor = closing_end
    return "".join(output)


def rewrite_segment_citations(
    markdown: str,
    evidence_ids_by_segment: Mapping[str, str],
) -> str:
    if not isinstance(markdown, str):
        raise _evidence_error(
            "draft_segment_citation_invalid",
            "Draft markdown must be text",
        )
    citation_map = dict(evidence_ids_by_segment)
    if any(
        not isinstance(value, str) or _EVIDENCE_ID.fullmatch(value) is None
        for value in citation_map.values()
    ):
        raise _evidence_error(
            "draft_segment_citation_invalid",
            "Evidence citation mapping is invalid",
        )

    output: list[str] = []
    fence_marker: str | None = None
    for line in markdown.splitlines(keepends=True):
        fence = _FENCE.match(line)
        if fence_marker is not None:
            output.append(line)
            if fence is not None and fence.group(1)[0] == fence_marker[0] and len(
                fence.group(1)
            ) >= len(fence_marker):
                fence_marker = None
            continue
        if fence is not None:
            fence_marker = fence.group(1)
            output.append(line)
            continue
        if line.startswith(("    ", "\t")):
            output.append(line)
            continue
        output.append(_rewrite_inline_code(line, citation_map))
    if not markdown:
        return ""
    return "".join(output)

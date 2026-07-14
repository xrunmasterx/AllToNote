from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.core.domain.video import TranscriptDocument, TranscriptSegment
from app.core.errors import DomainError, ErrorCategory
from app.core.portable.jsonio import encode_ndjson


_BUNDLE_ID = re.compile(
    r"bnd_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_REVISION_ID = re.compile(
    r"rev_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_ARTIFACT_ID = re.compile(
    r"art_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _invalid_reference() -> DomainError:
    return DomainError(
        "portable_artifact_reference_invalid",
        ErrorCategory.INVALID_REQUEST,
        "Portable artifact reference is invalid",
    )


def require_bundle_id(value: str) -> None:
    if not isinstance(value, str) or _BUNDLE_ID.fullmatch(value) is None:
        raise _invalid_reference()


def require_revision_id(value: str) -> None:
    if not isinstance(value, str) or _REVISION_ID.fullmatch(value) is None:
        raise _invalid_reference()


@dataclass(frozen=True)
class PortableArtifactRef:
    bundle_id: str
    artifact_id: str
    sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.bundle_id, str)
            or _BUNDLE_ID.fullmatch(self.bundle_id) is None
            or not isinstance(self.artifact_id, str)
            or _ARTIFACT_ID.fullmatch(self.artifact_id) is None
            or not isinstance(self.sha256, str)
            or _DIGEST.fullmatch(self.sha256) is None
        ):
            raise _invalid_reference()


def build_transcript(
    source_revision_id: str,
    language: str,
    segments: Sequence[TranscriptSegment],
) -> bytes:
    require_revision_id(source_revision_id)
    try:
        segment_snapshot = tuple(segments)
    except MemoryError:
        raise
    except Exception:
        raise DomainError(
            "transcript_invalid",
            ErrorCategory.INVALID_REQUEST,
            "Transcript input is invalid",
        ) from None
    document = TranscriptDocument(language=language, segments=segment_snapshot)
    records: list[dict[str, object]] = [
        {
            "record_type": "transcript_header",
            "transcript_schema_version": 1,
            "source_revision_id": source_revision_id,
            "time_base": "millisecond",
            "language": document.language,
        }
    ]
    records.extend(
        {
            "record_type": "segment",
            "segment_id": segment.segment_id,
            "start_ms": segment.start_ms,
            "end_ms": segment.end_ms,
            "text": segment.text,
        }
        for segment in document.segments
    )
    return encode_ndjson(records)

from __future__ import annotations

import json

from app.core.domain.ids import sha256_digest
from app.core.domain.video import TranscriptDocument
from app.core.errors import DomainError, ErrorCategory


def transcript_sha256(transcript: TranscriptDocument) -> str:
    """Return the canonical identity of an immutable Core Transcript."""

    if not isinstance(transcript, TranscriptDocument):
        raise DomainError(
            "transcript_identity_invalid",
            ErrorCategory.INVALID_REQUEST,
            "Transcript identity requires the Core transcript contract",
        )
    return sha256_digest(
        json.dumps(
            {
                "language": transcript.language,
                "segments": [
                    {
                        "end_ms": segment.end_ms,
                        "segment_id": segment.segment_id,
                        "start_ms": segment.start_ms,
                        "text": segment.text,
                    }
                    for segment in transcript.segments
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


__all__ = ["transcript_sha256"]

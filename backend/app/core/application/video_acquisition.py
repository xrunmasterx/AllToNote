from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from app.core.domain.ids import sha256_digest
from app.core.domain.video import TranscriptDocument
from app.core.errors import DomainError, ErrorCategory
from app.core.portable.bundle_assembler import VideoSourceMetadata
from app.core.ports.source import SubtitleAvailability


class TranscriptProvenance(StrEnum):
    PROVIDED = "provided"
    PLATFORM = "platform"
    GENERATED = "generated"


def transcript_identity(transcript: TranscriptDocument) -> str:
    if not isinstance(transcript, TranscriptDocument):
        raise DomainError(
            "video_acquisition_invalid",
            ErrorCategory.INVALID_REQUEST,
            "Acquisition transcript must use the Core transcript contract",
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


@dataclass(frozen=True)
class VideoAcquisition:
    metadata: VideoSourceMetadata
    subtitle_availability: SubtitleAvailability
    transcript: TranscriptDocument | None
    transcript_identity: str | None
    transcript_provenance: TranscriptProvenance | None

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, VideoSourceMetadata) or not isinstance(
            self.subtitle_availability, SubtitleAvailability
        ):
            raise DomainError(
                "video_acquisition_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Video acquisition metadata is invalid",
            )
        if self.transcript is None:
            if (
                self.transcript_identity is not None
                or self.transcript_provenance is not None
            ):
                raise DomainError(
                    "video_acquisition_invalid",
                    ErrorCategory.INVALID_REQUEST,
                    "Missing acquisition transcript cannot have an identity",
                )
            if self.subtitle_availability is SubtitleAvailability.AVAILABLE:
                raise DomainError(
                    "video_acquisition_invalid",
                    ErrorCategory.INVALID_REQUEST,
                    "Available subtitles require a canonical transcript",
                )
            return
        if (
            self.subtitle_availability is not SubtitleAvailability.AVAILABLE
            or self.transcript_identity != transcript_identity(self.transcript)
            or not isinstance(self.transcript_provenance, TranscriptProvenance)
            or self.metadata.subtitle_acquisition != self.transcript_provenance.value
        ):
            raise DomainError(
                "video_acquisition_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Acquisition transcript identity is invalid",
            )


__all__ = [
    "TranscriptProvenance",
    "VideoAcquisition",
    "transcript_identity",
]

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from app.core.domain.video import TranscriptDocument
from app.core.errors import DomainError, ErrorCategory
from app.core.ports.source import CancellationTokenPort


@dataclass(frozen=True)
class MediaInput:
    media_path: Path | None = field(default=None, repr=False)
    provided_transcript: TranscriptDocument | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.media_path is not None and not isinstance(self.media_path, Path):
            raise DomainError(
                "transcript_media_path_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Transcript media path must be a Path",
            )
        if self.provided_transcript is not None and not isinstance(
            self.provided_transcript, TranscriptDocument
        ):
            raise DomainError(
                "provided_transcript_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Provided transcript must use the Core transcript contract",
            )
        if self.media_path is None and self.provided_transcript is None:
            raise DomainError(
                "transcript_input_missing",
                ErrorCategory.INVALID_REQUEST,
                "Transcript input requires media or a normalized transcript",
            )


class TranscriptPort(Protocol):
    """Boundary for producing the canonical transcript timeline."""

    def transcribe(
        self,
        media: MediaInput,
        token: CancellationTokenPort,
    ) -> TranscriptDocument: ...


__all__ = ["MediaInput", "TranscriptPort"]

from __future__ import annotations

from dataclasses import dataclass

from app.core.domain.video import TranscriptSegment
from app.core.errors import DomainError, ErrorCategory
from app.core.ports.model import KnowledgeModelRequest
from app.core.recipes.video.prompt import (
    measure_video_prompt_segment,
    video_prompt_fixed_bytes,
)


@dataclass(frozen=True)
class TranscriptChunk:
    ordinal: int
    segments: tuple[TranscriptSegment, ...]
    encoded_bytes: int


@dataclass(frozen=True)
class TranscriptChunkPlan:
    chunks: tuple[TranscriptChunk, ...]
    max_prompt_bytes: int
    segment_visits: int
    encoded_bytes: int
    peak_chunk_bytes: int


def plan_transcript_chunks(
    request: KnowledgeModelRequest,
    *,
    max_prompt_bytes: int,
) -> TranscriptChunkPlan:
    """Partition once at segment boundaries using complete prompt byte size."""

    if not isinstance(request, KnowledgeModelRequest):
        raise DomainError(
            "model_chunk_input_invalid",
            ErrorCategory.INVALID_REQUEST,
            "Chunk input must be a knowledge model request",
        )
    if (
        isinstance(max_prompt_bytes, bool)
        or not isinstance(max_prompt_bytes, int)
        or max_prompt_bytes < 1
    ):
        raise DomainError(
            "model_chunk_budget_invalid",
            ErrorCategory.INVALID_REQUEST,
            "Chunk byte budget must be a positive integer",
        )

    chunks: list[TranscriptChunk] = []
    current: list[TranscriptSegment] = []
    fixed_bytes = video_prompt_fixed_bytes(request)
    current_bytes = fixed_bytes
    peak_bytes = 0
    visits = 0
    for segment in request.transcript.segments:
        visits += 1
        measured = measure_video_prompt_segment(segment)
        if fixed_bytes + measured.first_bytes > max_prompt_bytes:
            raise DomainError(
                "model_segment_too_large",
                ErrorCategory.RECIPE_FAILED,
                "A transcript segment exceeds the model chunk byte budget",
                {"segment_id": segment.segment_id},
            )
        next_bytes = measured.first_bytes if not current else measured.continuation_bytes
        if current and current_bytes + next_bytes > max_prompt_bytes:
            chunks.append(
                TranscriptChunk(len(chunks), tuple(current), current_bytes)
            )
            peak_bytes = max(peak_bytes, current_bytes)
            current = []
            current_bytes = fixed_bytes
            next_bytes = measured.first_bytes
        current.append(segment)
        current_bytes += next_bytes
    if current:
        chunks.append(TranscriptChunk(len(chunks), tuple(current), current_bytes))
        peak_bytes = max(peak_bytes, current_bytes)
    return TranscriptChunkPlan(
        chunks=tuple(chunks),
        max_prompt_bytes=max_prompt_bytes,
        segment_visits=visits,
        encoded_bytes=sum(chunk.encoded_bytes for chunk in chunks),
        peak_chunk_bytes=peak_bytes,
    )


__all__ = ["TranscriptChunk", "TranscriptChunkPlan", "plan_transcript_chunks"]

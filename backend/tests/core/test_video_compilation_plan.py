from __future__ import annotations

from dataclasses import asdict, replace

import pytest

from app.core.application.video_acquisition import transcript_identity
from app.core.domain.video import TranscriptDocument, TranscriptSegment
from app.core.errors import DomainError
from app.core.ports.model_executor import ModelExecutionBinding
from app.core.recipes.video.compilation.contracts import (
    CompilationQualityProfile,
    CompilationTopology,
    TranscriptBasis,
    TranscriptCheckStatus,
    TranscriptQualityInputV1,
    TranscriptQualityStatus,
    VideoCompilationPlanningRequestV1,
)
from app.core.recipes.video.compilation.pipeline import (
    assess_transcript_quality,
    plan_video_compilation,
)


def _transcript(
    count: int = 4,
    *,
    text: str = "A useful lesson segment.",
    gap_ms: int = 0,
) -> TranscriptDocument:
    segments = []
    cursor = 0
    for ordinal in range(count):
        segments.append(
            TranscriptSegment(
                f"seg_{ordinal + 1:06d}",
                cursor,
                cursor + 1_000,
                f"{text} {ordinal}",
            )
        )
        cursor += 1_000 + gap_ms
    return TranscriptDocument("en", tuple(segments))


def _binding(**changes: object) -> ModelExecutionBinding:
    binding = ModelExecutionBinding(
        schema_version=1,
        provider_type="openai-compatible",
        model_identity="provider/model-v1",
        credential_profile_ref="profile-default",
        context_window_tokens=4_096,
        max_output_tokens=1_024,
        max_concurrency=4,
        supports_structured_output=True,
        supports_temperature=True,
        timeout_seconds=60,
    )
    return replace(binding, **changes)


def _quality_input(
    transcript: TranscriptDocument,
    **changes: object,
) -> TranscriptQualityInputV1:
    value = TranscriptQualityInputV1(
        schema_version=1,
        transcript=transcript,
        transcript_basis=TranscriptBasis.PLATFORM_CAPTION,
        source_duration_ms=transcript.segments[-1].end_ms,
        detected_languages=(transcript.language,),
    )
    return replace(value, **changes)


def _planning_request(
    transcript: TranscriptDocument,
    **changes: object,
) -> VideoCompilationPlanningRequestV1:
    assessment = assess_transcript_quality(_quality_input(transcript))
    value = VideoCompilationPlanningRequestV1(
        schema_version=1,
        recipe_id="alltonote.video-course-note",
        recipe_version=2,
        quality_profile=CompilationQualityProfile.BALANCED,
        transcript=transcript,
        transcript_quality=assessment,
        model_binding=_binding(),
        stage_id="knowledge-map",
        stage_version=1,
        prompt_id="knowledge-map-balanced",
        prompt_version=1,
        prompt_overhead_tokens=128,
        prompt_overhead_bytes=512,
        reserved_output_tokens=1_024,
        max_request_bytes=16_384,
        max_chunk_duration_ms=10 * 60 * 1_000,
        estimated_map_output_tokens_per_chunk=256,
        map_output_byte_budget_per_chunk=1_024,
        max_repair_attempts=1,
    )
    return replace(value, **changes)


def _check_status(assessment: object, check_id: str) -> TranscriptCheckStatus:
    checks = getattr(assessment, "checks")
    return next(check.status for check in checks if check.check_id == check_id)


def test_quality_assessment_binds_exact_transcript_and_known_duration() -> None:
    transcript = _transcript()
    assessment = assess_transcript_quality(_quality_input(transcript))

    assert assessment.schema_version == 1
    assert assessment.transcript_sha256 == transcript_identity(transcript)
    assert assessment.status is TranscriptQualityStatus.PASS
    assert assessment.transcript_start_ms == 0
    assert assessment.transcript_end_ms == 4_000
    assert assessment.coverage_ratio == 1.0
    assert assessment.duration_known is True
    assert assessment.confidence_available is False
    assert assessment.confidence_summary is None


def test_transcript_hash_changes_with_every_authoritative_field() -> None:
    base = _transcript(count=1)
    segment = base.segments[0]
    variants = (
        TranscriptDocument("zh-CN", base.segments),
        TranscriptDocument("en", (replace(segment, text="changed"),)),
        TranscriptDocument("en", (replace(segment, end_ms=1_001),)),
        TranscriptDocument("en", (replace(segment, segment_id="seg_999999"),)),
    )
    base_hash = transcript_identity(base)
    assert all(transcript_identity(value) != base_hash for value in variants)


def test_unknown_duration_and_unavailable_confidence_are_explicit() -> None:
    transcript = _transcript()
    assessment = assess_transcript_quality(
        _quality_input(
            transcript,
            source_duration_ms=None,
            detected_languages=(),
        )
    )

    assert assessment.duration_known is False
    assert assessment.source_duration_ms is None
    assert assessment.coverage_ratio is None
    assert assessment.detected_languages == ()
    assert _check_status(assessment, "coverage") is TranscriptCheckStatus.NOT_APPLICABLE
    assert _check_status(assessment, "confidence") is TranscriptCheckStatus.NOT_APPLICABLE


def test_quality_reports_normalization_observations_and_abnormal_gaps() -> None:
    transcript = _transcript(count=3, gap_ms=35_000)
    assessment = assess_transcript_quality(
        _quality_input(
            transcript,
            source_duration_ms=transcript.segments[-1].end_ms,
            empty_segment_count=2,
            out_of_order_count=1,
        )
    )

    assert assessment.empty_segment_count == 2
    assert assessment.out_of_order_count == 1
    assert assessment.abnormal_gap_count == 2
    assert assessment.status is TranscriptQualityStatus.FAIL
    assert "normalization-observations" in assessment.warnings
    assert "abnormal-gaps" in assessment.warnings


def test_language_conflict_and_abnormally_long_segment_are_warnings() -> None:
    transcript = TranscriptDocument(
        "en",
        (TranscriptSegment("seg_000001", 0, 121_000, "long explanation"),),
    )
    assessment = assess_transcript_quality(
        _quality_input(
            transcript,
            detected_languages=("en", "zh-CN"),
        )
    )
    assert assessment.status is TranscriptQualityStatus.WARNING
    assert assessment.abnormal_segment_count == 1
    assert _check_status(assessment, "languages") is TranscriptCheckStatus.WARNING
    assert (
        _check_status(assessment, "abnormal-segments")
        is TranscriptCheckStatus.WARNING
    )


def test_severe_duplicate_overlap_and_low_coverage_are_failed() -> None:
    transcript = TranscriptDocument(
        "en",
        (
            TranscriptSegment("seg_000001", 60_000, 70_000, "repeat"),
            TranscriptSegment("seg_000002", 61_000, 69_000, "repeat"),
            TranscriptSegment("seg_000003", 62_000, 68_000, "repeat"),
            TranscriptSegment("seg_000004", 63_000, 67_000, "repeat"),
        ),
    )
    assessment = assess_transcript_quality(
        _quality_input(transcript, source_duration_ms=300_000)
    )

    assert assessment.status is TranscriptQualityStatus.FAIL
    assert assessment.duplicate_ratio == 0.75
    assert assessment.overlap_issue_count == 3
    assert assessment.coverage_ratio < 0.1
    assert _check_status(assessment, "duplicates") is TranscriptCheckStatus.FAIL
    assert _check_status(assessment, "overlap") is TranscriptCheckStatus.WARNING
    assert _check_status(assessment, "coverage") is TranscriptCheckStatus.FAIL


def test_confidence_summary_never_invents_confidence() -> None:
    transcript = _transcript(count=4)
    assessment = assess_transcript_quality(
        _quality_input(
            transcript,
            segment_confidences=(0.1, 0.2, 0.3, 0.2),
        )
    )

    assert assessment.confidence_available is True
    assert assessment.confidence_summary is not None
    assert assessment.confidence_summary.mean == pytest.approx(0.2)
    assert assessment.confidence_summary.minimum == 0.1
    assert assessment.confidence_summary.low_confidence_count == 4
    assert _check_status(assessment, "confidence") is TranscriptCheckStatus.WARNING


def test_corrupt_subtitle_text_is_failed_before_paid_compilation() -> None:
    transcript = TranscriptDocument(
        "en",
        (TranscriptSegment("seg_000001", 0, 1_000, "\ufffd\ufffd\ufffd"),),
    )
    assessment = assess_transcript_quality(_quality_input(transcript))
    assert assessment.status is TranscriptQualityStatus.FAIL
    assert (
        _check_status(assessment, "text-integrity")
        is TranscriptCheckStatus.FAIL
    )


def test_distinct_rolling_captions_overlap_only_warns() -> None:
    transcript = TranscriptDocument(
        "en",
        tuple(
            TranscriptSegment(
                f"seg_{ordinal + 1:06d}",
                ordinal * 2_000,
                ordinal * 2_000 + 4_000,
                f"distinct caption {ordinal}",
            )
            for ordinal in range(10)
        ),
    )
    assessment = assess_transcript_quality(
        _quality_input(transcript, source_duration_ms=22_000)
    )
    assert assessment.status is TranscriptQualityStatus.WARNING
    assert assessment.duplicate_ratio == 0.0
    assert assessment.overlap_issue_count == 9
    assert _check_status(assessment, "overlap") is TranscriptCheckStatus.WARNING


def test_invalid_raw_structure_is_rejected_before_quality_assessment() -> None:
    first = TranscriptSegment("seg_000001", 0, 1_000, "first")
    later = TranscriptSegment("seg_000002", 1_000, 2_000, "later")
    with pytest.raises(DomainError, match="transcript_empty"):
        TranscriptDocument("en", ())
    with pytest.raises(DomainError, match="transcript_segment_invalid"):
        TranscriptSegment("seg_000003", 2_000, 3_000, "   ")
    with pytest.raises(DomainError, match="transcript_segment_duplicate"):
        TranscriptDocument("en", (first, first))
    with pytest.raises(DomainError, match="transcript_order_invalid"):
        TranscriptDocument("en", (later, first))


def test_non_overlapping_repeated_speech_is_not_a_caption_duplicate() -> None:
    transcript = TranscriptDocument(
        "en",
        (
            TranscriptSegment("seg_000001", 0, 1_000, "repeat for teaching"),
            TranscriptSegment("seg_000002", 1_000, 2_000, "repeat for teaching"),
        ),
    )
    assessment = assess_transcript_quality(_quality_input(transcript))
    assert assessment.duplicate_ratio == 0.0
    assert _check_status(assessment, "duplicates") is TranscriptCheckStatus.PASS


def test_short_transcript_uses_direct_plan_without_copying_text() -> None:
    transcript = _transcript(count=3, text="private transcript phrase")
    plan = plan_video_compilation(_planning_request(transcript))

    assert plan.topology is CompilationTopology.DIRECT
    assert plan.expected_sequential_model_waves == 1
    assert plan.token_estimator_id == "utf8-byte-upper-bound-v1"
    assert len(plan.transcript_chunks) == 1
    chunk = plan.transcript_chunks[0]
    assert chunk.ordinal == 0
    assert chunk.start_segment_ordinal == 0
    assert chunk.end_segment_ordinal_exclusive == 3
    assert chunk.start_segment_id == "seg_000001"
    assert chunk.end_segment_id == "seg_000003"
    assert "private transcript phrase" not in repr(asdict(plan))
    assert not hasattr(chunk, "segments")


def test_unknown_source_duration_does_not_change_chunk_boundaries() -> None:
    transcript = _transcript(count=8, text="same immutable transcript")
    known_request = _planning_request(transcript)
    unknown_quality = assess_transcript_quality(
        _quality_input(transcript, source_duration_ms=None)
    )
    unknown_request = replace(
        known_request,
        transcript_quality=unknown_quality,
    )
    assert plan_video_compilation(known_request) == plan_video_compilation(
        unknown_request
    )


def test_long_transcript_has_deterministic_contiguous_chunk_refs() -> None:
    transcript = _transcript(count=30, text="x" * 420)
    request = _planning_request(
        transcript,
        model_binding=_binding(
            context_window_tokens=2_048,
            max_output_tokens=512,
        ),
        reserved_output_tokens=512,
        max_request_bytes=4_000,
        prompt_overhead_tokens=128,
        prompt_overhead_bytes=400,
        estimated_map_output_tokens_per_chunk=64,
        map_output_byte_budget_per_chunk=128,
    )
    first = plan_video_compilation(request)
    second = plan_video_compilation(request)

    assert first == second
    assert first.topology is CompilationTopology.MAP_COMPOSE
    assert first.expected_sequential_model_waves == 2
    assert len(first.transcript_chunks) > 1
    assert [chunk.ordinal for chunk in first.transcript_chunks] == list(
        range(len(first.transcript_chunks))
    )
    assert first.transcript_chunks[0].start_segment_ordinal == 0
    assert first.transcript_chunks[-1].end_segment_ordinal_exclusive == 30
    for previous, current in zip(
        first.transcript_chunks, first.transcript_chunks[1:]
    ):
        assert previous.end_segment_ordinal_exclusive == current.start_segment_ordinal
    assert all(
        chunk.estimated_input_tokens <= first.chunk_input_token_budget
        and chunk.encoded_input_bytes <= first.chunk_input_byte_budget
        for chunk in first.transcript_chunks
    )


def test_balanced_plan_caps_chunk_duration_independently_of_context_capacity() -> None:
    transcript = TranscriptDocument(
        "en",
        tuple(
            TranscriptSegment(
                f"seg_{ordinal + 1:06d}",
                ordinal * 60_000,
                (ordinal + 1) * 60_000,
                f"continuous lesson {ordinal}",
            )
            for ordinal in range(40)
        ),
    )
    request = _planning_request(
        transcript,
        model_binding=_binding(
            context_window_tokens=128_000,
            max_output_tokens=16_000,
        ),
        reserved_output_tokens=16_000,
        max_request_bytes=128_000,
        max_chunk_duration_ms=10 * 60 * 1_000,
    )

    plan = plan_video_compilation(request)

    assert len(plan.transcript_chunks) > 1
    assert all(
        chunk.end_ms - chunk.start_ms <= 10 * 60 * 1_000
        for chunk in plan.transcript_chunks
    )
    assert sum(chunk.segment_count for chunk in plan.transcript_chunks) == 40


def test_super_long_transcript_selects_progress_guaranteed_hierarchy() -> None:
    transcript = _transcript(count=240, text="y" * 480)
    request = _planning_request(
        transcript,
        model_binding=_binding(
            context_window_tokens=2_048,
            max_output_tokens=512,
            max_concurrency=3,
        ),
        reserved_output_tokens=512,
        max_request_bytes=3_000,
        prompt_overhead_tokens=128,
        prompt_overhead_bytes=400,
        estimated_map_output_tokens_per_chunk=256,
    )
    plan = plan_video_compilation(request)

    assert plan.topology is CompilationTopology.HIERARCHICAL_COMPOSE
    assert plan.expected_sequential_model_waves > 2
    assert plan.extraction_concurrency == 3
    assert len(plan.transcript_chunks) > 20


def test_chunk_planning_visits_each_segment_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.recipes.video.compilation import pipeline

    original = pipeline.estimate_segment_input_cost
    visits = 0

    def counting(segment: TranscriptSegment) -> tuple[int, int]:
        nonlocal visits
        visits += 1
        return original(segment)

    monkeypatch.setattr(pipeline, "estimate_segment_input_cost", counting)
    transcript = _transcript(count=1_000, text="linear")
    plan_video_compilation(_planning_request(transcript))
    assert visits == 1_000


def test_plan_rejects_transcript_hash_mismatch_and_oversized_segment() -> None:
    transcript = _transcript(count=2)
    with pytest.raises(DomainError, match="video_compilation_contract_invalid"):
        _planning_request(
            transcript,
            reserved_output_tokens=1,
            estimated_map_output_tokens_per_chunk=2,
        )
    other_assessment = assess_transcript_quality(_quality_input(_transcript(count=3)))
    with pytest.raises(DomainError, match="transcript_assessment_mismatch"):
        plan_video_compilation(
            _planning_request(transcript, transcript_quality=other_assessment)
        )

    corrupt = TranscriptDocument(
        "en",
        (TranscriptSegment("seg_000001", 0, 1_000, "\ufffd\ufffd\ufffd"),),
    )
    corrupt_quality = assess_transcript_quality(_quality_input(corrupt))
    with pytest.raises(DomainError, match="transcript_quality_failed"):
        plan_video_compilation(
            _planning_request(corrupt, transcript_quality=corrupt_quality)
        )

    huge = _transcript(count=1, text="z" * 20_000)
    budget_variants = (
        {
            "model_binding": _binding(
                context_window_tokens=1_024,
                max_output_tokens=256,
            ),
            "reserved_output_tokens": 256,
            "max_request_bytes": 100_000,
        },
        {
            "model_binding": _binding(
                context_window_tokens=65_536,
                max_output_tokens=1_024,
            ),
            "reserved_output_tokens": 1_024,
            "max_request_bytes": 2_000,
        },
    )
    for changes in budget_variants:
        with pytest.raises(DomainError, match="transcript_segment_budget_exceeded"):
            plan_video_compilation(
                _planning_request(
                    huge,
                    prompt_overhead_tokens=64,
                    prompt_overhead_bytes=200,
                    **changes,
                )
            )

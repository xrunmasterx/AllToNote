from __future__ import annotations

from dataclasses import replace

import pytest

import app.core.application.video_service as video_service
from app.core.domain.video import (
    GeneratedVideoDraft,
    ScreenshotPolicy,
    ScreenshotRequest,
    TranscriptDocument,
    TranscriptSegment,
)
from app.core.errors import DomainError


JOB_ID = "job_018cc251-f400-7000-8000-000000000000"
TRANSCRIPT = TranscriptDocument(
    language="en",
    segments=(
        TranscriptSegment("seg_000001", 1_000, 2_000, "first"),
        TranscriptSegment("seg_000002", 2_500, 4_000, "second"),
    ),
)


def _draft(*requests: ScreenshotRequest) -> GeneratedVideoDraft:
    return GeneratedVideoDraft(
        markdown="# Note\n",
        cited_segment_ids=(),
        screenshot_requests=requests,
        model_identity="fixture/model-v1",
        usage={},
        warnings=(),
    )


def _build(
    policy: ScreenshotPolicy,
    *requests: ScreenshotRequest,
) -> tuple[object, ...]:
    builder = getattr(video_service, "build_screenshot_plan", None)
    assert callable(builder), "Core screenshot plan builder is missing"
    return builder(JOB_ID, policy, _draft(*requests), TRANSCRIPT)


def test_half_open_offsets_accept_first_and_last_millisecond_in_model_order() -> None:
    plan = _build(
        ScreenshotPolicy.ON_DEMAND,
        ScreenshotRequest("seg_000002", 0),
        ScreenshotRequest("seg_000001", 999),
    )

    assert [(item.ordinal, item.segment_id, item.timestamp_ms) for item in plan] == [
        (0, "seg_000002", 2_500),
        (1, "seg_000001", 1_999),
    ]
    assert all(item.segment_start_ms <= item.timestamp_ms < item.segment_end_ms for item in plan)
    assert [item.relative_path for item in plan] == [
        f"assets/{plan[0].artifact_id}.webp",
        f"assets/{plan[1].artifact_id}.webp",
    ]


@pytest.mark.parametrize(
    "screenshot_request",
    (
        ScreenshotRequest("seg_000001", -1),
        ScreenshotRequest("seg_000001", 1_000),
        ScreenshotRequest("seg_999999", 0),
    ),
)
def test_invalid_half_open_or_unknown_request_is_rejected(
    screenshot_request: ScreenshotRequest,
) -> None:
    with pytest.raises(DomainError, match="screenshot_request_invalid"):
        _build(ScreenshotPolicy.ON_DEMAND, screenshot_request)


def test_bool_offset_is_rejected_by_the_raw_request_contract() -> None:
    with pytest.raises(DomainError, match="screenshot_request_invalid"):
        ScreenshotRequest("seg_000001", True)


def test_duplicate_requests_are_rejected_explicitly() -> None:
    with pytest.raises(DomainError, match="screenshot_request_duplicate"):
        _build(
            ScreenshotPolicy.ON_DEMAND,
            ScreenshotRequest("seg_000001", 5),
            ScreenshotRequest("seg_000001", 5),
        )


def test_off_policy_rejects_model_work_but_empty_policies_return_empty_plan() -> None:
    assert _build(ScreenshotPolicy.OFF) == ()
    assert _build(ScreenshotPolicy.ON_DEMAND) == ()

    with pytest.raises(DomainError, match="screenshot_request_not_allowed"):
        _build(ScreenshotPolicy.OFF, ScreenshotRequest("seg_000001", 0))


def test_plan_ids_and_paths_are_stable_for_job_and_ordinal() -> None:
    draft = _draft(
        ScreenshotRequest("seg_000002", 12),
        ScreenshotRequest("seg_000001", 34),
    )
    builder = getattr(video_service, "build_screenshot_plan", None)
    assert callable(builder), "Core screenshot plan builder is missing"

    first = builder(JOB_ID, ScreenshotPolicy.ON_DEMAND, draft, TRANSCRIPT)
    replayed = builder(JOB_ID, ScreenshotPolicy.ON_DEMAND, replace(draft), TRANSCRIPT)

    assert first == replayed
    assert len({item.artifact_id for item in first}) == 2
    assert all(item.artifact_id.startswith("art_") for item in first)

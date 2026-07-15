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
from app.core.portable.bundle_assembler import DisplayAssetInput


JOB_ID = "job_018cc251-f400-7000-8000-000000000000"
TRANSCRIPT = TranscriptDocument(
    language="en",
    segments=(
        TranscriptSegment("seg_000001", 1_000, 2_000, "first"),
        TranscriptSegment("seg_000002", 2_500, 4_000, "second"),
    ),
)
VALID_WEBP = bytes.fromhex(
    "524946461a000000574542505650384c0d0000002f00000010071011118888fe0700"
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


def _bind(
    draft: GeneratedVideoDraft,
    plan: tuple[object, ...],
    assets: tuple[object, ...],
) -> GeneratedVideoDraft:
    binder = getattr(video_service, "bind_screenshot_assets", None)
    assert callable(binder), "Core screenshot asset binder is missing"
    return binder(draft, plan, assets)


def _asset(item: object) -> DisplayAssetInput:
    return DisplayAssetInput(
        artifact_id=item.artifact_id,
        relative_path=item.relative_path,
        media_type="image/webp",
        payload=VALID_WEBP,
    )


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


def test_empty_plan_and_assets_return_the_exact_original_draft() -> None:
    draft = _draft()

    linked = _bind(draft, (), ())

    assert linked is draft
    assert linked.markdown.encode() == draft.markdown.encode()


@pytest.mark.parametrize(
    ("requests", "expected_section"),
    (
        (
            (ScreenshotRequest("seg_000001", 0),),
            "## Screenshots\n\n"
            "![Video screenshot 1 at 00:01.000]({first})\n",
        ),
        (
            (
                ScreenshotRequest("seg_000002", 0),
                ScreenshotRequest("seg_000001", 999),
            ),
            "## Screenshots\n\n"
            "![Video screenshot 1 at 00:02.500]({first})\n\n"
            "![Video screenshot 2 at 00:01.999]({second})\n",
        ),
    ),
)
def test_assets_are_linked_in_plan_order_with_exact_portable_section(
    requests: tuple[ScreenshotRequest, ...],
    expected_section: str,
) -> None:
    draft = _draft(*requests)
    plan = _build(ScreenshotPolicy.ON_DEMAND, *requests)
    assets = tuple(_asset(item) for item in plan)

    linked = _bind(draft, plan, assets)

    destinations = [f"../{item.relative_path}" for item in plan]
    expected = expected_section.format(
        first=destinations[0],
        second=destinations[-1],
    )
    assert linked.markdown == "# Note\n\n" + expected
    assert linked.markdown.endswith("\n")
    assert not linked.markdown.endswith("\n\n")
    assert linked.screenshot_requests == ()
    assert draft.screenshot_requests == requests


def _binding_error(
    draft: GeneratedVideoDraft,
    plan: tuple[object, ...],
    assets: tuple[object, ...],
) -> DomainError:
    with pytest.raises(DomainError) as raised:
        _bind(draft, plan, assets)
    return raised.value


def test_screenshot_binding_rejects_wrong_count_without_private_data() -> None:
    draft = _draft(ScreenshotRequest("seg_000001", 0))
    plan = _build(ScreenshotPolicy.ON_DEMAND, *draft.screenshot_requests)

    error = _binding_error(draft, plan, ())

    assert error.code == "screenshot_asset_binding_invalid"
    assert error.category.value == "recipe_failed"
    assert error.message == "Screenshot asset binding is invalid"
    assert dict(error.details) == {}
    assert plan[0].artifact_id not in str(error)
    assert plan[0].relative_path not in str(error)


@pytest.mark.parametrize("mutation", ("reordered", "wrong_id", "wrong_path", "non_asset"))
def test_screenshot_binding_rejects_pairwise_mismatch_without_leaks(
    mutation: str,
) -> None:
    requests = (
        ScreenshotRequest("seg_000001", 0),
        ScreenshotRequest("seg_000002", 0),
    )
    draft = _draft(*requests)
    plan = _build(ScreenshotPolicy.ON_DEMAND, *requests)
    assets: tuple[object, ...] = tuple(_asset(item) for item in plan)
    private_values = [plan[0].artifact_id, plan[0].relative_path, "private-checkpoint"]
    if mutation == "reordered":
        assets = tuple(reversed(assets))
    elif mutation == "wrong_id":
        wrong_id = "art_018f0000-0000-7000-8000-000000000999"
        assets = (
            DisplayAssetInput(
                wrong_id,
                f"assets/{wrong_id}.webp",
                "image/webp",
                VALID_WEBP,
            ),
            assets[1],
        )
        private_values.append(wrong_id)
    elif mutation == "wrong_path":
        assets = (
            DisplayAssetInput(
                plan[0].artifact_id,
                "assets/private-checkpoint.webp",
                "image/webp",
                VALID_WEBP,
            ),
            assets[1],
        )
    else:
        assets = (b"private-checkpoint", assets[1])

    error = _binding_error(draft, plan, assets)

    assert error.code == "screenshot_asset_binding_invalid"
    assert error.message == "Screenshot asset binding is invalid"
    assert dict(error.details) == {}
    assert all(value not in str(error) for value in private_values)


def test_empty_plan_rejects_an_unexpected_asset() -> None:
    unexpected_id = "art_018f0000-0000-7000-8000-000000000999"
    unexpected = DisplayAssetInput(
        unexpected_id,
        f"assets/{unexpected_id}.webp",
        "image/webp",
        VALID_WEBP,
    )

    error = _binding_error(_draft(), (), (unexpected,))

    assert error.code == "screenshot_asset_binding_invalid"
    assert unexpected_id not in str(error)

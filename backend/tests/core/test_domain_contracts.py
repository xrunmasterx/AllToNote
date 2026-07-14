from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import get_args
from uuid import UUID

import pytest

from app.core.domain.ids import new_typed_id, sha256_digest, utc_now_millis
from app.core.domain.video import (
    GeneratedVideoDraft,
    JobSnapshot,
    JobState,
    QualityOverall,
    RetryJobRequest,
    ScreenshotPolicy,
    ScreenshotRequest,
    TranscriptDocument,
    TranscriptSegment,
    VideoProduceRequest,
    VideoProduceResult,
)
from app.core.errors import DomainError, ErrorCategory, ErrorDetail
from app.core.ports.credentials import CredentialBrokerPort
from app.core.ports.events import EventSink
from app.core.ports.jobs import AttemptStoragePort, JobRepositoryPort
from app.core.ports.model import KnowledgeModelPort
from app.core.ports.portable import PortableWorkspacePort
from app.core.ports.screenshot import ScreenshotPort
from app.core.ports.source import VideoSourcePort
from app.core.ports.transcript import TranscriptPort


ALLOWED_ID_PREFIXES = {
    "job",
    "run",
    "att",
    "evt",
    "chl",
    "corr",
    "op",
    "bnd",
    "src",
    "rev",
    "art",
    "ev",
}


def test_typed_id_is_deterministic_uuid7_with_stable_prefix() -> None:
    value = new_typed_id("bnd", now_ms=1_721_000_000_000, randomness=b"\x00" * 10)

    parsed = UUID(value[4:])
    assert value == "bnd_0190b397-fa00-7000-8000-000000000000"
    assert parsed.version == 7
    assert parsed.variant == "specified in RFC 4122"
    assert int.from_bytes(parsed.bytes[:6], "big") == 1_721_000_000_000


def test_typed_id_nonzero_vector_freezes_uuid7_random_bit_layout() -> None:
    randomness = bytes.fromhex("123456789abcdef00100")

    value = new_typed_id("bnd", now_ms=0x0123456789AB, randomness=randomness)
    parsed = UUID(value[4:])

    assert value == "bnd_01234567-89ab-7123-9159-e26af37bc004"
    assert (parsed.int >> 64) & 0xFFF == 0x123
    assert parsed.int & ((1 << 62) - 1) == 0x1159E26AF37BC004
    assert parsed.version == 7
    assert parsed.variant == "specified in RFC 4122"
    assert new_typed_id(
        "bnd",
        now_ms=0x0123456789AB,
        randomness=bytes.fromhex("123456789abcdef0013f"),
    ) == value
    assert new_typed_id(
        "bnd",
        now_ms=0x0123456789AB,
        randomness=bytes.fromhex("123456789abcdef00140"),
    ) != value


@pytest.mark.parametrize("prefix", sorted(ALLOWED_ID_PREFIXES))
def test_typed_id_accepts_only_frozen_runtime_and_portable_prefixes(prefix: str) -> None:
    assert new_typed_id(prefix, now_ms=0, randomness=b"\x00" * 10).startswith(
        f"{prefix}_"
    )


@pytest.mark.parametrize(
    ("prefix", "now_ms", "randomness", "code"),
    [
        ("unknown", 0, b"\x00" * 10, "typed_id_prefix_invalid"),
        ("job", -1, b"\x00" * 10, "typed_id_timestamp_invalid"),
        ("job", 1 << 48, b"\x00" * 10, "typed_id_timestamp_invalid"),
        ("job", 0, b"\x00" * 9, "typed_id_randomness_invalid"),
    ],
)
def test_typed_id_rejects_invalid_components(
    prefix: str, now_ms: int, randomness: bytes, code: str
) -> None:
    with pytest.raises(DomainError, match=code):
        new_typed_id(prefix, now_ms=now_ms, randomness=randomness)


def test_digest_and_timestamp_match_portable_wire_formats() -> None:
    assert sha256_digest(b"AllToNote") == (
        "sha256:d233c465c91867ffa74f672598a99bd84615be3704b3ec9a5750ed0deace5b76"
    )
    assert sha256_digest("AllToNote") == sha256_digest(b"AllToNote")
    assert utc_now_millis(
        datetime(2026, 7, 14, 8, 9, 10, 123_999, tzinfo=timezone.utc)
    ) == "2026-07-14T08:09:10.123Z"


def test_timestamp_normalizes_offsets_and_rejects_naive_values() -> None:
    assert utc_now_millis(
        datetime(
            2026,
            7,
            14,
            16,
            9,
            10,
            123_999,
            tzinfo=timezone(timedelta(hours=8)),
        )
    ) == "2026-07-14T08:09:10.123Z"

    with pytest.raises(DomainError, match="timestamp_timezone_required"):
        utc_now_millis(datetime(2026, 7, 14, 8, 9, 10, 123_999))


def test_error_categories_are_the_exact_frozen_set() -> None:
    assert {category.value for category in ErrorCategory} == {
        "invalid_request",
        "workspace_incompatible",
        "conflict",
        "retryable_runtime",
        "policy_denied",
        "recipe_failed",
        "cancelled",
        "internal",
    }


def test_domain_error_has_stable_safe_surface_and_redacts_details_from_text() -> None:
    error = DomainError(
        code="credential_missing",
        category=ErrorCategory.POLICY_DENIED,
        message="Credential profile is unavailable",
        details={"profile": "providers/openai-main", "secret": "do-not-print"},
    )

    assert error.code == "credential_missing"
    assert error.category is ErrorCategory.POLICY_DENIED
    assert error.message == "Credential profile is unavailable"
    assert error.details == {
        "profile": "providers/openai-main",
        "secret": "do-not-print",
    }
    assert "do-not-print" not in str(error)
    assert "do-not-print" not in repr(error)
    with pytest.raises(TypeError):
        error.details["secret"] = "changed"  # type: ignore[index]


@pytest.mark.parametrize("error_type", [ErrorDetail, DomainError])
def test_error_details_snapshot_nested_caller_collections(error_type: type) -> None:
    source = {
        "mapping": {"value": "original"},
        "list": [{"value": "original"}],
        "tuple": ({"value": "original"},),
        "set": {"original"},
        "frozenset": frozenset({"original"}),
    }
    error = error_type(
        code="nested_details",
        category=ErrorCategory.INTERNAL,
        message="Nested details",
        details=source,
    )

    source["mapping"]["value"] = "changed"
    source["list"][0]["value"] = "changed"
    source["list"].append("changed")
    source["tuple"][0]["value"] = "changed"
    source["set"].add("changed")
    source["frozenset"] = frozenset({"changed"})

    assert error.details == {
        "mapping": {"value": "original"},
        "list": ({"value": "original"},),
        "tuple": ({"value": "original"},),
        "set": frozenset({"original"}),
        "frozenset": frozenset({"original"}),
    }


@pytest.mark.parametrize("error_type", [ErrorDetail, DomainError])
def test_error_details_reject_nested_internal_mutation(error_type: type) -> None:
    error = error_type(
        code="nested_details",
        category=ErrorCategory.INTERNAL,
        message="Nested details",
        details={
            "mapping": {"value": "original"},
            "list": [{"value": "original"}],
            "set": {"original"},
        },
    )

    nested_mapping = error.details["mapping"]
    nested_list = error.details["list"]
    nested_set = error.details["set"]

    assert isinstance(nested_mapping, MappingProxyType)
    assert isinstance(nested_list, tuple)
    assert isinstance(nested_set, frozenset)
    list_item = nested_list[0]
    assert isinstance(list_item, MappingProxyType)
    with pytest.raises(TypeError):
        nested_mapping["value"] = "changed"
    with pytest.raises(TypeError):
        nested_list[0] = "changed"
    with pytest.raises(TypeError):
        list_item["value"] = "changed"
    with pytest.raises(AttributeError):
        nested_set.add("changed")


def test_transcript_rejects_invalid_half_open_range() -> None:
    with pytest.raises(DomainError, match="transcript_segment_invalid"):
        TranscriptSegment("seg_000001", 100, 100, "text")


@pytest.mark.parametrize(
    "segment",
    [
        TranscriptSegment("seg_000001", 0, 100, "first"),
        TranscriptSegment("seg_000002", 100, 200, "second"),
    ],
)
def test_transcript_segment_is_immutable(segment: TranscriptSegment) -> None:
    with pytest.raises(FrozenInstanceError):
        segment.text = "changed"  # type: ignore[misc]


def test_transcript_requires_nonempty_unique_chronological_segments() -> None:
    first = TranscriptSegment("seg_000001", 0, 100, "first")
    later = TranscriptSegment("seg_000002", 100, 200, "second")

    assert TranscriptDocument("zh-CN", (first, later)).segments == (first, later)
    with pytest.raises(DomainError, match="transcript_empty"):
        TranscriptDocument("zh-CN", ())
    with pytest.raises(DomainError, match="transcript_segment_duplicate"):
        TranscriptDocument("zh-CN", (first, first))
    with pytest.raises(DomainError, match="transcript_order_invalid"):
        TranscriptDocument("zh-CN", (later, first))


def test_request_and_generated_draft_are_frozen_and_own_collection_snapshots(
    tmp_path: Path,
) -> None:
    transcript = TranscriptDocument(
        "zh-CN", (TranscriptSegment("seg_000001", 0, 100, "text"),)
    )
    request = VideoProduceRequest(
        request_schema_version=1,
        workspace_root=tmp_path,
        input_value="https://example.test/video",
        provided_transcript=transcript,
    )
    usage = {"input_tokens": 10}
    draft = GeneratedVideoDraft(
        markdown="# Note\n\nFact[^seg_000001]",
        cited_segment_ids=("seg_000001",),
        screenshot_requests=(ScreenshotRequest("seg_000001"),),
        model_identity="fake/model-v1",
        usage=usage,
        warnings=(),
    )
    usage["input_tokens"] = 20

    assert request.screenshot_policy is ScreenshotPolicy.OFF
    assert draft.usage == {"input_tokens": 10}
    assert isinstance(draft.usage, MappingProxyType)
    with pytest.raises(FrozenInstanceError):
        request.input_value = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        draft.usage["input_tokens"] = 20  # type: ignore[index]


def test_result_job_snapshot_and_retry_request_keep_frozen_contract() -> None:
    result = VideoProduceResult(
        job_id="job_1",
        run_id="run_1",
        bundle_id="bnd_1",
        manifest_sha256="sha256:" + "0" * 64,
        commit_sha256="sha256:" + "1" * 64,
        workspace_relative_bundle_path="raw/personal/bundles/bnd_1",
        source_id="src_1",
        source_revision_id="rev_1",
        primary_draft_artifact_id="art_draft",
        transcript_artifact_id="art_transcript",
        evidence_set_artifact_id="art_evidence",
        quality_report_artifact_id="art_quality",
        display_asset_ids=(),
        quality_overall=QualityOverall.PASS,
        publish_eligible=True,
        usage={"output_tokens": 5},
        warnings=(),
        idempotent=False,
    )
    snapshot = JobSnapshot(
        job_id=result.job_id,
        state=JobState.SUCCEEDED,
        active_attempt_id=None,
        challenge_id=None,
        retry_of_job_id=None,
        result=result,
        error=None,
    )
    retry = RetryJobRequest(1, "retry-1", JobState.FAILED)

    assert snapshot.result is result
    assert retry.confirmed_unknown_operation_ids == ()
    with pytest.raises(FrozenInstanceError):
        snapshot.state = JobState.RUNNING  # type: ignore[misc]


def test_error_detail_is_an_immutable_value_object() -> None:
    detail = ErrorDetail(
        code="source_not_found",
        category=ErrorCategory.INVALID_REQUEST,
        message="Source does not exist",
        details={"input_kind": "local"},
    )

    assert isinstance(detail.details, MappingProxyType)
    with pytest.raises(FrozenInstanceError):
        detail.code = "changed"  # type: ignore[misc]


def test_ports_are_protocols_without_unapproved_speculative_methods() -> None:
    marker_ports = (
        VideoSourcePort,
        TranscriptPort,
        KnowledgeModelPort,
        ScreenshotPort,
        PortableWorkspacePort,
        CredentialBrokerPort,
        JobRepositoryPort,
        AttemptStoragePort,
    )

    assert all(getattr(port, "_is_protocol", False) for port in marker_ports)
    assert all(
        not {name for name in port.__dict__ if not name.startswith("_")}
        for port in marker_ports
    )
    event_argument, event_return = get_args(EventSink)
    assert event_argument[0].__forward_arg__ == "JobEvent"
    assert event_return is type(None)

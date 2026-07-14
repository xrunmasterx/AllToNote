from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from app.core.domain.ids import sha256_digest
from app.core.domain.video import (
    GeneratedVideoDraft,
    QualityOverall,
    TranscriptDocument,
    TranscriptSegment,
)
from app.core.errors import DomainError, ErrorCategory
from app.core.portable.artifacts import PortableArtifactRef, build_transcript
from app.core.portable.evidence import build_evidence_set, rewrite_segment_citations
from app.core.portable.jsonio import encode_json, encode_ndjson
from app.core.portable.markdown_safety import validate_markdown_safety
from app.core.portable.quality import evaluate_video_draft


BUNDLE_ID = "bnd_018f0000-0000-7000-8000-000000000001"
REVISION_ID = "rev_018f0000-0000-7000-8000-000000000002"
TRANSCRIPT_ARTIFACT_ID = "art_018f0000-0000-7000-8000-000000000003"
DRAFT_ARTIFACT_ID = "art_018f0000-0000-7000-8000-000000000004"
EVIDENCE_IDS = (
    "ev_018f0000-0000-7000-8000-000000000005",
    "ev_018f0000-0000-7000-8000-000000000006",
)
SEGMENTS = (
    TranscriptSegment("seg_000001", 0, 2_530, "第一段内容"),
    TranscriptSegment("seg_000002", 2_400, 5_100, "Second segment"),
)


def _draft(markdown: str) -> GeneratedVideoDraft:
    return GeneratedVideoDraft(
        markdown=markdown,
        cited_segment_ids=("seg_000001", "seg_000002"),
        screenshot_requests=(),
        model_identity="test-model",
        usage={"input_tokens": 10},
        warnings=(),
    )


def _good_markdown() -> str:
    return (
        "# Note\n\n"
        f"## 第一节\n内容[^{EVIDENCE_IDS[0]}]\n\n"
        f"## Second\nContent[^{EVIDENCE_IDS[1]}]\n\n"
        f"[^{EVIDENCE_IDS[0]}]: Transcript segment 1\n"
        f"[^{EVIDENCE_IDS[1]}]: Transcript segment 2\n"
    )


def _evidence_set(
    *,
    evidence_ids: dict[str, str] | None = None,
):
    transcript = build_transcript(REVISION_ID, "zh-CN", SEGMENTS)
    transcript_ref = PortableArtifactRef(
        bundle_id=BUNDLE_ID,
        artifact_id=TRANSCRIPT_ARTIFACT_ID,
        sha256=sha256_digest(transcript),
    )
    return build_evidence_set(
        BUNDLE_ID,
        REVISION_ID,
        transcript_ref,
        TranscriptDocument("zh-CN", SEGMENTS),
        evidence_ids
        or {
            "seg_000001": EVIDENCE_IDS[0],
            "seg_000002": EVIDENCE_IDS[1],
        },
    )


def _evaluate(
    draft: GeneratedVideoDraft,
    *,
    evidence_set=None,
    repair=None,
):
    return evaluate_video_draft(
        draft,
        evidence_set or _evidence_set(),
        draft_bundle_id=BUNDLE_ID,
        draft_artifact_id=DRAFT_ARTIFACT_ID,
        repair=repair,
    )


def test_canonical_json_is_repeatable_utf8_lf_and_sorted() -> None:
    value = {"z": "换行\r\n内容", "a": [1, True, None]}

    first = encode_json(value)
    second = encode_json(value)

    assert first == second
    assert first == '{"a":[1,true,null],"z":"换行\\r\\n内容"}\n'.encode()
    assert first.endswith(b"\n")
    assert b"\r" not in first


def test_ndjson_has_one_lf_terminated_record_per_value() -> None:
    raw = encode_ndjson(({"b": 2}, {"a": "非 ASCII"}))

    assert raw == b'{"b":2}\n' + '{"a":"非 ASCII"}\n'.encode()
    assert raw.endswith(b"\n")
    assert b"\r" not in raw


@pytest.mark.parametrize("records", (b"bytes", "text"))
def test_ndjson_rejects_ambiguous_byte_and_text_iterables(records: object) -> None:
    with pytest.raises(DomainError, match="portable_json_invalid"):
        encode_ndjson(records)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    (
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": b"ambiguous"},
        {1: "non-string-key"},
        {"value": object()},
    ),
)
def test_canonical_json_rejects_non_json_values(value: object) -> None:
    with pytest.raises(DomainError) as caught:
        encode_json(value)

    assert caught.value.code == "portable_json_invalid"
    assert caught.value.category is ErrorCategory.INVALID_REQUEST


def test_transcript_has_one_header_no_full_text_and_millisecond_segments() -> None:
    raw = build_transcript(REVISION_ID, "zh-CN", SEGMENTS)
    lines = [json.loads(line) for line in raw.splitlines()]

    assert lines[0] == {
        "language": "zh-CN",
        "record_type": "transcript_header",
        "source_revision_id": REVISION_ID,
        "time_base": "millisecond",
        "transcript_schema_version": 1,
    }
    assert "full_text" not in lines[0]
    assert lines[1] == {
        "end_ms": 2_530,
        "record_type": "segment",
        "segment_id": "seg_000001",
        "start_ms": 0,
        "text": "第一段内容",
    }
    assert len(lines) == 3
    assert raw.endswith(b"\n") and b"\r" not in raw


@pytest.mark.parametrize(
    ("segments", "code"),
    (
        ((), "transcript_empty"),
        ((SEGMENTS[0], SEGMENTS[0]), "transcript_segment_duplicate"),
        ((SEGMENTS[1], SEGMENTS[0]), "transcript_order_invalid"),
    ),
)
def test_transcript_rejects_empty_duplicate_and_out_of_order_segments(
    segments: tuple[TranscriptSegment, ...],
    code: str,
) -> None:
    with pytest.raises(DomainError, match=code):
        build_transcript(REVISION_ID, "zh-CN", segments)


def test_evidence_set_exactly_maps_segments_to_verifiable_records() -> None:
    evidence_set = _evidence_set()
    records = [json.loads(line) for line in evidence_set.payload.splitlines()]

    assert records[0] == {
        "bundle_id": BUNDLE_ID,
        "evidence_set_schema_version": 1,
        "record_count": 2,
        "record_type": "evidence_set_header",
    }
    assert records[1]["evidence_id"] == EVIDENCE_IDS[0]
    assert records[1]["locator"] == {
        "end_ms": 2_530,
        "scheme": "video-time-range.v1",
        "start_ms": 0,
    }
    assert records[1]["excerpt_sha256"] == sha256_digest("第一段内容")
    assert records[1]["target_artifact_ref"]["sha256"] == sha256_digest(
        build_transcript(REVISION_ID, "zh-CN", SEGMENTS)
    )
    assert dict(evidence_set.citation_map) == {
        "seg_000001": EVIDENCE_IDS[0],
        "seg_000002": EVIDENCE_IDS[1],
    }


@pytest.mark.parametrize(
    ("mapping", "code"),
    (
        (
            {"seg_000001": EVIDENCE_IDS[0]},
            "evidence_segment_mapping_incomplete",
        ),
        (
            {
                "seg_000001": EVIDENCE_IDS[0],
                "seg_000002": EVIDENCE_IDS[0],
            },
            "evidence_id_duplicate",
        ),
        (
            {
                "seg_000001": EVIDENCE_IDS[0],
                "seg_000002": EVIDENCE_IDS[1],
                "seg_999999": "ev_018f0000-0000-7000-8000-000000000007",
            },
            "evidence_segment_mapping_incomplete",
        ),
    ),
)
def test_evidence_set_rejects_missing_duplicate_and_extra_mappings(
    mapping: dict[str, str],
    code: str,
) -> None:
    with pytest.raises(DomainError, match=code):
        _evidence_set(evidence_ids=mapping)


def test_evidence_set_rejects_transcript_artifact_hash_mismatch() -> None:
    mismatched_ref = PortableArtifactRef(
        BUNDLE_ID,
        TRANSCRIPT_ARTIFACT_ID,
        "sha256:" + "0" * 64,
    )

    with pytest.raises(DomainError) as caught:
        build_evidence_set(
            BUNDLE_ID,
            REVISION_ID,
            mismatched_ref,
            TranscriptDocument("zh-CN", SEGMENTS),
            {
                "seg_000001": EVIDENCE_IDS[0],
                "seg_000002": EVIDENCE_IDS[1],
            },
        )

    assert caught.value.code == "evidence_target_hash_mismatch"


def test_segment_citations_are_rewritten_to_trusted_evidence_ids() -> None:
    result = rewrite_segment_citations(
        "结论[^seg_000001]",
        {"seg_000001": EVIDENCE_IDS[0]},
    )

    assert result == f"结论[^{EVIDENCE_IDS[0]}]"


def test_citation_rewrite_ignores_fenced_and_inline_code() -> None:
    source = (
        "正文[^seg_000001]\n\n"
        "```markdown\n[^seg_000001]\n```\n\n"
        "`[^seg_000001]` remains literal\n"
    )

    result = rewrite_segment_citations(
        source,
        {"seg_000001": EVIDENCE_IDS[0]},
    )

    assert result.startswith(f"正文[^{EVIDENCE_IDS[0]}]")
    assert "```markdown\n[^seg_000001]\n```" in result
    assert "`[^seg_000001]` remains literal" in result


def test_citation_rewrite_ignores_escaped_and_indented_code_literals() -> None:
    source = "\\[^seg_000001] is escaped\n    [^seg_000001] is code\n"

    result = rewrite_segment_citations(
        source,
        {"seg_000001": EVIDENCE_IDS[0]},
    )

    assert result == source


@pytest.mark.parametrize(
    "markdown",
    (
        "unknown[^seg_000002]",
        "malformed[^seg_x]",
        "malformed[^seg_000001_extra]",
        "malformed[^seg_000001",
    ),
)
def test_citation_rewrite_rejects_unknown_or_malformed_segment_labels(
    markdown: str,
) -> None:
    with pytest.raises(DomainError) as caught:
        rewrite_segment_citations(markdown, {"seg_000001": EVIDENCE_IDS[0]})

    assert caught.value.code == "draft_segment_citation_invalid"


@pytest.mark.parametrize(
    "markdown",
    (
        "<script>alert(1)</script>",
        "<iframe src='https://example.com'></iframe>",
        "<object data='safe.txt'></object>",
        "<embed src='safe.txt'>",
        "<form action='https://example.com'></form>",
        "<svg><a href='javascript:alert(1)'>x</a></svg>",
        "<div onclick='alert(1)'>x</div>",
        "<div style='background:url(javascript:alert(1))'>x</div>",
        "<style>body{display:none}</style>",
        "[x](javascript:alert(1))",
        "[x](vbscript:msgbox(1))",
        "![x](data:image/svg+xml;base64,PHN2Zz4=)",
        "[x](file:///C:/secret.txt)",
        "[x](C:/secret.txt)",
        "[x](/etc/passwd)",
        "[x](\\\\server\\share\\secret.txt)",
        "[x](..\\..\\secret.txt)",
        "[x](../../secret.txt)",
        "[x](%2e%2e/%2e%2e/secret.txt)",
        "[x](..%2f..%2fsecret.txt)",
        "[x](%5c%5cserver%5cshare)",
        "[local]: ../../secret.txt\n[x][local]",
        "[local]: C:/secret.txt\n[x][local]",
        "text\x00tail",
        "text\x01tail",
    ),
)
def test_markdown_safety_rejects_active_content_and_unsafe_links(
    markdown: str,
) -> None:
    with pytest.raises(DomainError) as caught:
        validate_markdown_safety(markdown, bundle_relative_path="drafts/note.md")

    assert caught.value.code == "draft_markdown_unsafe"
    assert caught.value.category is ErrorCategory.POLICY_DENIED
    assert "secret.txt" not in str(caught.value)


def test_markdown_safety_accepts_bundle_relative_obsidian_anchor_and_https_links() -> None:
    markdown = (
        "![frame](../assets/frame.webp)\n"
        "[[../evidence/transcript.jsonl#seg_000001]]\n"
        "[section](#heading)\n"
        "[source](https://example.com/video?id=1)\n"
    )

    validate_markdown_safety(markdown, bundle_relative_path="drafts/note.md")


def test_markdown_safety_treats_indented_code_as_literal_text() -> None:
    validate_markdown_safety(
        "    <script>alert(1)</script>\n",
        bundle_relative_path="drafts/note.md",
    )


def test_quality_passes_and_report_binds_exact_final_draft_bytes() -> None:
    outcome = _evaluate(_draft(_good_markdown()))
    report = json.loads(outcome.report.payload)

    assert outcome.overall is QualityOverall.PASS
    assert outcome.publish_eligible is True
    assert outcome.execution_error is None
    assert outcome.repair_attempts == 0
    assert outcome.report.subject_sha256 == sha256_digest(outcome.final_draft)
    assert report["subject"]["sha256"] == sha256_digest(outcome.final_draft)
    assert report["overall"] == "pass"
    assert report["method"] == {"kind": "deterministic"}
    assert report["evidence_ids"] == list(EVIDENCE_IDS)


def test_each_substantive_h2_requires_final_evidence_and_matching_definition() -> None:
    markdown = (
        "# Note\n\n"
        f"## Supported\nClaim[^{EVIDENCE_IDS[0]}]\n\n"
        "## Unsupported\nSubstantive text without evidence.\n\n"
        f"[^{EVIDENCE_IDS[0]}]: Segment 1\n"
    )

    outcome = _evaluate(_draft(markdown))

    assert outcome.overall is QualityOverall.FAIL
    assert outcome.publish_eligible is False
    assert outcome.execution_error is None
    assert "h2_evidence" in {
        check.check_id
        for check in outcome.report.checks
        if check.status == "fail"
    }


def test_indented_code_citations_do_not_satisfy_h2_evidence() -> None:
    markdown = (
        "# Note\n\n"
        f"## First\nClaim without evidence.\n    [^{EVIDENCE_IDS[0]}]\n\n"
        f"## Second\nAnother claim.\n    [^{EVIDENCE_IDS[1]}]\n\n"
        f"[^{EVIDENCE_IDS[0]}]: Segment 1\n"
        f"[^{EVIDENCE_IDS[1]}]: Segment 2\n"
    )

    outcome = _evaluate(_draft(markdown))

    assert outcome.overall is QualityOverall.FAIL
    assert outcome.publish_eligible is False
    assert "h2_evidence" in {
        check.check_id
        for check in outcome.report.checks
        if check.status == "fail"
    }


@pytest.mark.parametrize(
    "markdown",
    (
        f"# Note\n\n## One\nClaim[^{EVIDENCE_IDS[0]}]\n",
        (
            f"# Note\n\n## One\nClaim[^{EVIDENCE_IDS[0]}]\n\n"
            f"[^{EVIDENCE_IDS[0]}]: one\n"
            f"[^{EVIDENCE_IDS[0]}]: duplicate\n"
        ),
        (
            "# Note\n\n## One\n"
            "Claim[^ev_018f0000-0000-7000-8000-000000000099]\n\n"
            "[^ev_018f0000-0000-7000-8000-000000000099]: unknown\n"
        ),
    ),
)
def test_quality_rejects_missing_duplicate_and_unknown_footnote_definitions(
    markdown: str,
) -> None:
    outcome = _evaluate(_draft(markdown))

    assert outcome.overall is QualityOverall.FAIL
    assert outcome.publish_eligible is False


def test_quality_rejects_evidence_locator_or_excerpt_hash_mismatch() -> None:
    evidence_set = _evidence_set()
    records = [json.loads(line) for line in evidence_set.payload.splitlines()]
    records[1]["locator"]["end_ms"] += 1
    records[2]["excerpt_sha256"] = "sha256:" + "0" * 64
    corrupted = replace(evidence_set, payload=encode_ndjson(records))

    outcome = _evaluate(_draft(_good_markdown()), evidence_set=corrupted)

    assert outcome.overall is QualityOverall.FAIL
    assert outcome.publish_eligible is False
    assert "evidence_integrity" in {
        check.check_id
        for check in outcome.report.checks
        if check.status == "fail"
    }


def test_quality_rejects_replaced_transcript_target_digest() -> None:
    evidence_set = _evidence_set()
    corrupted = replace(
        evidence_set,
        target_artifact_ref=PortableArtifactRef(
            BUNDLE_ID,
            TRANSCRIPT_ARTIFACT_ID,
            "sha256:" + "0" * 64,
        ),
    )

    outcome = _evaluate(_draft(_good_markdown()), evidence_set=corrupted)

    assert outcome.overall is QualityOverall.FAIL
    assert outcome.publish_eligible is False
    assert "evidence_integrity" in {
        check.check_id
        for check in outcome.report.checks
        if check.status == "fail"
    }


def test_quality_rejects_transcript_language_mutation_against_target_digest() -> None:
    evidence_set = _evidence_set()
    corrupted = replace(
        evidence_set,
        transcript=TranscriptDocument("en-US", evidence_set.segments),
    )

    outcome = _evaluate(_draft(_good_markdown()), evidence_set=corrupted)

    assert outcome.overall is QualityOverall.FAIL
    assert outcome.publish_eligible is False
    assert "evidence_integrity" in {
        check.check_id
        for check in outcome.report.checks
        if check.status == "fail"
    }


def test_quality_repair_is_bounded_and_report_binds_repaired_draft() -> None:
    repair_calls = 0

    def repair(_draft_value: GeneratedVideoDraft) -> GeneratedVideoDraft:
        nonlocal repair_calls
        repair_calls += 1
        return _draft(_good_markdown())

    outcome = _evaluate(
        _draft("# Note\n\n## Missing\nNo evidence.\n"),
        repair=repair,
    )

    assert repair_calls == 1
    assert outcome.repair_attempts == 1
    assert outcome.overall is QualityOverall.PASS
    assert outcome.report.subject_sha256 == sha256_digest(outcome.final_draft)
    assert outcome.final_draft == _good_markdown().encode()


def test_repair_runs_once_then_rechecks_every_gate() -> None:
    repair_calls = 0

    def unsafe_repair(_draft_value: GeneratedVideoDraft) -> GeneratedVideoDraft:
        nonlocal repair_calls
        repair_calls += 1
        return _draft(_good_markdown() + "<script>alert(1)</script>\n")

    outcome = _evaluate(
        _draft("# Note\n\n## Missing\nNo evidence.\n"),
        repair=unsafe_repair,
    )

    assert repair_calls == 1
    assert outcome.repair_attempts == 1
    assert outcome.overall is QualityOverall.FAIL
    assert outcome.publish_eligible is False
    assert outcome.execution_error is None
    assert outcome.report.subject_sha256 == sha256_digest(outcome.final_draft)
    assert "markdown_safety" in {
        check.check_id
        for check in outcome.report.checks
        if check.status == "fail"
    }


def test_repair_cannot_make_an_initial_safety_failure_eligible() -> None:
    repair_calls = 0

    def repair(_draft_value: GeneratedVideoDraft) -> GeneratedVideoDraft:
        nonlocal repair_calls
        repair_calls += 1
        return _draft(_good_markdown())

    outcome = _evaluate(
        _draft(_good_markdown() + "<script>alert(1)</script>\n"),
        repair=repair,
    )

    assert repair_calls == 0
    assert outcome.repair_attempts == 0
    assert outcome.overall is QualityOverall.FAIL
    assert outcome.publish_eligible is False


def test_unrepairable_quality_failure_is_not_an_execution_error() -> None:
    outcome = _evaluate(_draft("# Note\n\n## Missing\nNo evidence.\n"))

    assert outcome.overall is QualityOverall.FAIL
    assert outcome.publish_eligible is False
    assert outcome.execution_error is None
    assert outcome.repair_attempts == 0


def test_repair_execution_failure_is_sanitized_and_distinct_from_quality() -> None:
    def fail_repair(_draft_value: GeneratedVideoDraft) -> GeneratedVideoDraft:
        raise OSError("secret C:/private/draft.md")

    outcome = _evaluate(
        _draft("# Note\n\n## Missing\nNo evidence.\n"),
        repair=fail_repair,
    )

    assert outcome.execution_error is not None
    assert outcome.execution_error.code == "quality_repair_failed"
    assert outcome.execution_error.category is ErrorCategory.RECIPE_FAILED
    assert "C:/private" not in outcome.execution_error.message
    assert outcome.publish_eligible is False
    assert outcome.repair_attempts == 1


def test_builders_snapshot_mutable_inputs_and_return_frozen_results() -> None:
    segments = list(SEGMENTS)
    evidence_ids = {
        "seg_000001": EVIDENCE_IDS[0],
        "seg_000002": EVIDENCE_IDS[1],
    }
    transcript = build_transcript(REVISION_ID, "zh-CN", segments)
    transcript_ref = PortableArtifactRef(
        BUNDLE_ID,
        TRANSCRIPT_ARTIFACT_ID,
        sha256_digest(transcript),
    )
    evidence_set = build_evidence_set(
        BUNDLE_ID,
        REVISION_ID,
        transcript_ref,
        TranscriptDocument("zh-CN", tuple(segments)),
        evidence_ids,
    )
    snapshot = evidence_set.payload

    segments.clear()
    evidence_ids.clear()

    assert evidence_set.payload == snapshot
    assert len(evidence_set.segments) == 2
    assert len(evidence_set.citation_map) == 2
    with pytest.raises(TypeError):
        evidence_set.citation_map["seg_000001"] = EVIDENCE_IDS[1]  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        evidence_set.payload = b"changed"  # type: ignore[misc]

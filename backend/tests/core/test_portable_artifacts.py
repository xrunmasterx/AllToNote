from __future__ import annotations

import json
import time
from collections.abc import Iterator, Mapping
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
from app.core.portable.quality import evaluate_video_draft, rebuild_quality_outcome


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


@pytest.mark.parametrize("failure", (ValueError, RuntimeError, OSError))
def test_ndjson_sanitizes_generator_failures(
    failure: type[Exception],
) -> None:
    def records():
        raise failure("secret C:/private/records.jsonl")
        yield None

    with pytest.raises(DomainError) as caught:
        encode_ndjson(records())

    assert caught.value.code == "portable_json_invalid"
    assert "C:/private" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_ndjson_does_not_swallow_generator_memory_error() -> None:
    def records():
        raise MemoryError()
        yield None

    with pytest.raises(MemoryError):
        encode_ndjson(records())


def _raise_caller_domain_error() -> None:
    raise DomainError(
        "caller_secret",
        ErrorCategory.INVALID_REQUEST,
        "secret C:/private/input.jsonl",
        {"path": "C:/private/input.jsonl"},
    ) from RuntimeError("secret C:/private/cause")


def _assert_sanitized_boundary_error(error: DomainError, code: str) -> None:
    assert error.code == code
    assert "C:/private" not in str(error)
    assert "C:/private" not in repr(error)
    assert "C:/private" not in repr(error.details)
    assert error.details == {}
    assert error.__cause__ is None


def test_ndjson_sanitizes_domain_errors_from_the_caller_iterator() -> None:
    def records():
        _raise_caller_domain_error()
        yield None

    with pytest.raises(DomainError) as caught:
        encode_ndjson(records())

    _assert_sanitized_boundary_error(caught.value, "portable_json_invalid")


def test_ndjson_preserves_trusted_json_validation_errors() -> None:
    with pytest.raises(DomainError) as caught:
        encode_ndjson(({"value": object()},))

    assert caught.value.code == "portable_json_invalid"
    assert caught.value.message == "Portable JSON value is invalid"


def test_canonical_json_sanitizes_circular_values() -> None:
    circular: list[object] = []
    circular.append(circular)

    with pytest.raises(DomainError) as caught:
        encode_json(circular)

    assert caught.value.code == "portable_json_invalid"
    assert caught.value.__cause__ is None


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


def test_transcript_sanitizes_invalid_segment_collections() -> None:
    with pytest.raises(DomainError) as caught:
        build_transcript(REVISION_ID, "zh-CN", None)  # type: ignore[arg-type]

    assert caught.value.code == "transcript_invalid"
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("failure", (ValueError, RuntimeError, OSError))
def test_transcript_sanitizes_segment_iterator_failures(
    failure: type[Exception],
) -> None:
    def segments() -> Iterator[TranscriptSegment]:
        raise failure("secret C:/private/segments.jsonl")
        yield SEGMENTS[0]

    with pytest.raises(DomainError) as caught:
        build_transcript(REVISION_ID, "zh-CN", segments())

    assert caught.value.code == "transcript_invalid"
    assert "C:/private" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_transcript_does_not_swallow_segment_iterator_memory_error() -> None:
    def segments() -> Iterator[TranscriptSegment]:
        raise MemoryError()
        yield SEGMENTS[0]

    with pytest.raises(MemoryError):
        build_transcript(REVISION_ID, "zh-CN", segments())


def test_transcript_sanitizes_domain_errors_from_the_caller_iterator() -> None:
    def segments() -> Iterator[TranscriptSegment]:
        _raise_caller_domain_error()
        yield SEGMENTS[0]

    with pytest.raises(DomainError) as caught:
        build_transcript(REVISION_ID, "zh-CN", segments())

    _assert_sanitized_boundary_error(caught.value, "transcript_invalid")


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


@pytest.mark.parametrize("bad_input", ("reference", "mapping"))
def test_evidence_set_sanitizes_invalid_boundary_inputs(bad_input: str) -> None:
    transcript = TranscriptDocument("zh-CN", SEGMENTS)
    reference = PortableArtifactRef(
        BUNDLE_ID,
        TRANSCRIPT_ARTIFACT_ID,
        sha256_digest(build_transcript(REVISION_ID, "zh-CN", SEGMENTS)),
    )
    mapping = {
        "seg_000001": EVIDENCE_IDS[0],
        "seg_000002": EVIDENCE_IDS[1],
    }

    with pytest.raises(DomainError) as caught:
        build_evidence_set(
            BUNDLE_ID,
            REVISION_ID,
            None if bad_input == "reference" else reference,  # type: ignore[arg-type]
            transcript,
            None if bad_input == "mapping" else mapping,  # type: ignore[arg-type]
        )

    assert caught.value.code == "evidence_input_invalid"
    assert caught.value.__cause__ is None


class _FailingMapping(Mapping[str, str]):
    def __init__(self, failure: type[BaseException]) -> None:
        self.failure = failure

    def __getitem__(self, key: str) -> str:
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        raise self.failure("secret C:/private/evidence.jsonl")

    def __len__(self) -> int:
        return 1


class _DomainErrorMapping(Mapping[str, str]):
    def __getitem__(self, key: str) -> str:
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        _raise_caller_domain_error()
        yield "seg_000001"

    def __len__(self) -> int:
        return 1


@pytest.mark.parametrize("failure", (RuntimeError, OSError))
@pytest.mark.parametrize("operation", ("build", "rewrite"))
def test_evidence_mapping_failures_are_sanitized(
    failure: type[Exception],
    operation: str,
) -> None:
    mapping = _FailingMapping(failure)

    with pytest.raises(DomainError) as caught:
        if operation == "build":
            transcript = TranscriptDocument("zh-CN", SEGMENTS)
            reference = PortableArtifactRef(
                BUNDLE_ID,
                TRANSCRIPT_ARTIFACT_ID,
                sha256_digest(build_transcript(REVISION_ID, "zh-CN", SEGMENTS)),
            )
            build_evidence_set(
                BUNDLE_ID,
                REVISION_ID,
                reference,
                transcript,
                mapping,
            )
        else:
            rewrite_segment_citations("claim[^seg_000001]", mapping)

    assert "C:/private" not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("operation", ("build", "rewrite"))
def test_evidence_mapping_memory_error_is_not_swallowed(operation: str) -> None:
    mapping = _FailingMapping(MemoryError)

    with pytest.raises(MemoryError):
        if operation == "build":
            transcript = TranscriptDocument("zh-CN", SEGMENTS)
            reference = PortableArtifactRef(
                BUNDLE_ID,
                TRANSCRIPT_ARTIFACT_ID,
                sha256_digest(build_transcript(REVISION_ID, "zh-CN", SEGMENTS)),
            )
            build_evidence_set(
                BUNDLE_ID,
                REVISION_ID,
                reference,
                transcript,
                mapping,
            )
        else:
            rewrite_segment_citations("claim[^seg_000001]", mapping)


@pytest.mark.parametrize(
    ("operation", "code"),
    (
        ("build", "evidence_input_invalid"),
        ("rewrite", "draft_segment_citation_invalid"),
    ),
)
def test_evidence_sanitizes_domain_errors_from_caller_mappings(
    operation: str,
    code: str,
) -> None:
    mapping = _DomainErrorMapping()

    with pytest.raises(DomainError) as caught:
        if operation == "build":
            transcript = TranscriptDocument("zh-CN", SEGMENTS)
            reference = PortableArtifactRef(
                BUNDLE_ID,
                TRANSCRIPT_ARTIFACT_ID,
                sha256_digest(build_transcript(REVISION_ID, "zh-CN", SEGMENTS)),
            )
            build_evidence_set(
                BUNDLE_ID,
                REVISION_ID,
                reference,
                transcript,
                mapping,
            )
        else:
            rewrite_segment_citations("claim[^seg_000001]", mapping)

    _assert_sanitized_boundary_error(caught.value, code)


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


def test_citation_rewrite_ignores_non_rendered_markdown_controls() -> None:
    source = (
        "Visible[^seg_000001]\n"
        "[link](https://example.test/[^seg_000001])\n"
        "<!-- [^seg_000001] -->\n"
        '<span data-proof="[^seg_000001]">text</span>\n'
        "<code>[^seg_000001]</code>\n"
        "<pre>[^seg_000001]</pre>\n"
    )

    result = rewrite_segment_citations(
        source,
        {"seg_000001": EVIDENCE_IDS[0]},
    )

    assert result == source.replace(
        "Visible[^seg_000001]",
        f"Visible[^{EVIDENCE_IDS[0]}]",
    )


def test_rewrite_then_quality_ignores_literal_hidden_segment_controls() -> None:
    raw = (
        "# Note\n\n"
        "## First\nContent[^seg_000001]\n\n"
        "## Second\nContent[^seg_000002]\n\n"
        "[link](https://example.test/[^seg_000001])\n"
        "<!-- [^seg_000002] -->\n"
    )
    rewritten = rewrite_segment_citations(
        raw,
        {
            "seg_000001": EVIDENCE_IDS[0],
            "seg_000002": EVIDENCE_IDS[1],
        },
    )
    markdown = (
        rewritten
        + f"[^{EVIDENCE_IDS[0]}]: Transcript segment 1\n"
        + f"[^{EVIDENCE_IDS[1]}]: Transcript segment 2\n"
    )

    outcome = _evaluate(_draft(markdown))

    assert outcome.overall is QualityOverall.PASS
    assert "https://example.test/[^seg_000001]" in markdown
    assert "<!-- [^seg_000002] -->" in markdown


def test_escaped_backtick_does_not_hide_active_html() -> None:
    with pytest.raises(DomainError, match="draft_markdown_unsafe"):
        validate_markdown_safety(
            "\\`<script>alert(1)</script>`",
            bundle_relative_path="drafts/note.md",
        )


def test_escaped_backtick_does_not_hide_visible_segment_citation() -> None:
    result = rewrite_segment_citations(
        "\\`claim[^seg_000001]`",
        {"seg_000001": EVIDENCE_IDS[0]},
    )

    assert result == f"\\`claim[^{EVIDENCE_IDS[0]}]`"


def test_segment_citation_escape_uses_backslash_parity() -> None:
    source = "\\[^seg_000001] \\\\[^seg_000001]"

    result = rewrite_segment_citations(
        source,
        {"seg_000001": EVIDENCE_IDS[0]},
    )

    assert result == f"\\[^seg_000001] \\\\[^{EVIDENCE_IDS[0]}]"


def test_multiline_code_span_is_literal_for_safety_and_citations() -> None:
    source = "before `code\n<script>[^seg_000001]</script>\nend` after"

    validate_markdown_safety(source, bundle_relative_path="drafts/note.md")
    assert rewrite_segment_citations(
        source,
        {"seg_000001": EVIDENCE_IDS[0]},
    ) == source


def test_fence_with_trailing_text_does_not_close_code_block() -> None:
    source = "```text\n```not-close\n<script>[^seg_000001]</script>\n"

    validate_markdown_safety(source, bundle_relative_path="drafts/note.md")
    assert rewrite_segment_citations(
        source,
        {"seg_000001": EVIDENCE_IDS[0]},
    ) == source


def test_mermaid_fence_is_rejected_as_active_content() -> None:
    with pytest.raises(DomainError, match="draft_markdown_unsafe"):
        validate_markdown_safety(
            "```mermaid\ngraph TD; A-->B\n```\n",
            bundle_relative_path="drafts/note.md",
        )


def test_markdown_scan_scales_linearly_on_unclosed_link_markers() -> None:
    durations: list[float] = []
    for size in (4_000, 8_000, 16_000):
        started = time.perf_counter()
        validate_markdown_safety(
            "[" * size,
            bundle_relative_path="drafts/note.md",
        )
        durations.append(time.perf_counter() - started)

    assert durations[-1] < 1.0
    assert durations[-1] < max(0.2, durations[0] * 6)


def test_markdown_safety_rejects_remote_reference_images() -> None:
    markdown = "![frame][asset]\n[asset]: https://tracker.example/frame.webp\n"

    with pytest.raises(DomainError, match="draft_markdown_unsafe"):
        validate_markdown_safety(markdown, bundle_relative_path="drafts/note.md")


def test_markdown_safety_rejects_absolute_destination_after_nested_label() -> None:
    with pytest.raises(DomainError, match="draft_markdown_unsafe"):
        validate_markdown_safety(
            "[outer [inner]](/etc/passwd)",
            bundle_relative_path="drafts/note.md",
        )


def test_markdown_safety_rejects_remote_obsidian_embeds() -> None:
    with pytest.raises(DomainError, match="draft_markdown_unsafe"):
        validate_markdown_safety(
            "![[https://tracker.example/frame.webp]]",
            bundle_relative_path="drafts/note.md",
        )


def test_markdown_safety_preserves_link_and_embed_destination_kinds() -> None:
    validate_markdown_safety(
        "[source][site]\n[site]: https://example.com/watch\n"
        "![[../assets/frame.webp]]\n",
        bundle_relative_path="drafts/note.md",
    )


@pytest.mark.parametrize("shape", ("nested", "unclosed"))
def test_balanced_destination_scan_scales_linearly(shape: str) -> None:
    durations: list[float] = []
    for size in (4_000, 8_000, 16_000):
        markdown = (
            "[" * size + "label" + "]" * size
            if shape == "nested"
            else "[" * size
        )
        started = time.perf_counter()
        validate_markdown_safety(markdown, bundle_relative_path="drafts/note.md")
        durations.append(time.perf_counter() - started)

    assert durations[-1] < 1.0
    assert durations[-1] < max(0.2, durations[0] * 6)


@pytest.mark.parametrize(
    "markdown",
    (
        "[\n\n![x][id]\n[id]: https://tracker.example/frame.webp\n",
        "[\n\n[safe](/etc/passwd)\n",
    ),
)
def test_unmatched_bracket_does_not_hide_later_unsafe_destinations(
    markdown: str,
) -> None:
    with pytest.raises(DomainError, match="draft_markdown_unsafe"):
        validate_markdown_safety(markdown, bundle_relative_path="drafts/note.md")


def test_many_unmatched_brackets_remain_linear_and_do_not_hide_remote_images() -> None:
    markdown = (
        "[" * 64_000
        + "\n\n![x][id]\n[id]: https://tracker.example/frame.webp\n"
    )
    started = time.perf_counter()

    with pytest.raises(DomainError, match="draft_markdown_unsafe"):
        validate_markdown_safety(markdown, bundle_relative_path="drafts/note.md")

    assert time.perf_counter() - started < 1.0


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


def test_citation_rewrite_sanitizes_invalid_mapping_inputs() -> None:
    with pytest.raises(DomainError) as caught:
        rewrite_segment_citations(
            "claim[^seg_000001]",
            None,  # type: ignore[arg-type]
        )

    assert caught.value.code == "draft_segment_citation_invalid"
    assert caught.value.__cause__ is None


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


def test_backslash_does_not_escape_a_closing_backtick_inside_code() -> None:
    markdown = r"`literal\`<script>alert(1)</script>`"

    with pytest.raises(DomainError, match="draft_markdown_unsafe"):
        validate_markdown_safety(markdown, bundle_relative_path="drafts/note.md")


@pytest.mark.parametrize(
    "markdown",
    (
        "<video src='../assets/movie.mp4'></video>",
        "<audio src='../assets/audio.mp3'></audio>",
        "<source src='../assets/movie.mp4'>",
        "<track src='../assets/captions.vtt'>",
        "<canvas>active surface</canvas>",
        "<img srcset='../assets/a.webp 1x, ../assets/b.webp 2x'>",
        "![remote](https://example.com/frame.webp)",
        "<img src='https://example.com/frame.webp'>",
    ),
)
def test_markdown_safety_rejects_active_media_and_external_images(
    markdown: str,
) -> None:
    with pytest.raises(DomainError, match="draft_markdown_unsafe"):
        validate_markdown_safety(markdown, bundle_relative_path="drafts/note.md")


@pytest.mark.parametrize(
    "markdown",
    (
        "<input type='image' src='https://tracker.example/pixel'>",
        "<button formaction='https://tracker.example/submit'>send</button>",
        "<a href='https://example.com' ping='https://tracker.example/ping'>link</a>",
        "<a formaction='https://tracker.example/submit'>link</a>",
    ),
)
def test_markdown_safety_rejects_active_network_html(markdown: str) -> None:
    with pytest.raises(DomainError, match="draft_markdown_unsafe"):
        validate_markdown_safety(markdown, bundle_relative_path="drafts/note.md")


@pytest.mark.parametrize(
    "markdown",
    (
        "[source](https://example.com/watch?v=1)",
        "![frame](../assets/frame.webp)",
        "<img src='../assets/frame.webp' alt='frame'>",
    ),
)
def test_markdown_safety_allows_http_links_and_bundle_relative_images(
    markdown: str,
) -> None:
    validate_markdown_safety(markdown, bundle_relative_path="drafts/note.md")


@pytest.mark.parametrize(
    "markdown",
    (
        "[x](%252e%252e%252fsecret.txt)",
        "[x](%25252e%25252e%25252fsecret.txt)",
    ),
)
def test_markdown_safety_rejects_multiply_encoded_traversal(markdown: str) -> None:
    with pytest.raises(DomainError, match="draft_markdown_unsafe"):
        validate_markdown_safety(markdown, bundle_relative_path="drafts/note.md")


@pytest.mark.parametrize(
    "base_path",
    (
        "C:/drafts/note.md",
        "/drafts/note.md",
        "drafts/../note.md",
    ),
)
def test_markdown_safety_rejects_absolute_or_noncanonical_base_paths(
    base_path: str,
) -> None:
    with pytest.raises(DomainError, match="draft_markdown_unsafe"):
        validate_markdown_safety("safe", bundle_relative_path=base_path)


@pytest.mark.parametrize(
    "base_path",
    (
        "drafts/%2e%2e/note.md",
        "drafts/%252e%252e/note.md",
        "drafts/%252fetc/note.md",
        "drafts/%2500note.md",
        "drafts/no\x00te.md",
        "drafts/no\x01te.md",
    ),
)
def test_markdown_safety_rejects_encoded_or_raw_controls_in_base_paths(
    base_path: str,
) -> None:
    with pytest.raises(DomainError, match="draft_markdown_unsafe"):
        validate_markdown_safety("safe", bundle_relative_path=base_path)


@pytest.mark.parametrize(
    "device_name",
    (
        "CON",
        "prn.txt",
        "AuX",
        "nul.md",
        *(f"COM{index}.txt" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    ),
)
def test_markdown_safety_rejects_windows_device_names_in_any_base_segment(
    device_name: str,
) -> None:
    with pytest.raises(DomainError, match="draft_markdown_unsafe"):
        validate_markdown_safety(
            "safe",
            bundle_relative_path=f"drafts/{device_name}/note.md",
        )


@pytest.mark.parametrize(
    "base_path",
    (
        "drafts/console/note.md",
        "drafts/prn-notes.md",
        "drafts/com10.txt",
        "drafts/lpt0.txt",
    ),
)
def test_markdown_safety_allows_portable_non_device_base_names(base_path: str) -> None:
    validate_markdown_safety("safe", bundle_relative_path=base_path)


@pytest.mark.parametrize(
    "base_path",
    (
        "drafts/note:part.md",
        "drafts/folder./note.md",
        "drafts/folder /note.md",
        "drafts/CON/note.md",
        "drafts/CON:/note.md",
        "drafts/NUL::$DATA/note.md",
        "drafts/prn.backup/note.md",
    ),
)
def test_markdown_safety_enforces_public_portable_component_rules(
    base_path: str,
) -> None:
    with pytest.raises(DomainError, match="draft_markdown_unsafe"):
        validate_markdown_safety("safe", bundle_relative_path=base_path)


@pytest.mark.parametrize(
    "base_path",
    (
        "drafts/conifer/note.md",
        "drafts/console.txt",
        "drafts/COM10.txt",
        "drafts/lpt-notes/note.md",
    ),
)
def test_markdown_safety_keeps_normal_portable_component_names(
    base_path: str,
) -> None:
    validate_markdown_safety("safe", bundle_relative_path=base_path)


@pytest.mark.parametrize(
    "device_name",
    tuple(
        name
        for prefix in ("COM", "LPT")
        for suffix in ("¹", "²", "³")
        for name in (f"{prefix}{suffix}", f"{prefix}{suffix}.txt")
    ),
)
def test_markdown_safety_rejects_superscript_windows_device_aliases(
    device_name: str,
) -> None:
    with pytest.raises(DomainError, match="draft_markdown_unsafe"):
        validate_markdown_safety(
            "safe",
            bundle_relative_path=f"drafts/{device_name}/note.md",
        )


@pytest.mark.parametrize(
    "base_path",
    (
        "drafts/COM¹-notes/note.md",
        "drafts/LPT²backup/note.md",
        "drafts/xCOM³/note.md",
        "drafts/COM⁴/note.md",
    ),
)
def test_markdown_safety_allows_similar_superscript_component_names(
    base_path: str,
) -> None:
    validate_markdown_safety("safe", bundle_relative_path=base_path)


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


def test_markdown_safety_sanitizes_malformed_ipv6_urls() -> None:
    with pytest.raises(DomainError) as caught:
        validate_markdown_safety(
            "[source](http://[::1)",
            bundle_relative_path="drafts/note.md",
        )

    assert caught.value.code == "draft_markdown_unsafe"
    assert caught.value.__cause__ is None


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
    assert report["profile"] == {"id": "alltonote.video-course-note", "version": 1}
    assert report["evidence_ids"] == list(EVIDENCE_IDS)


def test_quality_report_uses_the_video_course_note_profile() -> None:
    report = json.loads(_evaluate(_draft(_good_markdown())).report.payload)

    assert report["profile"] == {"id": "alltonote.video-course-note", "version": 1}


def test_canonical_quality_rebuild_uses_final_artifact_and_evidence() -> None:
    evidence_set = _evidence_set()
    expected = _evaluate(_draft(_good_markdown()), evidence_set=evidence_set)

    rebuilt = rebuild_quality_outcome(
        expected.final_draft,
        evidence_set,
        draft_bundle_id=BUNDLE_ID,
        draft_artifact_id=DRAFT_ARTIFACT_ID,
        repair_attempts=expected.repair_attempts,
    )

    assert rebuilt == expected


@pytest.mark.parametrize("bad_input", ("draft", "evidence", "bundle_id", "artifact_id"))
def test_quality_sanitizes_invalid_boundary_inputs(bad_input: str) -> None:
    kwargs: dict[str, object] = {
        "draft": _draft(_good_markdown()),
        "evidence_set": _evidence_set(),
        "draft_bundle_id": BUNDLE_ID,
        "draft_artifact_id": DRAFT_ARTIFACT_ID,
    }
    if bad_input in {"draft", "evidence"}:
        kwargs["draft" if bad_input == "draft" else "evidence_set"] = object()
    else:
        kwargs["draft_bundle_id" if bad_input == "bundle_id" else "draft_artifact_id"] = (
            "C:/private/draft.md"
        )

    with pytest.raises(DomainError) as caught:
        evaluate_video_draft(**kwargs)  # type: ignore[arg-type]

    assert caught.value.code == "quality_input_invalid"
    assert caught.value.category is ErrorCategory.INVALID_REQUEST
    assert "C:/private" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_quality_does_not_require_an_h2_when_all_citations_are_valid() -> None:
    markdown = (
        "# Note\n\n"
        f"Summary[^{EVIDENCE_IDS[0]}][^{EVIDENCE_IDS[1]}]\n\n"
        f"[^{EVIDENCE_IDS[0]}]: Segment 1\n"
        f"[^{EVIDENCE_IDS[1]}]: Segment 2\n"
    )

    outcome = _evaluate(_draft(markdown))

    assert outcome.overall is QualityOverall.PASS
    assert outcome.publish_eligible is True


@pytest.mark.parametrize(
    ("slashes", "expected_overall"),
    (
        ("\\", QualityOverall.PASS),
        ("\\\\", QualityOverall.FAIL),
    ),
)
def test_quality_citation_and_definition_escapes_use_backslash_parity(
    slashes: str,
    expected_overall: QualityOverall,
) -> None:
    unknown = "ev_018f0000-0000-7000-8000-000000000099"
    markdown = (
        _good_markdown()
        + f"\n{slashes}[^{unknown}]\n"
        + f"{slashes}[^{unknown}]: escaped or active definition\n"
    )

    outcome = _evaluate(_draft(markdown))

    assert outcome.overall is expected_overall


def test_quality_ignores_an_escaped_unknown_segment_citation() -> None:
    outcome = _evaluate(_draft(_good_markdown() + r"\[^seg_unknown]" + "\n"))

    assert outcome.overall is QualityOverall.PASS
    assert outcome.publish_eligible is True


def test_setext_h2_requires_evidence_when_its_body_is_substantive() -> None:
    markdown = (
        "# Note\n\n"
        "Unsupported\n"
        "-----------\n"
        "Substantive text without evidence.\n\n"
        f"## Supported\nSummary[^{EVIDENCE_IDS[0]}][^{EVIDENCE_IDS[1]}]\n\n"
        f"[^{EVIDENCE_IDS[0]}]: Segment 1\n"
        f"[^{EVIDENCE_IDS[1]}]: Segment 2\n"
    )

    outcome = _evaluate(_draft(markdown))

    assert outcome.overall is QualityOverall.FAIL
    assert "h2_evidence" in {
        check.check_id
        for check in outcome.report.checks
        if check.status == "fail"
    }


@pytest.mark.parametrize(
    "literal_block",
    (
        "```text\nNot a section\n-------------\n[^ev_fake]: fake\n```\n",
        "    Not a section\n    -------------\n    [^ev_fake]: fake\n",
        "``Not a section\n-------------\n[^ev_fake]: fake``\n",
    ),
)
def test_setext_headings_and_footnotes_inside_code_are_ignored(
    literal_block: str,
) -> None:
    markdown = (
        "# Note\n\n"
        + literal_block
        + f"Summary[^{EVIDENCE_IDS[0]}][^{EVIDENCE_IDS[1]}]\n\n"
        + f"[^{EVIDENCE_IDS[0]}]: Segment 1\n"
        + f"[^{EVIDENCE_IDS[1]}]: Segment 2\n"
    )

    outcome = _evaluate(_draft(markdown))

    assert outcome.overall is QualityOverall.PASS
    assert outcome.publish_eligible is True


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


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("header_schema", True),
        ("record_count", 2.0),
        ("record_schema", True),
        ("start_ms", False),
        ("end_ms", 2530.0),
    ),
)
def test_quality_rejects_non_integer_evidence_schema_and_locator_values(
    field: str,
    value: object,
) -> None:
    evidence_set = _evidence_set()
    records = [json.loads(line) for line in evidence_set.payload.splitlines()]
    if field == "header_schema":
        records[0]["evidence_set_schema_version"] = value
    elif field == "record_count":
        records[0]["record_count"] = value
    elif field == "record_schema":
        records[1]["evidence_ref_schema_version"] = value
    else:
        records[1]["locator"][field] = value
    corrupted = replace(evidence_set, payload=encode_ndjson(records))

    outcome = _evaluate(_draft(_good_markdown()), evidence_set=corrupted)

    assert outcome.overall is QualityOverall.FAIL
    assert "evidence_integrity" in {
        check.check_id
        for check in outcome.report.checks
        if check.status == "fail"
    }


def test_quality_rejects_noncanonical_evidence_ndjson() -> None:
    evidence_set = _evidence_set()
    records = [json.loads(line) for line in evidence_set.payload.splitlines()]
    noncanonical = b"".join(
        json.dumps(record, ensure_ascii=False).encode() + b"\n" for record in records
    )
    corrupted = replace(evidence_set, payload=noncanonical)

    outcome = _evaluate(_draft(_good_markdown()), evidence_set=corrupted)

    assert outcome.overall is QualityOverall.FAIL
    assert "evidence_integrity" in {
        check.check_id
        for check in outcome.report.checks
        if check.status == "fail"
    }


def test_quality_treats_deeply_nested_evidence_json_as_an_integrity_failure() -> None:
    evidence_set = _evidence_set()
    deeply_nested = b"[" * 2_000 + b"0" + b"]" * 2_000 + b"\n"
    corrupted = replace(evidence_set, payload=deeply_nested)

    outcome = _evaluate(_draft(_good_markdown()), evidence_set=corrupted)

    assert outcome.overall is QualityOverall.FAIL
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


def test_invalid_repair_return_is_sanitized() -> None:
    def invalid_repair(_draft_value: GeneratedVideoDraft):
        return "C:/private/draft.md"

    outcome = _evaluate(
        _draft("# Note\n\n## Missing\nNo evidence.\n"),
        repair=invalid_repair,
    )

    assert outcome.execution_error is not None
    assert outcome.execution_error.code == "quality_repair_failed"
    assert "C:/private" not in outcome.execution_error.message


@pytest.mark.parametrize("fatal", (MemoryError, KeyboardInterrupt, SystemExit))
def test_repair_does_not_swallow_fatal_exceptions(fatal: type[BaseException]) -> None:
    def fail_repair(_draft_value: GeneratedVideoDraft) -> GeneratedVideoDraft:
        raise fatal()

    with pytest.raises(fatal):
        _evaluate(
            _draft("# Note\n\n## Missing\nNo evidence.\n"),
            repair=fail_repair,
        )


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

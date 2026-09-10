from dataclasses import replace
import json

import pytest

from app.core.domain.video import TranscriptDocument, TranscriptSegment
from app.core.domain.visual_frame import VisualFrame
from app.core.errors import DomainError
from app.core.recipes.video.faithful_edition.source_grounding import (
    GROUNDING_CHECK_INSTRUCTION,
    GROUNDING_REPAIR_INSTRUCTION,
    grounding_payload,
    parse_grounding,
)
from app.core.recipes.video.faithful_edition.pipeline import plan_faithful_edition
from app.core.application.artifact_query_service import project_reading_markdown
from test_faithful_edition_compiler import _FaithfulExecutor, _compiler_context, _request, _binding


SEGMENT = TranscriptSegment("seg_000001", 0, 1000, "到这个1.25左右")
FRAME = VisualFrame(SEGMENT.segment_id, 500, bytes.fromhex(
    "524946461a000000574542505650384c0d0000002f00000000071011118888fe0700"))


def test_grounding_review_instruction_blocks_only_owned_substantive_corrections():
    assert "SAME owned segment" in GROUNDING_CHECK_INSTRUCTION
    assert "screen-only aliases" in GROUNDING_CHECK_INSTRUCTION
    assert "Chinese versus Arabic digits" in GROUNDING_CHECK_INSTRUCTION
    assert "context without a readable same-segment frame" in GROUNDING_CHECK_INSTRUCTION
    assert "reasonable alternate grouping" in GROUNDING_CHECK_INSTRUCTION
    assert "Address every valid review_feedback item" in GROUNDING_REPAIR_INSTRUCTION
    assert "never copy words owned by an adjacent segment" in GROUNDING_REPAIR_INSTRUCTION


def _proposal():
    return {"corrections": [{"segment_id": SEGMENT.segment_id, "before": SEGMENT.text,
        "after": "到这个1.5左右", "frame_segment_id": FRAME.segment_id,
        "visible_quote": "到这个1.5左右", "reason": "对应字幕明确显示1.5"}],
        "chapter_start_ids": [], "illustration_ids": [FRAME.segment_id]}


@pytest.mark.parametrize("fault", ["unknown", "duplicate", "before", "number_without_frame", "remote_frame", "empty_quote", "wrong_number_quote", "unknown_chapter", "duplicate_illustration"])
def test_grounding_rejects_invalid_evidence(fault):
    value = _proposal()
    correction = value["corrections"][0]
    if fault == "unknown": correction["segment_id"] = "seg_999999"
    if fault == "duplicate": value["corrections"].append(dict(correction))
    if fault == "before": correction["before"] = "wrong original"
    if fault == "number_without_frame": correction.update(frame_segment_id="", visible_quote="")
    if fault == "remote_frame": correction["frame_segment_id"] = "seg_999999"
    if fault == "empty_quote": correction["visible_quote"] = ""
    if fault == "wrong_number_quote": correction["visible_quote"] = "图中价格是3.4"
    if fault == "unknown_chapter": value["chapter_start_ids"] = ["seg_999999"]
    if fault == "duplicate_illustration": value["illustration_ids"] *= 2
    with pytest.raises(DomainError, match="faithful_source_grounding_invalid"):
        parse_grounding(json.dumps(value), (SEGMENT,), (FRAME,), max_response_bytes=16000)


def test_grounding_ignores_only_explicit_read_only_context_ids():
    context_id = "seg_000002"
    value = _proposal()
    value["corrections"].append({**value["corrections"][0], "segment_id": context_id})
    value["chapter_start_ids"] = [context_id]
    value["illustration_ids"].append(context_id)

    parsed = parse_grounding(
        json.dumps(value), (SEGMENT,), (FRAME,), max_response_bytes=16000,
        ignored_segment_ids=frozenset({context_id}),
    )

    assert len(parsed.corrections) == 1
    assert parsed.chapter_start_ids == ()
    assert parsed.illustration_ids == (FRAME.segment_id,)


@pytest.mark.parametrize(("before", "after", "quote"), [
    ("区间是74到74000", "区间是74,000到74,700", "区间是74,000-74,700"),
    ("第二个位置61515", "第二个位置65,000，65,000", "第二个位置65,00065,000"),
    ("第二个位置61515", "第二个位置65000", "第二个位置65,000"),
])
def test_grounding_accepts_visible_range_and_repeated_number_formats(before, after, quote):
    segment = replace(SEGMENT, text=before)
    frame = replace(FRAME, segment_id=segment.segment_id)
    value = _proposal()
    value["corrections"][0].update(before=before, after=after, visible_quote=quote)

    parsed = parse_grounding(json.dumps(value), (segment,), (frame,), max_response_bytes=16000)

    assert parsed.corrections[0].after == after


@pytest.mark.parametrize("before,after,quote", [
    ("到这个1.25左右", "到这个1.5左右", "到这个1.5左右"),
    ("这个萌莫币", "这个门罗币", "门罗币也是三连胜"),
    ("看见久转", "看跌9转", "4小时有一个看跌9转"),
    ("决定要不要超级", "决定要不要抄底", "决定要不要去抄底"),
    ("看一下BGC", "看一下BTC", "然后看一下BTC"),
    ("后面什么时候开始做空", "后面什么时候开始做多", "后面什么时候开始做多"),
])
def test_grounded_terms_and_numbers_reach_body_and_preserve_raw_audit(tmp_path, before, after, quote):
    segment = replace(SEGMENT, text=before)
    class Executor(_FaithfulExecutor):
        def complete(self, request, token):
            result = super().complete(request, token)
            if request.stage_id == "faithful-source-prepare":
                data = _proposal()
                data["corrections"][0].update(before=before, after=after, visible_quote=quote)
                return replace(result, text=json.dumps(data))
            return result
    executor = Executor()
    compiler, context = _compiler_context(tmp_path, executor)
    request = replace(_request(transcript=TranscriptDocument("zh", (segment,))),
        visual_frames=(FRAME,), section_input_byte_budget=8192,
        model_binding=replace(_binding(), provider_type="codex-app-server"))
    result = compiler.compile(request, context)
    assert after in project_reading_markdown(result.markdown)
    overview = result.markdown.split("## 全文概览")[1].split("## 正文")[0]
    assert "[^seg_000001]" in overview
    assert before == request.transcript.segments[0].text
    assert result.text_assessment.metrics.number_mismatch_count == 0
    log = json.loads(result.markdown.split("```alltonote-edit-log-v1\n")[1].split("\n```")[0])
    assert log["records"][0]["before"] == before
    assert log["records"][0]["visible_quote"] == quote
    assert compiler.compile(request, context).markdown == result.markdown
    assert len(executor.requests) == 5


def test_grounding_review_rejection_blocks_before_writing(tmp_path):
    class Executor(_FaithfulExecutor):
        def complete(self, request, token):
            result = super().complete(request, token)
            if request.stage_id in {"faithful-source-check", "faithful-source-recheck"}:
                return replace(result, text=json.dumps({"pass": False, "issues": ["seg_000001 unsupported number"]}))
            return result
    executor = Executor()
    compiler, context = _compiler_context(tmp_path, executor)
    request = replace(_request(transcript=TranscriptDocument("zh", (SEGMENT,))),
        visual_frames=(FRAME,), model_binding=replace(_binding(), provider_type="codex-app-server"))
    with pytest.raises(DomainError, match="faithful_source_review_failed"):
        compiler.compile(request, context)
    assert len(executor.requests) == 4


def test_omitted_correction_feedback_repairs_source_before_writing(tmp_path):
    class Executor(_FaithfulExecutor):
        def complete(self, request, token):
            result = super().complete(request, token)
            if request.stage_id == "faithful-source-check":
                assert "ALL uncorrected owned segments" in request.system_instruction
                return replace(result, text=json.dumps({"pass": False, "issues": [
                    "seg_000001 omitted numeric correction; frame seg_000001 shows 1.5"]}))
            if request.stage_id == "faithful-source-repair":
                payload = json.loads(request.user_content)
                assert "omitted numeric correction" in payload["review_feedback"][0]
                return replace(result, text=json.dumps(_proposal()))
            return result
    executor = Executor()
    compiler, context = _compiler_context(tmp_path, executor)
    request = replace(_request(transcript=TranscriptDocument("zh", (SEGMENT,))),
        visual_frames=(FRAME,), model_binding=replace(_binding(), provider_type="codex-app-server"))
    result = compiler.compile(request, context)
    assert "到这个1.5左右" in project_reading_markdown(result.markdown)
    assert [value.stage_id for value in executor.requests[:4]] == [
        "faithful-source-prepare", "faithful-source-check", "faithful-source-repair", "faithful-source-recheck"]
    assert len(executor.requests) == 7


@pytest.mark.parametrize("caption,selected", [
    ("白色框选区域邻近104.36刻度。", False),
    ("白色框选区域在104附近。", True),
    ("图中框选的支撑区域。", True),
])
def test_caption_cannot_add_unverified_numeric_precision(tmp_path, caption, selected):
    class Executor(_FaithfulExecutor):
        def complete(self, request, token):
            result = super().complete(request, token)
            if request.stage_id == "faithful-review":
                value = json.loads(result.text)
                value["frames"][0]["caption"] = caption
                return replace(result, text=json.dumps(value))
            return result
    compiler, context = _compiler_context(tmp_path, Executor())
    request = replace(_request(transcript=TranscriptDocument("zh", (replace(SEGMENT, text="支撑在104左右"),))),
        visual_frames=(FRAME,), model_binding=replace(_binding(), provider_type="codex-app-server"))
    result = compiler.compile(request, context)
    assert ("[SCREENSHOT:" in result.markdown) is selected
    assert "支撑在104左右" in project_reading_markdown(result.markdown)


@pytest.mark.parametrize("invalid_repair", [False, True])
def test_truncated_before_gets_one_contract_repair_without_relaxing_validation(tmp_path, invalid_repair):
    class Executor(_FaithfulExecutor):
        def complete(self, request, token):
            result = super().complete(request, token)
            if request.stage_id in {"faithful-source-prepare", "faithful-source-repair"}:
                value = _proposal()
                if request.stage_id == "faithful-source-prepare" or invalid_repair:
                    value["corrections"][0]["before"] = "到这个"
                if request.stage_id == "faithful-source-repair":
                    payload = json.loads(request.user_content)
                    assert "invalid_proposal" in payload
                    assert "EXACTLY" in payload["review_feedback"][0]
                return replace(result, text=json.dumps(value))
            return result
    executor = Executor()
    compiler, context = _compiler_context(tmp_path, executor)
    request = replace(_request(transcript=TranscriptDocument("zh", (SEGMENT,))),
        visual_frames=(FRAME,), model_binding=replace(_binding(), provider_type="codex-app-server"))
    if invalid_repair:
        with pytest.raises(DomainError, match="faithful_source_grounding_invalid"):
            compiler.compile(request, context)
        assert len(executor.requests) == 2
    else:
        result = compiler.compile(request, context)
        assert "到这个1.5左右" in project_reading_markdown(result.markdown)
        assert len(executor.requests) == 6


def test_semantic_chapters_do_not_cut_a_continuing_topic_at_150_seconds():
    transcript = TranscriptDocument("en", tuple(
        TranscriptSegment(f"seg_{index + 1:06d}", index * 100_000, (index + 1) * 100_000, "Topic continues.")
        for index in range(4)))
    request = replace(_request(transcript=transcript), section_input_byte_budget=8192,
                      chapter_start_ids=("seg_000001", "seg_000004"))
    plan = plan_faithful_edition(request)
    assert [(section.start_segment_id, section.end_segment_id) for section in plan.sections] == [
        ("seg_000001", "seg_000003"), ("seg_000004", "seg_000004")]


def test_dense_frames_are_bounded_chronological_and_inside_segments():
    from app.core.application.video_service import build_visual_candidate_plan
    transcript = TranscriptDocument("en", tuple(
        TranscriptSegment(f"seg_{index + 1:06d}", index * 1000, (index + 1) * 1000, "text")
        for index in range(300)))
    plan = build_visual_candidate_plan("job_018f0000-0000-7000-8000-000000000001", transcript, dense=True)
    assert len(plan) == 192
    assert all(item.timestamp_ms % 1000 == 500 for item in plan)
    assert [item.timestamp_ms for item in plan] == sorted(item.timestamp_ms for item in plan)


def test_long_dense_frame_is_near_segment_end_for_late_burned_subtitle():
    from app.core.application.video_service import build_visual_candidate_plan
    transcript = TranscriptDocument("zh", (
        TranscriptSegment("seg_000001", 314_320, 317_521, "4430到4450"),
    ))

    plan = build_visual_candidate_plan(
        "job_018f0000-0000-7000-8000-000000000001", transcript, dense=True,
    )

    assert plan[0].timestamp_ms == 317_021


def test_grounding_declares_exact_frame_eligibility_without_relaxing_time_window():
    segment = TranscriptSegment("seg_000095", 241_200, 244_100, "先行牛骑3或4次向下推动")
    frames = (
        VisualFrame("seg_000083", 220_300, FRAME.payload),
        VisualFrame("seg_000094", 231_200, FRAME.payload),
        VisualFrame("seg_000096", 254_100, FRAME.payload),
        VisualFrame("seg_000097", 254_101, FRAME.payload),
    )
    payload = grounding_payload((segment,), frames, (), ())
    assert payload["allowed_correction_frames"] == {
        segment.segment_id: ["seg_000094", "seg_000096"],
    }


@pytest.mark.parametrize(("timestamp", "allowed"), [(220_300, False), (231_199, False),
                                                   (231_200, True), (254_100, True), (254_101, False)])
def test_declared_frame_window_matches_parser_enforcement(timestamp, allowed):
    segment = TranscriptSegment("seg_000095", 241_200, 244_100, "Disable the switch")
    frame = VisualFrame("seg_000083", timestamp, FRAME.payload)
    data = {"corrections": [{"segment_id": segment.segment_id, "before": segment.text,
        "after": "Enable the switch", "frame_segment_id": frame.segment_id,
        "visible_quote": "Enable the switch", "reason": "Matching subtitle"}],
        "chapter_start_ids": [], "illustration_ids": []}
    if allowed:
        assert parse_grounding(json.dumps(data), (segment,), (frame,), max_response_bytes=16000).corrections
    else:
        with pytest.raises(DomainError, match="faithful_source_grounding_invalid"):
            parse_grounding(json.dumps(data), (segment,), (frame,), max_response_bytes=16000)


@pytest.mark.parametrize(("before", "after", "quote"), [
    ("后面什么时候开始做空", "后面什么时候开始做多", "后面什么时候开始做多"),
    ("80根K线", "10根K线", "10 bar bull Micro channel"),
    ("Disable the switch", "Enable the switch", "Enable the switch"),
])
def test_review_and_repair_get_raw_source_and_unselected_correction_frame(tmp_path, before, after, quote):
    segment = replace(SEGMENT, text=before)

    class Executor(_FaithfulExecutor):
        reviews = 0

        def complete(self, request, token):
            result = super().complete(request, token)
            payload = json.loads(request.user_content)
            if request.stage_id == "faithful-source-prepare":
                data = _proposal()
                data["corrections"][0].update(before=before, after=after, visible_quote=quote)
                data["illustration_ids"] = []
                return replace(result, text=json.dumps(data))
            if request.stage_id == "faithful-review":
                self.reviews += 1
                assert payload["transcript"][0]["text"] == before
                assert payload["corrected_transcript"][0]["text"] == after
                assert payload["source_corrections"][0]["visible_quote"] == quote
                assert request.image_webp == (FRAME.payload,)
                if self.reviews == 1:
                    data = json.loads(result.text)
                    data.update(auxiliary_pass=False, auxiliary_issues=["seg_000001: auxiliary changed meaning"])
                    return replace(result, text=json.dumps(data))
            if request.stage_id == "faithful-semantic-repair":
                assert payload["original_transcript"][0]["text"] == before
                assert payload["section"]["segments"][0]["text"] == after
                assert payload["source_corrections"][0]["visible_quote"] == quote
                assert request.image_webp == (FRAME.payload,)
            return result

    executor = Executor()
    compiler, context = _compiler_context(tmp_path, executor)
    request = replace(_request(transcript=TranscriptDocument("zh", (segment,))),
        visual_frames=(FRAME,), section_input_byte_budget=8192,
        model_binding=replace(_binding(), provider_type="codex-app-server"))
    result = compiler.compile(request, context)
    assert executor.reviews == 2
    assert after in project_reading_markdown(result.markdown)
    assert request.transcript.segments[0].text == before


def test_neighbor_frame_corrects_owned_segment_without_transferring_ownership(tmp_path):
    # A subtitle straddles the 16-segment processing boundary.
    segments = tuple(TranscriptSegment(f"seg_{index + 1:06d}", index * 1000, (index + 1) * 1000,
                                      "Disable the switch" if index == 15 else "The explanation continues.")
                     for index in range(17))
    frame = VisualFrame("seg_000017", 16_500, FRAME.payload)

    class Executor(_FaithfulExecutor):
        def complete(self, request, token):
            result = super().complete(request, token)
            payload = json.loads(request.user_content)
            if request.stage_id == "faithful-source-prepare":
                value = json.loads(result.text)
                value["illustration_ids"] = []
                if payload["segments"][0]["segment_id"] == "seg_000001":
                    assert payload["context_after"][0]["segment_id"] == frame.segment_id
                    assert payload["allowed_correction_frames"]["seg_000016"] == [frame.segment_id]
                    assert request.image_webp == (frame.payload,)
                    value["corrections"] = [{"segment_id": "seg_000016", "before": segments[15].text,
                        "after": "Enable the switch", "frame_segment_id": frame.segment_id,
                        "visible_quote": "Enable the switch", "reason": "Matching subtitle spans boundary"}]
                return replace(result, text=json.dumps(value))
            return result

    compiler, context = _compiler_context(tmp_path, Executor())
    request = replace(_request(transcript=TranscriptDocument("en", segments)),
        visual_frames=(frame,), model_binding=replace(_binding(), provider_type="codex-app-server"))
    grounded, _, _ = compiler._ground_source(request, context)
    assert grounded.source_corrections[0].segment_id == "seg_000016"
    assert grounded.visual_frames == (frame,)
    assert request.transcript.segments[15].text == "Disable the switch"


def test_review_evidence_is_section_owned_with_read_only_context_and_no_image_truncation():
    from app.core.application.faithful_edition_compiler import FaithfulEditionCompiler
    from app.core.recipes.video.faithful_edition.contracts import GroundedCorrection

    segments = tuple(TranscriptSegment(f"seg_{i + 1:06d}", i * 1000, (i + 1) * 1000, "text")
                     for i in range(30))
    frames = tuple(VisualFrame(segment.segment_id, segment.start_ms + 500, FRAME.payload) for segment in segments)
    request = replace(_request(transcript=TranscriptDocument("en", segments)),
        section_input_byte_budget=8192, visual_frames=frames, chapter_start_ids=("seg_000001", "seg_000003"),
        source_corrections=(GroundedCorrection("seg_000002", "text", "corrected", "seg_000003", "corrected", "subtitle"),))
    plan = plan_faithful_edition(request)
    payload, selected = FaithfulEditionCompiler._section_evidence(request, plan.sections[0], segments[:2])
    assert [value["segment_id"] for value in payload["original_transcript"]] == ["seg_000001", "seg_000002"]
    assert [value["segment_id"] for value in payload["context_after"]] == [f"seg_{i:06d}" for i in range(3, 7)]
    assert [frame.segment_id for frame in selected] == ["seg_000001", "seg_000002", "seg_000003"]
    with pytest.raises(DomainError, match="model_request_budget_exceeded"):
        FaithfulEditionCompiler._section_evidence(replace(request, max_request_images=24), plan.sections[1], segments[2:])


@pytest.mark.parametrize("fault", ["missing", "wrong_source", "wrong_ordinal", "empty_facts", "mismatch_with_pass"])
def test_review_requires_complete_fact_checks_and_cannot_override_mismatch_with_pass(tmp_path, fault):
    class Executor(_FaithfulExecutor):
        def complete(self, request, token):
            result = super().complete(request, token)
            if request.stage_id == "faithful-review":
                value = json.loads(result.text)
                if fault == "missing":
                    value["claim_checks"] = []
                elif fault == "wrong_source":
                    value["claim_checks"][0]["source_segment_ids"] = ["seg_999999"]
                elif fault == "wrong_ordinal":
                    value["claim_checks"][0]["paragraph_ordinal"] = 1
                elif fault == "empty_facts":
                    value["claim_checks"][0]["source_facts"] = ""
                else:
                    value["claim_checks"][0].update(matches_source=False,
                        source_facts="Mode A: fast, high memory; Mode B: slow, low memory",
                        candidate_facts="Mode A: slow, low memory; Mode B: fast, high memory")
                    assert value["pass"] is True
                return replace(result, text=json.dumps(value))
            return result

    compiler, context = _compiler_context(tmp_path, Executor())
    error = "faithful_review_failed" if fault == "mismatch_with_pass" else "faithful_review_invalid"
    with pytest.raises(DomainError, match=error):
        compiler.compile(_request(transcript=TranscriptDocument("en", (
            TranscriptSegment("seg_000001", 0, 1000, "Mode A is faster."),))), context)


def test_independent_source_facts_are_blind_and_reused_after_candidate_repair(tmp_path):
    class Executor(_FaithfulExecutor):
        reviews = 0

        def complete(self, request, token):
            result = super().complete(request, token)
            payload = json.loads(request.user_content)
            if request.stage_id == "faithful-facts":
                assert not ({"candidate", "previous_section", "review_feedback", "independent_source_facts"} & payload.keys())
                assert set(payload["section"]) == {"section_ordinal", "segments"}
                assert payload["original_transcript"][0]["text"] == "Mode A is faster."
                return replace(result, text=json.dumps({"facts": "Source-only record: Mode A is faster.", "unresolved_segment_ids": []}))
            if request.stage_id in {"faithful-edit", "faithful-review", "faithful-semantic-repair"}:
                assert payload["independent_source_facts"] == "Source-only record: Mode A is faster."
            if request.stage_id == "faithful-review":
                self.reviews += 1
                if self.reviews == 1:
                    value = json.loads(result.text)
                    value.update(auxiliary_pass=False, auxiliary_issues=["seg_000001: fix auxiliary"])
                    return replace(result, text=json.dumps(value))
            return result

    executor = Executor()
    compiler, context = _compiler_context(tmp_path, executor)
    result = compiler.compile(_request(transcript=TranscriptDocument("en", (
        TranscriptSegment("seg_000001", 0, 1000, "Mode A is faster."),))), context)
    stages = [request.stage_id for request in executor.requests]
    assert stages == ["faithful-facts", "faithful-edit", "faithful-review", "faithful-semantic-repair", "faithful-review"]
    assert result.execution_summary.model_operation_count == len(stages)
    assert "Source-only record" not in result.markdown


@pytest.mark.parametrize("response", ['{}', '{"facts":""}', '{"facts":42}', '{"facts":"a","facts":"b"}'])
def test_invalid_blind_source_facts_block_before_editing(tmp_path, response):
    class Executor(_FaithfulExecutor):
        def complete(self, request, token):
            result = super().complete(request, token)
            return replace(result, text=response) if request.stage_id == "faithful-facts" else result

    executor = Executor()
    compiler, context = _compiler_context(tmp_path, executor)
    with pytest.raises(DomainError, match="faithful_source_facts_invalid"):
        compiler.compile(_request(transcript=TranscriptDocument("en", (
            TranscriptSegment("seg_000001", 0, 1000, "Mode A is faster."),))), context)
    assert [request.stage_id for request in executor.requests] == ["faithful-facts"]


@pytest.mark.parametrize("always_invent", [False, True])
def test_unresolved_source_cannot_be_resolved_even_if_reviewer_would_pass(tmp_path, always_invent):
    segments = (
        TranscriptSegment("seg_000001", 0, 1000, "Later validation costs more."),
        TranscriptSegment("seg_000002", 1000, 2000, "Here the cost is lower."),
        TranscriptSegment("seg_000003", 2000, 3000, "Here the cost is higher."),
    )

    class Executor(_FaithfulExecutor):
        def complete(self, request, token):
            result = super().complete(request, token)
            if request.stage_id == "faithful-facts":
                return replace(result, text=json.dumps({"facts": "Later costs more. The two 'here' references are unresolved.",
                    "unresolved_segment_ids": ["seg_000002", "seg_000003"]}))
            if request.stage_id == "faithful-edit" or (always_invent and request.stage_id == "faithful-repair"):
                value = json.loads(result.text)
                value["paragraphs"][0]["text"] = "Later validation costs more. Early validation costs more; later validation costs less."
                return replace(result, text=json.dumps(value))
            return result

    executor = Executor()
    compiler, context = _compiler_context(tmp_path, executor)
    request = replace(_request(transcript=TranscriptDocument("en", segments)), section_input_byte_budget=8192)
    result = compiler.compile(request, context)
    assert "Here the cost is lower." in result.markdown
    assert "Early validation costs more" not in result.markdown
    assert "faithful_unresolved_paragraph_restored" in result.warnings
    assert [value.stage_id for value in executor.requests] == [
        "faithful-facts", "faithful-edit", "faithful-review"]


def test_unresolved_claims_are_not_invented_in_optional_summaries(tmp_path):
    class Executor(_FaithfulExecutor):
        def complete(self, request, token):
            result = super().complete(request, token)
            if request.stage_id == "faithful-facts":
                return replace(result, text=json.dumps({"facts": "The referent of 'here' is unresolved.",
                                                       "unresolved_segment_ids": ["seg_000001"]}))
            if request.stage_id == "faithful-edit":
                value = json.loads(result.text)
                value["summary"]["text"] = "Invented association"
                value["key_points"][0]["text"] = "Invented association"
                return replace(result, text=json.dumps(value))
            return result

    compiler, context = _compiler_context(tmp_path, Executor())
    result = compiler.compile(_request(transcript=TranscriptDocument("en", (
        TranscriptSegment("seg_000001", 0, 1000, "Here the cost is lower."),))), context)
    assert "Here the cost is lower." in result.markdown
    assert "Invented association" not in result.markdown

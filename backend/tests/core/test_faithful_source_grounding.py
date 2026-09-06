from dataclasses import replace
import json

import pytest

from app.core.domain.video import TranscriptDocument, TranscriptSegment
from app.core.domain.visual_frame import VisualFrame
from app.core.errors import DomainError
from app.core.recipes.video.faithful_edition.source_grounding import parse_grounding
from app.core.recipes.video.faithful_edition.pipeline import plan_faithful_edition
from app.core.application.artifact_query_service import project_reading_markdown
from test_faithful_edition_compiler import _FaithfulExecutor, _compiler_context, _request, _binding


SEGMENT = TranscriptSegment("seg_000001", 0, 1000, "到这个1.25左右")
FRAME = VisualFrame(SEGMENT.segment_id, 500, b"RIFF\x04\x00\x00\x00WEBP")


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
    overview = result.markdown.split("## 全文概览（AI）")[1].split("## 精编正文")[0]
    assert "[^seg_000001]" in overview
    assert before == request.transcript.segments[0].text
    assert result.text_assessment.metrics.number_mismatch_count == 0
    log = json.loads(result.markdown.split("```alltonote-edit-log-v1\n")[1].split("\n```")[0])
    assert log["records"][0]["before"] == before
    assert log["records"][0]["visible_quote"] == quote
    assert compiler.compile(request, context).markdown == result.markdown
    assert len(executor.requests) == 4


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
    assert len(executor.requests) == 6


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
        assert len(executor.requests) == 5


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

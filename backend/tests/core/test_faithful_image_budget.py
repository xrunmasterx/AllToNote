from dataclasses import replace
import json

import pytest

from app.core.application.faithful_edition_compiler import FaithfulEditionCompiler
from app.core.domain.video import TranscriptDocument, TranscriptSegment
from app.core.domain.visual_frame import VisualFrame
from app.core.errors import DomainError
from app.core.portable.webp_dimensions import webp_dimensions
from app.core.recipes.video.faithful_edition.pipeline import plan_faithful_edition
from app.core.recipes.video.faithful_edition.contracts import GroundedCorrection
from tests.core.test_faithful_edition_compiler import _request, _compiler_context, _FaithfulExecutor


def header(width=1920, height=1080):
    chunk = b"\0" * 4 + (width-1).to_bytes(3, "little") + (height-1).to_bytes(3, "little")
    return b"RIFF\x16\0\0\0WEBPVP8X\x0a\0\0\0" + chunk


def request_with_frames(count=50):
    segments = tuple(TranscriptSegment(f"seg_{i+1:06d}", i*1000, (i+1)*1000, "Preserve this condition.") for i in range(count))
    request = _request(transcript=TranscriptDocument("en", segments))
    return replace(request, section_input_byte_budget=12000, max_request_bytes=112000,
                   model_binding=replace(request.model_binding, provider_type="codex-app-server"),
                   chapter_start_ids=(segments[0].segment_id,),
                   visual_frames=tuple(VisualFrame(s.segment_id, s.start_ms+500, header()) for s in segments))


def test_35_images_fit_and_reach_the_real_model_request_contract(tmp_path, monkeypatch):
    executor = _FaithfulExecutor()
    compiler, context = _compiler_context(tmp_path, executor)
    monkeypatch.setattr(compiler, "_ground_source", lambda req, ctx: (req, [], 0))
    result = compiler.compile(request_with_frames(35), context)
    assert len(result.plan.sections) == 1
    assert all(len(r.image_webp) == 35 for r in executor.requests)
    assert {r.stage_id for r in executor.requests} == {"faithful-facts", "faithful-edit", "faithful-review"}
    review_request = next(r for r in executor.requests if r.stage_id == "faithful-review")
    schema = json.loads(review_request.response_schema_json)["properties"]
    assert schema["frames"]["minItems"] == schema["frames"]["maxItems"] == 35
    assert schema["claim_checks"]["minItems"] == schema["claim_checks"]["maxItems"] == 1


@pytest.mark.parametrize("budget", [{"max_request_images": 30},
                                   {"max_request_image_bytes": 30*30},
                                   {"max_request_image_pixels": 30*1920*1080}])
def test_splits_preserve_all_evidence_and_one_reading_chapter(tmp_path, monkeypatch, budget):
    request = replace(request_with_frames(), **budget)
    executor = _FaithfulExecutor()
    compiler, context = _compiler_context(tmp_path, executor)
    monkeypatch.setattr(compiler, "_ground_source", lambda req, ctx: (req, [], 0))
    result = compiler.compile(request, context)
    assert len(result.plan.sections) == 2
    assert len({s.reading_chapter_id for s in result.plan.sections}) == 1
    edits = sorted((r for r in executor.requests if r.stage_id == "faithful-edit"),
                   key=lambda r: json.loads(r.user_content)["section"]["section_ordinal"])
    owned = [s["segment_id"] for r in edits for s in json.loads(r.user_content)["section"]["segments"]]
    assert owned == [s.segment_id for s in request.transcript.segments]
    assert sum(len(r.image_webp) for r in edits) == 50
    assert len(json.loads(edits[1].user_content)["context_before"]) == 4
    body = result.markdown.split("## 正文\n", 1)[1].split("## AI 辅助摘要", 1)[0]
    assert body.count("### ") == 1
    assert "00:00–00:50" in body


def test_late_unsplittable_image_fails_before_any_section_calls(tmp_path, monkeypatch):
    request = request_with_frames(3)
    request = replace(request, chapter_start_ids=("seg_000001", "seg_000003"),
                      visual_frames=(*request.visual_frames[:2], VisualFrame("seg_000003", 2500, header(16000,16000))))
    executor = _FaithfulExecutor()
    compiler, context = _compiler_context(tmp_path, executor)
    monkeypatch.setattr(compiler, "_ground_source", lambda req, ctx: (req, [], 0))
    with pytest.raises(DomainError, match="model_request_budget_exceeded"):
        compiler.compile(request, context)
    assert executor.requests == []


def test_split_retains_neighbor_correction_frame():
    request = request_with_frames(4)
    request = replace(request, max_request_images=2, source_corrections=(GroundedCorrection(
        "seg_000002", "Preserve this condition.", "Preserve that condition.", "seg_000003", "that", "subtitle"),))
    compiler = object.__new__(FaithfulEditionCompiler)
    plan = compiler._fit_evidence_plan(request, plan_faithful_edition(request))
    refs = [r for r in plan.sections if r.start_segment_ordinal <= 1 < r.end_segment_ordinal_exclusive]
    assert len(refs) == 1
    payload, frames = compiler._section_evidence(request, refs[0], compiler._section_segments(request, plan, refs[0]))
    assert "seg_000003" in {f.segment_id for f in frames}
    assert payload["source_corrections"][0]["frame_segment_id"] == "seg_000003"
    assert sum(s.editable_segment_count for s in plan.sections) == 4


def test_full_text_evidence_budget_can_split_a_text_only_plan():
    request = request_with_frames(30)
    segments = tuple(replace(s, text="Preserve the condition and its consequence. " * 3)
                     for s in request.transcript.segments)
    base = _request(transcript=TranscriptDocument("en", segments))
    request = replace(base, visual_frames=request.visual_frames, chapter_start_ids=(segments[0].segment_id,),
                      section_input_byte_budget=12000, max_request_bytes=16384)
    initial = plan_faithful_edition(request)
    assert len(initial.sections) == 1
    compiler = object.__new__(FaithfulEditionCompiler)
    plan = compiler._fit_evidence_plan(request, initial)
    assert len(plan.sections) > 1
    assert sum(s.editable_segment_count for s in plan.sections) == 30
    assert len({s.reading_chapter_id for s in plan.sections}) == 1


def test_reader_merge_does_not_resurrect_failed_auxiliary(tmp_path, monkeypatch):
    executor = _FaithfulExecutor()
    compiler, context = _compiler_context(tmp_path, executor)
    request = replace(request_with_frames(4), max_request_images=2)
    monkeypatch.setattr(compiler, "_ground_source", lambda req, ctx: (req, [], 0))
    result = compiler.compile(request, context)
    from app.core.recipes.video.faithful_edition.pipeline import parse_faithful_section
    edits = sorted((r for r in executor.requests if r.stage_id == "faithful-edit"),
                   key=lambda r: json.loads(r.user_content)["section"]["section_ordinal"])
    sections = [parse_faithful_section(executor.complete(r, None).text, section_ref=ref,
        allowed_segment_ids=tuple(s["segment_id"] for s in json.loads(r.user_content)["section"]["segments"]),
        limits=request.parser_limits) for r, ref in zip(edits, result.plan.sections)]
    sections[0] = replace(sections[0], summary=replace(sections[0].summary, text="REJECTED"),
                          key_points=(replace(sections[0].key_points[0], text="REJECTED"),))
    sections[1] = replace(sections[1], summary=replace(sections[1].summary, text="APPROVED"))
    markdown = compiler._assemble_markdown(request, sections, plan=result.plan, excluded_auxiliaries=frozenset({0}))
    assert "REJECTED" not in markdown
    assert "APPROVED" in markdown


def test_webp_dimensions_include_extended_lossless_lossy_and_padding():
    assert webp_dimensions(header()) == (1920, 1080)
    assert webp_dimensions(bytes.fromhex("524946461a000000574542505650384c0d0000002f00000000071011118888fe0700")) == (1,1)
    lossy = b"\0\0\0\x9d\x01\x2a" + (640).to_bytes(2, "little") + (480).to_bytes(2, "little")
    assert webp_dimensions(b"RIFF\x16\0\0\0WEBPVP8 \x0a\0\0\0" + lossy) == (640,480)
    assert webp_dimensions(header()[:12] + b"JUNK\x01\0\0\0x\0" + header()[12:]) == (1920,1080)
    with pytest.raises(DomainError, match="dimensions_invalid"):
        webp_dimensions(b"RIFF\x04\0\0\0WEBP")


@pytest.mark.parametrize("value", [0, -1, True, 193])
def test_invalid_image_count_budget_is_rejected(value):
    with pytest.raises(DomainError, match="contract_invalid"):
        replace(request_with_frames(1), max_request_images=value)

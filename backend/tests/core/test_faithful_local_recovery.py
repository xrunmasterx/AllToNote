"""Local recovery must preserve evidence gates without repeating whole sections."""
from dataclasses import replace
import json

import pytest

from app.core.domain.video import TranscriptDocument, TranscriptSegment
from app.core.errors import DomainError
from app.core.recipes.video.faithful_edition.source_grounding import isolate_grounding_corrections
from test_faithful_edition_compiler import _FaithfulExecutor, _compiler_context, _request, _binding, _visual_request
from test_faithful_source_grounding import SEGMENT, FRAME, _proposal


OTHER = TranscriptSegment("seg_000002", 1000, 2000, "The value is 0.4.")


def mixed_proposal():
    proposal = _proposal()
    proposal["corrections"].append(dict(segment_id=OTHER.segment_id, before=OTHER.text,
        after="The value is 0.6.", frame_segment_id="", visible_quote="", reason="Guess"))
    return proposal


def test_isolates_only_owned_invalid_correction():
    grounding, omitted = isolate_grounding_corrections(json.dumps(mixed_proposal()),
        (SEGMENT, OTHER), (FRAME,), max_response_bytes=16000)
    assert len(grounding.corrections) == 1
    assert grounding.corrections[0].segment_id == SEGMENT.segment_id
    assert omitted == (OTHER.segment_id,)


@pytest.mark.parametrize("fault", ["unknown", "duplicate", "container", "all_invalid", "boundary"])
def test_isolation_does_not_salvage_ambiguous_or_globally_invalid_contract(fault):
    proposal = mixed_proposal()
    if fault == "unknown": proposal["corrections"][1]["segment_id"] = "seg_999999"
    if fault == "duplicate": proposal["corrections"].append(proposal["corrections"][0])
    if fault == "container": proposal["corrections"] = "invalid"
    if fault == "all_invalid": proposal["corrections"] = proposal["corrections"][1:]
    if fault == "boundary": proposal["chapter_start_ids"] = ["seg_999999"]
    with pytest.raises(DomainError):
        isolate_grounding_corrections(json.dumps(proposal), (SEGMENT, OTHER), (FRAME,), max_response_bytes=16000)


@pytest.mark.parametrize("review_pass", [True, False])
def test_isolated_corrections_still_require_independent_review(tmp_path, review_pass):
    class Executor(_FaithfulExecutor):
        def complete(self, request, token):
            result = super().complete(request, token)
            if request.stage_id in {"faithful-source-prepare", "faithful-source-repair"}:
                return replace(result, text=json.dumps(mixed_proposal()))
            if request.stage_id in {"faithful-source-check", "faithful-source-recheck"}:
                payload = json.loads(request.user_content)
                assert [s["segment_id"] for s in payload["segments"]] == [SEGMENT.segment_id]
                assert [s["segment_id"] for s in payload["read_only_unreviewed_segments"]] == [OTHER.segment_id]
                return replace(result, text=json.dumps({"pass": review_pass,
                    "issues": [] if review_pass else ["Correction unsupported"]}))
            return result
    executor = Executor()
    compiler, context = _compiler_context(tmp_path, executor)
    request = replace(_request(transcript=TranscriptDocument("zh", (SEGMENT, OTHER))),
        visual_frames=(FRAME,), section_input_byte_budget=8192, allow_partial_fallback=True,
        model_binding=replace(_binding(), provider_type="codex-app-server"))
    grounded, results, _ = compiler._ground_source(request, context)
    assert len(grounded.source_corrections) == int(review_pass)
    assert OTHER.segment_id in grounded.unresolved_source_ids
    assert (SEGMENT.segment_id in grounded.unresolved_source_ids) is not review_pass
    assert grounded.transcript.segments[1].text == OTHER.text
    if review_pass:
        assert any("source_correction_omitted:seg_000002" in r.warnings for r in results)
        cached, _, _ = compiler._ground_source(request, context)
        assert cached == grounded
        assert len(executor.requests) == 2
        compiled = compiler.compile(request, context)
        assert "source_correction_omitted:seg_000002" in compiled.warnings
        assert any(c.check_id == "partial_source_corrections" for c in compiled.text_assessment.checks)


def test_source_fallback_marker_is_not_a_model_wave(tmp_path):
    class Executor(_FaithfulExecutor):
        def complete(self, request, token):
            result = super().complete(request, token)
            if request.stage_id in {"faithful-source-check", "faithful-source-recheck"}:
                return replace(result, text=json.dumps({"pass": False, "issues": ["Not supported"]}))
            return result
    executor = Executor()
    compiler, context = _compiler_context(tmp_path, executor)
    request = replace(_request(transcript=TranscriptDocument("zh", (SEGMENT,))),
        visual_frames=(FRAME,), allow_partial_fallback=True,
        model_binding=replace(_binding(), provider_type="codex-app-server"))
    _, results, waves = compiler._ground_source(request, context)
    assert len(results) == 5  # Four model calls and one local fallback marker.
    assert len(executor.requests) == 4
    assert waves == 4


def test_source_fallback_and_body_repairs_can_finish_at_ten_model_waves(tmp_path):
    class Executor(_FaithfulExecutor):
        reviews = 0

        def complete(self, request, token):
            result = super().complete(request, token)
            if request.stage_id in {"faithful-source-check", "faithful-source-recheck"}:
                return replace(result, text=json.dumps({"pass": False, "issues": ["Not supported"]}))
            if request.stage_id == "faithful-review":
                self.reviews += 1
                if self.reviews == 1:
                    data = json.loads(result.text)
                    data.update({"pass": False, "issues": ["Recheck wording against source"]})
                    return replace(result, text=json.dumps(data))
            return result
    executor = Executor(invalid_contract_once=True)
    compiler, context = _compiler_context(tmp_path, executor)
    compiled = compiler.compile(replace(_visual_request(), allow_partial_fallback=True), context)
    assert compiled.execution_summary.sequential_model_waves == 10
    assert len(executor.requests) == 10
    assert "faithful_local_fallback" in compiled.warnings


@pytest.mark.parametrize("edited", ["It moves here.", "It does not move here, right?", "It moves 99 times."])
def test_unresolved_paragraph_restored_locally_before_review(tmp_path, edited):
    segments = (TranscriptSegment("seg_000001", 0, 1000, "It moves here, right?"),
                TranscriptSegment("seg_000002", 1000, 2000, "Next, it stops."))
    class Executor(_FaithfulExecutor):
        def complete(self, request, token):
            result = super().complete(request, token)
            data = json.loads(result.text)
            if request.stage_id == "faithful-facts":
                data["unresolved_segment_ids"] = [segments[0].segment_id]
            if request.stage_id == "faithful-edit":
                data["paragraphs"] = [
                    dict(paragraph_ordinal=0, text=edited, source_segment_ids=[segments[0].segment_id]),
                    dict(paragraph_ordinal=1, text="Next it stops.", source_segment_ids=[segments[1].segment_id])]
            if request.stage_id == "faithful-review":
                candidate = json.loads(request.user_content)["section"]
                assert candidate["paragraphs"][0]["text"] == segments[0].text
                assert candidate["paragraphs"][1]["text"] == "Next it stops."
                assert not candidate["summary"]["text"]
                assert not candidate["key_points"]
            return replace(result, text=json.dumps(data))
    executor = Executor()
    compiler, context = _compiler_context(tmp_path, executor)
    request = replace(_request(transcript=TranscriptDocument("en", segments)),
        section_input_byte_budget=8192,
        model_binding=replace(_binding(), provider_type="codex-app-server"))
    compiled = compiler.compile(request, context)
    assert "faithful_unresolved_paragraph_restored" in compiled.warnings
    assert [r.stage_id for r in executor.requests] == ["faithful-facts", "faithful-edit", "faithful-review"]
    assert compiled.execution_summary.body_segment_reference_coverage_ratio == 1.0

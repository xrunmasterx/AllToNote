from dataclasses import replace
import json

import pytest

from app.core.domain.video import QualityOverall
from app.core.errors import DomainError, ErrorCategory
from app.core.recipes.video.faithful_edition.quality import QualityCheckStatus
from test_faithful_edition_compiler import _FaithfulExecutor, _compiler_context, _request
from test_faithful_source_grounding import FRAME


@pytest.mark.parametrize("source,target", [("剩下9分之4", "剩下4/9"), ("12分之1", "1/12"),
                                          ("５／９和９分之４", "5/9和4/9")])
def test_equivalent_integer_fraction_spelling_is_not_a_numeric_failure(source, target):
    from app.core.recipes.video.faithful_edition.quality import _anchors, _NUMBER, _retains_anchors
    assert _retains_anchors(_anchors(_NUMBER, source), _anchors(_NUMBER, target))


@pytest.mark.parametrize("source,target", [("9分之4", "9/4"), ("9分之4", "5/9"), ("12分之1", "1/21")])
def test_changed_fraction_value_still_fails(source, target):
    from app.core.recipes.video.faithful_edition.quality import _anchors, _NUMBER, _retains_anchors
    assert not _retains_anchors(_anchors(_NUMBER, source), _anchors(_NUMBER, target))


@pytest.mark.parametrize("fault", ["invalid_contract_always", "omit_number_always"])
def test_failed_local_repair_keeps_source_and_other_sections(tmp_path, fault):
    executor = _FaithfulExecutor(**{fault: True})
    compiler, context = _compiler_context(tmp_path, executor)
    request = replace(_request(), allow_partial_fallback=True)
    result = compiler.compile(request, context)
    assert result.text_assessment.overall is QualityOverall.PASS_WITH_WARNINGS
    assert result.execution_summary.body_segment_reference_coverage_ratio == 1.0
    assert "42" in result.markdown and "50" in result.markdown
    assert "faithful_local_fallback" in result.warnings
    assert "Section 2" in result.markdown
    assert not result.usage.token_counts_complete


@pytest.mark.parametrize("stage", ["faithful-source-check", "faithful-facts", "faithful-review"])
def test_invalid_model_payload_is_local_not_document_failure(tmp_path, stage):
    class Executor(_FaithfulExecutor):
        def complete(self, request, token):
            value = super().complete(request, token)
            if request.stage_id == stage or (stage == "faithful-source-check" and request.stage_id == "faithful-source-recheck"):
                return replace(value, text="{}")
            return value

    compiler, context = _compiler_context(tmp_path, Executor())
    request = replace(_request(), allow_partial_fallback=True, max_request_bytes=48000,
                      model_binding=replace(_request().model_binding, provider_type="codex-app-server"),
                      visual_frames=(FRAME,))
    result = compiler.compile(request, context)
    assert result.text_assessment.overall is QualityOverall.PASS_WITH_WARNINGS
    assert result.execution_summary.body_segment_reference_coverage_ratio == 1.0
    assert "faithful_local_fallback" in result.warnings
    check = next(c for c in result.text_assessment.checks if c.check_id == "faithful_semantic_review")
    assert check.status is QualityCheckStatus.WARNING
    if stage in {"faithful-facts", "faithful-review"}:
        assert FRAME.segment_id in {f.segment_id for f in result.screenshot_requests}


def test_rejected_source_correction_never_enters_body(tmp_path):
    from app.core.domain.video import TranscriptDocument, TranscriptSegment
    from app.core.application.artifact_query_service import project_reading_markdown
    source = TranscriptDocument("zh", (TranscriptSegment("seg_000001", 0, 1000, "小反转进入震荡趋监。"),))

    class Executor(_FaithfulExecutor):
        def complete(self, request, token):
            value = super().complete(request, token)
            if request.stage_id in {"faithful-source-prepare", "faithful-source-repair"}:
                return replace(value, text=json.dumps({"corrections": [{"segment_id": "seg_000001",
                    "before": source.segments[0].text, "after": "小反转进入震荡趋势。",
                    "frame_segment_id": "", "reason": "proposed typo correction", "visible_quote": ""}],
                    "chapter_start_ids": [], "illustration_ids": [FRAME.segment_id]}))
            if request.stage_id in {"faithful-source-check", "faithful-source-recheck"}:
                return replace(value, text=json.dumps({"pass": False, "issues": ["unsupported terminology correction"]}))
            return value

    compiler, context = _compiler_context(tmp_path, Executor())
    request = replace(_request(transcript=source), allow_partial_fallback=True, max_request_bytes=48000,
                      section_input_byte_budget=1000,
                      model_binding=replace(_request().model_binding, provider_type="codex-app-server"),
                      visual_frames=(FRAME,))
    result = compiler.compile(request, context)
    assert result.text_assessment.overall is QualityOverall.PASS_WITH_WARNINGS
    assert result.execution_summary.body_segment_reference_coverage_ratio == 1.0
    assert "震荡趋势" not in project_reading_markdown(result.markdown)
    assert "震荡趋监" in project_reading_markdown(result.markdown)


def test_auxiliary_failure_omits_summary_without_paid_repair(tmp_path):
    class Executor(_FaithfulExecutor):
        def complete(self, request, token):
            value = super().complete(request, token)
            if request.stage_id == "faithful-review":
                payload = json.loads(value.text)
                payload.update(auxiliary_pass=False, auxiliary_issues=["summary is unsupported"])
                return replace(value, text=json.dumps(payload))
            return value

    executor = Executor()
    compiler, context = _compiler_context(tmp_path, executor)
    result = compiler.compile(replace(_request(), allow_partial_fallback=True), context)
    assert result.text_assessment.overall is QualityOverall.PASS_WITH_WARNINGS
    assert "faithful_auxiliary_omitted_after_review" in result.warnings
    assert not any("repair" in r.stage_id for r in executor.requests)
    assert "faithful_local_fallback" not in result.warnings


def test_capacity_exhaustion_outputs_source_without_claiming_model_success(tmp_path):
    executor = _FaithfulExecutor(provider_error=DomainError("model_capacity_exhausted", ErrorCategory.RECIPE_FAILED, "capacity"))
    compiler, context = _compiler_context(tmp_path, executor)
    result = compiler.compile(replace(_request(), allow_partial_fallback=True), context)
    assert result.text_assessment.overall is QualityOverall.PASS_WITH_WARNINGS
    assert result.execution_summary.model_operation_count == 0
    assert result.execution_summary.body_segment_reference_coverage_ratio == 1.0


def test_missing_optional_frame_review_does_not_discard_good_body(tmp_path):
    class Executor(_FaithfulExecutor):
        def complete(self, request, token):
            value = super().complete(request, token)
            if request.stage_id == "faithful-review":
                payload = json.loads(value.text)
                payload["frames"] = []
                return replace(value, text=json.dumps(payload))
            return value

    executor = Executor()
    compiler, context = _compiler_context(tmp_path, executor)
    request = replace(_request(), allow_partial_fallback=True, max_request_bytes=48000,
                      model_binding=replace(_request().model_binding, provider_type="codex-app-server"),
                      visual_frames=(FRAME,))
    result = compiler.compile(request, context)
    assert result.text_assessment.overall is QualityOverall.PASS_WITH_WARNINGS
    assert "faithful_optional_frames_omitted" in result.warnings
    assert "faithful_local_fallback" not in result.warnings
    assert not any("repair" in r.stage_id for r in executor.requests)
    assert result.screenshot_requests == ()


def test_zero_repair_budget_still_keeps_source(tmp_path):
    compiler, context = _compiler_context(tmp_path, _FaithfulExecutor(invalid_contract_always=True))
    result = compiler.compile(replace(_request(), allow_partial_fallback=True, max_repair_attempts=0), context)
    assert result.text_assessment.overall is QualityOverall.PASS_WITH_WARNINGS
    assert result.execution_summary.body_segment_reference_coverage_ratio == 1.0


def test_source_fallback_does_not_reintroduce_music_markers(tmp_path):
    from app.core.domain.video import TranscriptDocument, TranscriptSegment
    source = TranscriptDocument("en", (
        TranscriptSegment("seg_000001", 0, 1000, "Keep 42 items."),
        TranscriptSegment("seg_000002", 1000, 2000, "[music]"),
        TranscriptSegment("seg_000003", 2000, 3000, "Keep 50 items.")))
    request = replace(_request(transcript=source), section_input_byte_budget=1000, allow_partial_fallback=True)
    compiler, context = _compiler_context(tmp_path, _FaithfulExecutor(
        provider_error=DomainError("model_capacity_exhausted", ErrorCategory.RECIPE_FAILED, "capacity")))
    result = compiler.compile(request, context)
    assert result.cited_segment_ids == ("seg_000001", "seg_000003")
    assert result.text_assessment.overall is QualityOverall.PASS_WITH_WARNINGS


@pytest.mark.parametrize("code,category", [
    ("model_policy_denied", ErrorCategory.POLICY_DENIED),
    ("model_authentication_failed", ErrorCategory.POLICY_DENIED),
    ("external_outcome_unknown", ErrorCategory.CONFLICT),
    ("job_cancelled", ErrorCategory.CANCELLED),
])
def test_partial_mode_does_not_swallow_policy_auth_unknown_or_cancel(tmp_path, code, category):
    compiler, context = _compiler_context(tmp_path, _FaithfulExecutor(provider_error=DomainError(code, category, "stop")))
    with pytest.raises(DomainError, match=code):
        compiler.compile(replace(_request(), allow_partial_fallback=True), context)

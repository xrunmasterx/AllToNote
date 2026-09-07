from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.adapters.models.model_result_store import ModelOperationResultStore
from app.core.application.faithful_edition_compiler import (
    FaithfulEditionCompiler,
    FaithfulEditionRequestV1,
)
from app.core.application.model_call_coordinator import (
    ModelCallCoordinator,
    ModelCallExecution,
)
from app.core.application.artifact_query_service import project_reading_markdown
from app.core.domain.visual_frame import VisualFrame
from app.core.application.video_compiler import VideoCompilationContext
from app.core.domain.video import (
    FaithfulLanguagePolicy,
    JobState,
    TranscriptDocument,
    TranscriptSegment,
    VideoDocumentKind,
)
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.cancellation import CancellationToken
from app.core.ports.model_executor import (
    ModelExecutionBinding,
    ModelExecutionRequest,
    ModelExecutionResult,
    ModelFinishReason,
)
from app.core.recipes.video.compilation.contracts import (
    CompilationQualityProfile,
    TranscriptBasis,
    TranscriptQualityInputV1,
)
from app.core.recipes.video.compilation.pipeline import assess_transcript_quality
from app.core.recipes.video.faithful_edition.contracts import (
    FaithfulAuxiliaryTextV1,
    FaithfulEditionParserLimitsV1,
    FaithfulEditionSectionV1,
    FaithfulParagraphV1,
)
from app.core.recipes.video.faithful_edition.pipeline import (
    parse_faithful_section,
    plan_faithful_edition,
)
from app.core.recipes.video.faithful_edition.quality import (
    FaithfulEditionCandidateV1,
    FaithfulTextAssessmentV1,
    assess_faithful_edition,
)
from app.core.recipes.video.faithful_edition.review import REVIEW_INSTRUCTION


def test_review_instruction_ignores_surface_and_screen_only_differences():
    assert "equivalent numeral spelling" in REVIEW_INSTRUCTION
    assert "screen-only label" in REVIEW_INSTRUCTION
    assert "adjacent segment" in REVIEW_INSTRUCTION


class _FaithfulExecutor:
    def __init__(
        self,
        *,
        omit_number_once: bool = False,
        omit_number_always: bool = False,
        invalid_contract_once: bool = False,
        invalid_contract_always: bool = False,
        invalid_contract_kind: str = "missing",
        provider_error: DomainError | None = None,
    ) -> None:
        self._omit_number_once = omit_number_once
        self._omit_number_always = omit_number_always
        self._invalid_contract_once = invalid_contract_once
        self._invalid_contract_always = invalid_contract_always
        self._invalid_contract_kind = invalid_contract_kind
        self._provider_error = provider_error
        self._omitted = False
        self._invalidated = False
        self._lock = threading.Lock()
        self.requests: list[ModelExecutionRequest] = []

    def complete(self, request: ModelExecutionRequest, token: object) -> ModelExecutionResult:
        payload = json.loads(request.user_content)
        with self._lock:
            self.requests.append(request)
            ordinal = len(self.requests)
        if self._provider_error is not None:
            raise self._provider_error
        if request.stage_id.startswith("faithful-source-"):
            data = ({"corrections": [], "chapter_start_ids": [],
                     "illustration_ids": [frame["segment_id"] for frame in payload["frames"][:2]]}
                    if request.stage_id in {"faithful-source-prepare", "faithful-source-repair"} else {"pass": True, "issues": []})
            return ModelExecutionResult(text=json.dumps(data), actual_model_identity="fixture/model-v1",
                input_tokens=100, output_tokens=50, finish_reason=ModelFinishReason.STOP,
                provider_request_id=f"req_{ordinal}")
        if request.stage_id == "faithful-review":
            return ModelExecutionResult(
                text=json.dumps({"pass": True, "issues": [], "auxiliary_pass": True, "auxiliary_issues": [], "uncertainties": [],
                                 "frames": [{"segment_id": frame["segment_id"], "use": True,
                                             "caption": "A visible chart."} for frame in payload["frames"]]}),
                actual_model_identity="fixture/model-v1", input_tokens=100, output_tokens=50,
                finish_reason=ModelFinishReason.STOP, provider_request_id=f"req_{ordinal}",
            )
        section = payload["section"]
        segments = section["segments"]
        text = " ".join(value["text"] for value in segments)
        if (
            (self._omit_number_once or self._omit_number_always)
            and request.stage_id in {"faithful-edit", "faithful-repair"}
            and (not self._omitted or self._omit_number_always)
            and "42" in text
        ):
            self._omitted = True
            text = text.replace("42", "the value")
        response = {
            "schema_version": 1,
            "section_id": section["section_id"],
            "section_ordinal": section["section_ordinal"],
            "title": f"Section {section['section_ordinal'] + 1}",
            "paragraphs": [
                {
                    "paragraph_ordinal": 0,
                    "text": text,
                    "source_segment_ids": [
                        value["segment_id"] for value in segments
                    ],
                }
            ],
            "summary": {
                "text": "Section summary",
                "source_segment_ids": [segments[0]["segment_id"]],
            },
            "key_points": [
                {
                    "key_point_ordinal": 0,
                    "text": "Key point",
                    "source_segment_ids": [segments[0]["segment_id"]],
                }
            ],
            "uncertainties": [],
            "warnings": [],
        }
        if (
            (self._invalid_contract_once or self._invalid_contract_always)
            and request.stage_id in {"faithful-edit", "faithful-repair"}
            and (not self._invalidated or self._invalid_contract_always)
            and section["section_ordinal"] == 0
        ):
            self._invalidated = True
            source_ids = response["paragraphs"][0]["source_segment_ids"]
            if self._invalid_contract_kind == "missing":
                response["paragraphs"][0]["source_segment_ids"] = source_ids[:-1]
            elif self._invalid_contract_kind == "duplicate":
                response["paragraphs"][0]["source_segment_ids"] = [
                    source_ids[0],
                    *source_ids,
                ]
            elif self._invalid_contract_kind == "out-of-order":
                response["paragraphs"][0]["source_segment_ids"] = list(
                    reversed(source_ids)
                )
            elif self._invalid_contract_kind == "summary-unknown":
                response["summary"]["source_segment_ids"] = ["seg_999999"]
            else:  # pragma: no cover - fixture contract
                raise AssertionError(self._invalid_contract_kind)
        return ModelExecutionResult(
            text=json.dumps(response, ensure_ascii=False),
            actual_model_identity="fixture/model-v1",
            input_tokens=100,
            output_tokens=50,
            finish_reason=ModelFinishReason.STOP,
            provider_request_id=f"req_{ordinal}",
        )


def _transcript() -> TranscriptDocument:
    return TranscriptDocument(
        "en",
        (
            TranscriptSegment(
                "seg_000001", 0, 1_000, "Version API-v2 may process 42 items."
            ),
            TranscriptSegment(
                "seg_000002", 1_000, 2_000, "It must not exceed 50 percent."
            ),
            TranscriptSegment(
                "seg_000003", 2_000, 3_000, "For example, use path /v2/items."
            ),
            TranscriptSegment(
                "seg_000004", 3_000, 4_000, "However, this is only a limit."
            ),
        ),
    )


def _binding() -> ModelExecutionBinding:
    return ModelExecutionBinding(
        schema_version=1,
        provider_type="fixture",
        model_identity="fixture/model-v1",
        credential_profile_ref="fixture-profile",
        context_window_tokens=4_096,
        max_output_tokens=1_024,
        max_concurrency=2,
        supports_structured_output=True,
        supports_temperature=True,
        timeout_seconds=60,
    )


def _request(
    *,
    language_policy: FaithfulLanguagePolicy = FaithfulLanguagePolicy.PRESERVE_SOURCE,
    target_language: str | None = None,
    transcript: TranscriptDocument | None = None,
) -> FaithfulEditionRequestV1:
    transcript = transcript or _transcript()
    quality = assess_transcript_quality(
        TranscriptQualityInputV1(
            schema_version=1,
            transcript=transcript,
            transcript_basis=TranscriptBasis.PLATFORM_CAPTION,
            source_duration_ms=4_000,
            detected_languages=(transcript.language,),
        )
    )
    return FaithfulEditionRequestV1(
        schema_version=1,
        recipe_id="alltonote.video-faithful-edition",
        recipe_version=1,
        quality_profile=CompilationQualityProfile.BALANCED,
        transcript=transcript,
        transcript_quality=quality,
        transcript_basis=TranscriptBasis.PLATFORM_CAPTION,
        source_title="Fixture lesson",
        source_language=transcript.language,
        language_policy=language_policy,
        target_language=target_language,
        model_binding=_binding(),
        max_request_bytes=16_384,
        section_input_byte_budget=115,
        reserved_output_tokens=1_024,
        parser_limits=FaithfulEditionParserLimitsV1(
            max_response_bytes=16_384,
            max_title_characters=200,
            max_paragraphs=16,
            max_paragraph_characters=8_192,
            max_segment_refs_per_paragraph=64,
            max_key_points=16,
            max_uncertainties=16,
            max_auxiliary_text_characters=2_048,
            max_warnings=16,
        ),
        max_repair_attempts=1,
    )


def _compiler_context(
    tmp_path: Path,
    executor: _FaithfulExecutor,
) -> tuple[FaithfulEditionCompiler, VideoCompilationContext]:
    repository = SqliteJobRepository.open(tmp_path / "machine", clock=lambda: 1_000)
    job = repository.create_job(
        request_hash="sha256:" + "a" * 64,
        principal="local",
        client_request_id=None,
    )
    repository.transition_job(job.job_id, JobState.RUNNING)
    authority = repository.claim_job(
        job.job_id, "fixture", ttl_seconds=60
    ).authority
    attempt = repository.start_attempt(
        repository.create_attempt(
            job.job_id,
            "faithful-compile",
            authority=authority,
        ).attempt_id,
        authority,
    )
    execution = ModelCallExecution(
        job_id=job.job_id,
        step_id=attempt.step_id,
        attempt_id=attempt.attempt_id,
        authority=authority,
        heartbeat=lambda: repository.heartbeat_job_claim(
            authority, ttl_seconds=60
        ),
    )
    coordinator = ModelCallCoordinator(
        operation_store=repository,
        result_store=ModelOperationResultStore(tmp_path / "results"),
        executor=executor,
    )
    return (
        FaithfulEditionCompiler(coordinator),
        VideoCompilationContext(
            execution=execution,
            cancellation_token=CancellationToken(repository, job.job_id),
        ),
    )


def test_fast_section_review_overlaps_slow_section_generation(tmp_path):
    reviewed_first = threading.Event()

    class StreamingExecutor(_FaithfulExecutor):
        def complete(self, request, token):
            section = json.loads(request.user_content)["section"]
            if request.stage_id == "faithful-edit" and section["section_ordinal"] == 1:
                assert reviewed_first.wait(3), "review waited for all sections to finish"
            result = super().complete(request, token)
            if request.stage_id == "faithful-review" and section["ordinal"] == 0:
                reviewed_first.set()
            return result

    executor = StreamingExecutor()
    compiler, context = _compiler_context(tmp_path, executor)
    result = compiler.compile(_request(), context)
    assert reviewed_first.is_set()
    assert result.text_assessment.metrics.body_segment_reference_coverage_ratio == 1.0
    assert len([r for r in executor.requests if r.stage_id == "faithful-review"]) == len(result.plan.sections)


def test_streaming_preserves_serial_output_and_full_quality_gates(tmp_path):
    class SerialCompiler(FaithfulEditionCompiler):
        def _execute_wave(self, *args, **kwargs):
            kwargs.pop("review_plan", None)
            return super()._execute_wave(*args, **kwargs)

    stream, stream_context = _compiler_context(tmp_path / "stream", _FaithfulExecutor())
    serial_base, serial_context = _compiler_context(tmp_path / "serial", _FaithfulExecutor())
    streamed = stream.compile(_request(), stream_context)
    serial = SerialCompiler(serial_base._coordinator).compile(_request(), serial_context)
    assert streamed.markdown == serial.markdown
    assert streamed.text_assessment == serial.text_assessment
    assert streamed.execution_summary == serial.execution_summary
    assert streamed.usage == serial.usage


def test_numeric_failure_is_repaired_before_its_independent_review(tmp_path):
    executor = _FaithfulExecutor(omit_number_once=True)
    compiler, context = _compiler_context(tmp_path, executor)
    result = compiler.compile(_request(), context)
    reviews = [json.loads(r.user_content)["section"] for r in executor.requests
               if r.stage_id == "faithful-review"]
    first = [r for r in reviews if r["ordinal"] == 0]
    assert len(first) == 1
    assert "42" in first[0]["paragraphs"][0]["text"]
    assert result.execution_summary.repair_operation_count == 1
    assert result.text_assessment.metrics.number_mismatch_count == 0


def _assessment_for_source_and_target(
    source_text: str,
    target_text: str,
) -> FaithfulTextAssessmentV1:
    transcript = TranscriptDocument(
        "zh-CN",
        (TranscriptSegment("seg_000001", 0, 1_000, source_text),),
    )
    request = replace(_request(transcript=transcript), section_input_byte_budget=8192)
    plan = plan_faithful_edition(request)
    section_ref = plan.sections[0]
    section = FaithfulEditionSectionV1(
        section_id=section_ref.section_id,
        ordinal=section_ref.ordinal,
        title="Section",
        start_ms=section_ref.start_ms,
        end_ms=section_ref.end_ms,
        paragraphs=(
            FaithfulParagraphV1(
                paragraph_ordinal=0,
                text=target_text,
                source_segment_ids=("seg_000001",),
            ),
        ),
        summary=FaithfulAuxiliaryTextV1(
            text="Summary",
            source_segment_ids=("seg_000001",),
        ),
        key_points=(),
        uncertainties=(),
        warnings=(),
    )
    return assess_faithful_edition(
        FaithfulEditionCandidateV1(
            transcript=transcript,
            plan=plan,
            sections=(section,),
            markdown="# Fixture\n",
        )
    )


def test_plan_is_a_contiguous_non_overlapping_partition() -> None:
    request = _request()
    plan = plan_faithful_edition(request)

    assert len(plan.sections) > 1
    assert plan.sections[0].start_segment_ordinal == 0
    assert plan.sections[-1].end_segment_ordinal_exclusive == 4
    assert all(
        previous.end_segment_ordinal_exclusive == current.start_segment_ordinal
        for previous, current in zip(plan.sections, plan.sections[1:])
    )
    assert not plan.excluded_segment_ids


def test_plan_excludes_only_core_markers_and_preserves_spoken_repetition() -> None:
    transcript = TranscriptDocument(
        "en",
        (
            TranscriptSegment("seg_000001", 0, 1_000, "[Music]"),
            TranscriptSegment("seg_000002", 1_000, 2_000, "Keep this explanation."),
            TranscriptSegment("seg_000003", 2_000, 3_000, "Keep this explanation."),
            TranscriptSegment("seg_000004", 3_000, 4_000, "Keep this advertisement."),
        ),
    )
    plan = plan_faithful_edition(_request(transcript=transcript))

    assert plan.excluded_segment_ids == ("seg_000001",)
    assert sum(value.editable_segment_count for value in plan.sections) == 3


def test_contract_rejects_non_balanced_and_implicit_translation() -> None:
    with pytest.raises(DomainError, match="faithful_edition_contract_invalid"):
        replace(_request(), quality_profile=CompilationQualityProfile.FAST)

    with pytest.raises(DomainError, match="faithful_edition_contract_invalid"):
        _request(
            language_policy=FaithfulLanguagePolicy.TRANSLATE_TO_OUTPUT,
            target_language=None,
        )

    with pytest.raises(DomainError, match="faithful_edition_contract_invalid"):
        _request(target_language="zh-CN")


def test_parser_rejects_duplicate_or_unknown_paragraph_sources() -> None:
    request = _request()
    plan = plan_faithful_edition(request)
    section_ref = plan.sections[0]
    allowed = tuple(
        segment.segment_id
        for segment in request.transcript.segments[
            section_ref.start_segment_ordinal : section_ref.end_segment_ordinal_exclusive
        ]
    )
    payload = {
        "schema_version": 1,
        "section_id": section_ref.section_id,
        "section_ordinal": section_ref.ordinal,
        "title": "Section",
        "paragraphs": [
            {
                "paragraph_ordinal": 0,
                "text": "Body",
                "source_segment_ids": [allowed[0], allowed[0]],
            }
        ],
        "summary": {"text": "Summary", "source_segment_ids": [allowed[0]]},
        "key_points": [],
        "uncertainties": [],
        "warnings": [],
    }

    with pytest.raises(DomainError, match="faithful_section_response_invalid"):
        parse_faithful_section(
            json.dumps(payload),
            section_ref=section_ref,
            allowed_segment_ids=allowed,
            limits=request.parser_limits,
        )

    payload["paragraphs"][0]["source_segment_ids"] = ["seg_999999"]
    with pytest.raises(DomainError, match="faithful_section_response_invalid"):
        parse_faithful_section(
            json.dumps(payload),
            section_ref=section_ref,
            allowed_segment_ids=allowed,
            limits=request.parser_limits,
        )


def test_quality_reports_anchor_and_qualifier_risk_without_fidelity_score() -> None:
    request = _request()
    plan = plan_faithful_edition(request)
    section_ref = plan.sections[0]
    section = FaithfulEditionSectionV1(
        section_id=section_ref.section_id,
        ordinal=section_ref.ordinal,
        title="Section",
        start_ms=section_ref.start_ms,
        end_ms=section_ref.end_ms,
        paragraphs=(
            FaithfulParagraphV1(
                paragraph_ordinal=0,
                text="Version processes items.",
                source_segment_ids=("seg_000001",),
            ),
        ),
        summary=FaithfulAuxiliaryTextV1(
            text="Summary",
            source_segment_ids=("seg_000001",),
        ),
        key_points=(),
        uncertainties=(),
        warnings=(),
    )
    assessment = assess_faithful_edition(
        FaithfulEditionCandidateV1(
            transcript=request.transcript,
            plan=plan,
            sections=(section,),
            markdown="# Fixture\n",
        )
    )

    assert assessment.metrics.number_mismatch_count > 0
    assert assessment.metrics.technical_token_mismatch_count > 0
    assert assessment.metrics.qualifier_warning_count > 0
    assert any(check.check_id == "numeric_anchors" for check in assessment.checks)
    assert "score" not in vars(assessment)
    assert "fidelity" not in vars(assessment.metrics)


@pytest.mark.parametrize("technical_token", ("UE5", "API", "MCP", "LLM", "FFmpeg"))
def test_quality_recognizes_common_bare_technical_tokens(
    technical_token: str,
) -> None:
    assessment = _assessment_for_source_and_target(
        f"这个工具使用{technical_token}处理内容。",
        "这个工具使用模块处理内容。",
    )

    assert assessment.metrics.technical_token_mismatch_count == 1


@pytest.mark.parametrize("source,target", [
    ("现在是197.6768", "现在是197.68"),
    ("买入81份", "买入18份"),
    ("价格230到242", "价格2.30到2.42"),
    ("涨了百分之八", "涨了百分之十"),
    ("买入81，卖出18", "买入18，卖出81"),
])
def test_quality_detects_chinese_numeric_changes(source: str, target: str) -> None:
    assessment = _assessment_for_source_and_target(source, target)
    assert assessment.metrics.number_mismatch_count == 1


def test_numeric_anchors_allow_typographic_width_but_not_new_numeric_boundaries() -> None:
    assert _assessment_for_source_and_target("报价355,366", "报价355，366").metrics.number_mismatch_count == 0
    assert _assessment_for_source_and_target("81份", "８１份").metrics.number_mismatch_count == 0
    assert _assessment_for_source_and_target("报价355360", "报价355，360").metrics.number_mismatch_count == 1


@pytest.mark.parametrize(
    "qualifier",
    ("不", "不能", "可能", "也许", "必须", "仅", "除非", "但是", "不过"),
)
def test_quality_recognizes_chinese_qualifiers_without_word_boundaries(
    qualifier: str,
) -> None:
    assessment = _assessment_for_source_and_target(
        f"这个方案{qualifier}适用于当前条件。",
        "这个方案适用于当前条件。",
    )

    assert assessment.metrics.qualifier_warning_count == 1


def test_compiler_preserves_order_separates_regions_and_never_requests_screenshots(
    tmp_path: Path,
) -> None:
    executor = _FaithfulExecutor()
    compiler, context = _compiler_context(tmp_path, executor)
    result = compiler.compile(_request(), context)

    assert result.document_kind is VideoDocumentKind.FAITHFUL_EDITION
    assert result.model_identity == "fixture/model-v1"
    assert result.cited_segment_ids == tuple(
        segment.segment_id for segment in _transcript().segments
    )
    assert result.screenshot_requests == ()
    assert "## 精编正文" in result.markdown
    assert "## 全文概览（AI）" in result.markdown
    assert "**AI 章节摘要**" not in result.markdown
    assert "**AI 关键点**" in result.markdown
    assert "#### AI" not in result.markdown
    assert "### 待复核项" in result.markdown
    assert "fidelity score" not in result.markdown.casefold()
    assert result.text_assessment.metrics.order_violation_count == 0
    edit = next(call for call in executor.requests if call.stage_id == "faithful-edit")
    assert "start at 0" in edit.system_instruction
    schema = json.loads(edit.response_schema_json)
    assert schema["properties"]["paragraphs"]["items"]["properties"]["source_segment_ids"]["maxItems"] == 64


def test_translation_is_explicitly_labeled(tmp_path: Path) -> None:
    executor = _FaithfulExecutor()
    compiler, context = _compiler_context(tmp_path, executor)
    result = compiler.compile(
        _request(
            language_policy=FaithfulLanguagePolicy.TRANSLATE_TO_OUTPUT,
            target_language="zh-CN",
        ),
        context,
    )

    assert "翻译型高保真精编稿" in result.markdown
    assert "源语言：en" in result.markdown
    assert "目标语言：zh-CN" in result.markdown
    assert any(check.check_id == "translation_label" for check in result.text_assessment.checks)


def test_compiler_repairs_only_failed_section_once(tmp_path: Path) -> None:
    executor = _FaithfulExecutor(omit_number_once=True)
    compiler, context = _compiler_context(tmp_path, executor)
    result = compiler.compile(_request(), context)

    edit_payloads = [
        json.loads(value.user_content)
        for value in executor.requests
        if value.stage_id == "faithful-edit"
    ]
    repair_payloads = [
        json.loads(value.user_content)
        for value in executor.requests
        if value.stage_id == "faithful-repair"
    ]
    assert len(edit_payloads) == len(result.plan.sections)
    assert len(repair_payloads) == 1
    assert repair_payloads[0]["section"]["section_ordinal"] == 0
    assert result.execution_summary.repair_operation_count == 1
    assert result.text_assessment.overall.value == "pass"


@pytest.mark.parametrize(
    "invalid_contract_kind",
    ("missing", "duplicate", "out-of-order", "summary-unknown"),
)
def test_compiler_repairs_initial_section_response_contract_once(
    tmp_path: Path,
    invalid_contract_kind: str,
) -> None:
    executor = _FaithfulExecutor(
        invalid_contract_once=True,
        invalid_contract_kind=invalid_contract_kind,
    )
    compiler, context = _compiler_context(tmp_path, executor)
    result = compiler.compile(
        replace(_request(), section_input_byte_budget=8_192),
        context,
    )

    repair_payloads = [
        json.loads(value.user_content)
        for value in executor.requests
        if value.stage_id == "faithful-repair"
    ]
    assert len(repair_payloads) == 1
    assert repair_payloads[0]["failed_checks"] == ["response_contract"]
    repair_request = next(
        value for value in executor.requests if value.stage_id == "faithful-repair"
    )
    assert "cover every supplied body segment exactly once in order" in (
        repair_request.system_instruction
    )
    assert result.execution_summary.repair_operation_count == 1
    assert result.execution_summary.sequential_model_waves == 3
    assert result.text_assessment.overall.value == "pass"


def test_compiler_fails_explicitly_when_response_contract_repair_is_invalid(
    tmp_path: Path,
) -> None:
    executor = _FaithfulExecutor(invalid_contract_always=True)
    compiler, context = _compiler_context(tmp_path, executor)

    with pytest.raises(DomainError, match="faithful_section_repair_failed"):
        compiler.compile(_request(), context)

    repair_requests = [
        value for value in executor.requests if value.stage_id == "faithful-repair"
    ]
    assert len(repair_requests) == 1


def test_response_contract_repair_runs_full_gate_without_second_repair(
    tmp_path: Path,
) -> None:
    executor = _FaithfulExecutor(
        invalid_contract_once=True,
        omit_number_always=True,
    )
    compiler, context = _compiler_context(tmp_path, executor)
    result = compiler.compile(_request(), context)

    repair_requests = [
        value for value in executor.requests if value.stage_id == "faithful-repair"
    ]
    assert len(repair_requests) == 1
    assert result.execution_summary.repair_operation_count == 1
    assert result.text_assessment.overall.value == "fail"
    assert any(
        check.check_id == "numeric_anchors" and check.status.value == "fail"
        for check in result.text_assessment.checks
    )


def test_compiler_does_not_repair_response_contract_when_budget_is_zero(
    tmp_path: Path,
) -> None:
    executor = _FaithfulExecutor(invalid_contract_once=True)
    compiler, context = _compiler_context(tmp_path, executor)

    with pytest.raises(DomainError, match="faithful_section_response_invalid"):
        compiler.compile(replace(_request(), max_repair_attempts=0), context)

    # Other valid sections may already be reviewed by the streaming pipeline;
    # the failed section still cannot consume a repair when its budget is zero.
    assert all(value.stage_id in {"faithful-edit", "faithful-review"} for value in executor.requests)
    assert all(json.loads(value.user_content)["section"]["ordinal"] != 0
               for value in executor.requests if value.stage_id == "faithful-review")


@pytest.mark.parametrize(
    ("error_code", "category"),
    (
        ("model_auth_required", ErrorCategory.POLICY_DENIED),
        ("model_policy_denied", ErrorCategory.POLICY_DENIED),
        ("job_cancelled", ErrorCategory.CANCELLED),
        ("external_outcome_unknown", ErrorCategory.CONFLICT),
    ),
)
def test_compiler_propagates_non_response_errors_without_repair(
    tmp_path: Path,
    error_code: str,
    category: ErrorCategory,
) -> None:
    executor = _FaithfulExecutor(
        provider_error=DomainError(error_code, category, "fixture provider failure")
    )
    compiler, context = _compiler_context(tmp_path, executor)

    with pytest.raises(DomainError, match=error_code):
        compiler.compile(
            replace(_request(), section_input_byte_budget=8_192),
            context,
        )

    assert len(executor.requests) == 1
    assert executor.requests[0].stage_id == "faithful-edit"


def test_compiler_never_performs_a_second_repair_wave(tmp_path: Path) -> None:
    executor = _FaithfulExecutor(omit_number_always=True)
    compiler, context = _compiler_context(tmp_path, executor)
    result = compiler.compile(_request(), context)

    repair_requests = [
        value for value in executor.requests if value.stage_id == "faithful-repair"
    ]
    assert len(repair_requests) == 1
    assert result.execution_summary.sequential_model_waves == 2
    assert result.text_assessment.overall.value == "fail"


def test_all_section_prompts_are_preflighted_before_paid_calls(tmp_path: Path) -> None:
    executor = _FaithfulExecutor()
    compiler, context = _compiler_context(tmp_path, executor)

    with pytest.raises(DomainError, match="model_request_budget_exceeded"):
        compiler.compile(replace(_request(), source_title="x" * 20_000), context)

    assert executor.requests == []


def _visual_request() -> FaithfulEditionRequestV1:
    return replace(
        _request(), section_input_byte_budget=8192,
        model_binding=replace(_binding(), provider_type="codex-app-server"),
        visual_frames=(VisualFrame("seg_000001", 0, b"RIFF\x04\x00\x00\x00WEBP"),),
    )


def test_faithful_review_receives_images_binds_local_paragraph_and_recovers(tmp_path: Path) -> None:
    executor = _FaithfulExecutor()
    compiler, context = _compiler_context(tmp_path, executor)
    request = _visual_request()
    result = compiler.compile(request, context)
    assert [call.stage_id for call in executor.requests] == ["faithful-source-prepare", "faithful-source-check", "faithful-edit", "faithful-review"]
    assert all(call.image_webp == (request.visual_frames[0].payload,) for call in executor.requests)
    assert "corrected spelling need NOT already appear verbatim" in executor.requests[3].system_instruction
    assert "'always' becoming 'usually'" in executor.requests[3].system_instruction
    assert "'always' must not become 'usually'" in executor.requests[2].system_instruction
    assert [value.segment_id for value in result.screenshot_requests] == ["seg_000001"]
    assert result.markdown.index("42 items") < result.markdown.index("[SCREENSHOT:")
    assert result.markdown.index("[SCREENSHOT:") < result.markdown.index("## AI 辅助摘要")
    assert result.execution_summary.model_operation_count == 4
    assert result.execution_summary.sequential_model_waves == 4
    assert any(check.method.value == "model" for check in result.text_assessment.checks)
    recovered = compiler.compile(request, context)
    assert recovered.markdown == result.markdown
    assert len(executor.requests) == 4


def test_faithful_images_reject_unsupported_provider_before_edit(tmp_path: Path) -> None:
    executor = _FaithfulExecutor()
    compiler, context = _compiler_context(tmp_path, executor)
    with pytest.raises(DomainError, match="model_capability_missing"):
        compiler.compile(replace(_visual_request(), model_binding=_binding()), context)
    assert not executor.requests


@pytest.mark.parametrize("mode,error", [
    ("fail", "faithful_review_failed"), ("unknown", "faithful_review_invalid"),
    ("duplicate", "faithful_review_invalid"), ("truthy", "faithful_review_invalid"),
])
def test_faithful_review_failures_remain_blocked_after_bounded_repair(tmp_path: Path, mode: str, error: str) -> None:
    class Executor(_FaithfulExecutor):
        def complete(self, request, token):
            result = super().complete(request, token)
            if request.stage_id == "faithful-review":
                payload = json.loads(result.text)
                if mode == "fail":
                    payload.update({"pass": False, "issues": ["seg_000001: omitted a condition"]})
                elif mode == "unknown":
                    payload["frames"][0]["segment_id"] = "seg_999999"
                elif mode == "duplicate":
                    payload["frames"].append(payload["frames"][0])
                else:
                    payload["pass"] = "true"
                return replace(result, text=json.dumps(payload))
            return result
    executor = Executor()
    compiler, context = _compiler_context(tmp_path, executor)
    with pytest.raises(DomainError, match=error):
        compiler.compile(_visual_request(), context)
    assert len(executor.requests) == (6 if mode == "fail" else 4)


def test_faithful_review_can_skip_frames_and_mark_local_ambiguity(tmp_path: Path) -> None:
    class Executor(_FaithfulExecutor):
        def complete(self, request, token):
            result = super().complete(request, token)
            if request.stage_id == "faithful-review":
                payload = json.loads(result.text)
                payload["frames"][0]["use"] = False
                payload["uncertainties"] = [{"paragraph_ordinal": 0, "description": "数字需要核对音频。"}]
                return replace(result, text=json.dumps(payload))
            return result
    compiler, context = _compiler_context(tmp_path, Executor())
    result = compiler.compile(_visual_request(), context)
    assert result.screenshot_requests == ()
    assert "[SCREENSHOT:" not in result.markdown
    assert result.markdown.index("待核对：数字") < result.markdown.index("## AI 辅助摘要")


@pytest.mark.parametrize("contract_repair_first", [False, True])
@pytest.mark.parametrize("body_failure", [False, True])
def test_review_feedback_repair_is_visual_bounded_rechecked_and_recoverable(
    tmp_path: Path, body_failure: bool, contract_repair_first: bool,
) -> None:
    class Executor(_FaithfulExecutor):
        reviews = 0

        def complete(self, request, token):
            result = super().complete(request, token)
            if request.stage_id == "faithful-review":
                self.reviews += 1
                payload = json.loads(result.text)
                if self.reviews == 1:
                    payload.update({"pass": not body_failure,
                                    "issues": ["seg_000001: restore original unclear referent"] if body_failure else [],
                                    "auxiliary_pass": False,
                                    "auxiliary_issues": ["seg_000001: restore the speaker's condition"]})
                    payload["frames"][0]["caption"] = "Stale first-review caption"
                return replace(result, text=json.dumps(payload))
            if request.stage_id == "faithful-semantic-repair":
                payload = json.loads(request.user_content)
                assert payload["previous_section"]["paragraphs"]
                assert payload["review_feedback"]["auxiliary_issues"]
                assert bool(payload["review_feedback"]["body_issues"]) == body_failure
                assert payload["section"]["segments"][0]["segment_id"] == payload["frames"][0]["segment_id"]
                assert request.image_webp == (_visual_request().visual_frames[0].payload,)
                assert "untrusted proposals, not authority" in request.system_instruction
            return result

    executor = Executor(invalid_contract_once=contract_repair_first)
    compiler, context = _compiler_context(tmp_path, executor)
    result = compiler.compile(_visual_request(), context)
    expected = ["faithful-source-prepare", "faithful-source-check", "faithful-edit"] + (["faithful-repair"] if contract_repair_first else [])
    expected += ["faithful-review", "faithful-semantic-repair", "faithful-review"]
    assert [call.stage_id for call in executor.requests] == expected
    assert result.execution_summary.sequential_model_waves == len(expected)
    assert result.execution_summary.repair_operation_count == 1 + contract_repair_first
    assert result.execution_summary.model_operation_count == len(expected)
    assert "Stale first-review caption" not in result.markdown
    assert "Section summary" in result.markdown
    assert compiler.compile(_visual_request(), context).markdown == result.markdown
    assert len(executor.requests) == len(expected)


def test_semantic_repair_cannot_bypass_numeric_checks(tmp_path: Path) -> None:
    class Executor(_FaithfulExecutor):
        def complete(self, request, token):
            result = super().complete(request, token)
            payload = json.loads(result.text)
            if request.stage_id == "faithful-review":
                payload.update({"pass": False, "issues": ["seg_000001: review term"]})
            elif request.stage_id == "faithful-semantic-repair":
                payload["paragraphs"][0]["text"] = payload["paragraphs"][0]["text"].replace("42", "43")
            return replace(result, text=json.dumps(payload))
    executor = Executor()
    compiler, context = _compiler_context(tmp_path, executor)
    with pytest.raises(DomainError, match="faithful_semantic_repair_failed"):
        compiler.compile(_visual_request(), context)
    assert [call.stage_id for call in executor.requests] == ["faithful-source-prepare", "faithful-source-check", "faithful-edit", "faithful-review", "faithful-semantic-repair"]


def test_semantic_repair_only_rechecks_the_failed_section(tmp_path: Path) -> None:
    class Executor(_FaithfulExecutor):
        reviewed: dict[int, int] = {}

        def complete(self, request, token):
            result = super().complete(request, token)
            if request.stage_id == "faithful-review":
                section = json.loads(request.user_content)["section"]
                ordinal = section["ordinal"]
                self.reviewed[ordinal] = self.reviewed.get(ordinal, 0) + 1
                if ordinal == 1 and self.reviewed[ordinal] == 1:
                    payload = json.loads(result.text)
                    payload.update({"pass": False, "issues": ["Restore the original wording"]})
                    return replace(result, text=json.dumps(payload))
            return result
    executor = Executor()
    compiler, context = _compiler_context(tmp_path, executor)
    result = compiler.compile(_request(), context)
    assert result.execution_summary.section_count > 1
    repairs = [call for call in executor.requests if call.stage_id == "faithful-semantic-repair"]
    assert len(repairs) == 1
    assert json.loads(repairs[0].user_content)["section"]["section_ordinal"] == 1
    assert executor.reviewed[1] == 2
    assert all(count == 1 for ordinal, count in executor.reviewed.items() if ordinal != 1)


def test_failed_optional_summary_is_omitted_without_discarding_reviewed_body(tmp_path: Path) -> None:
    class Executor(_FaithfulExecutor):
        def complete(self, request, token):
            result = super().complete(request, token)
            if request.stage_id == "faithful-review":
                payload = json.loads(result.text)
                payload.update(auxiliary_pass=False, auxiliary_issues=["summary omitted a condition"])
                return replace(result, text=json.dumps(payload))
            return result
    compiler, context = _compiler_context(tmp_path, Executor())
    result = compiler.compile(_visual_request(), context)
    assert "42 items" in result.markdown
    assert "[SCREENSHOT:seg_000001]" in result.markdown
    assert "Section summary" not in result.markdown
    assert "Key point" not in result.markdown
    assert "已省略" in result.markdown
    assert result.text_assessment.overall.value == "pass_with_warnings"
    assert "faithful_auxiliary_omitted_after_review" in result.warnings
    assert any(check.check_id == "faithful_semantic_review" and check.status.value == "pass"
               for check in result.text_assessment.checks)


def test_faithful_edit_log_is_exact_and_hidden_only_in_reading(tmp_path: Path) -> None:
    class Executor(_FaithfulExecutor):
        def complete(self, request, token):
            result = super().complete(request, token)
            if request.stage_id == "faithful-edit":
                payload = json.loads(result.text)
                payload["paragraphs"][0]["text"] += " "
                return replace(result, text=json.dumps(payload))
            return result
    compiler, context = _compiler_context(tmp_path, Executor())
    result = compiler.compile(replace(_request(), section_input_byte_budget=8192), context)
    payload = json.loads(result.markdown.split("```alltonote-edit-log-v1\n")[1].split("\n```")[0])
    assert len(payload["records"]) == 1
    record = payload["records"][0]
    assert record["before"] == " ".join(segment.text for segment in _transcript().segments)
    assert record["after"] == record["before"] + " "
    assert record["source_segment_ids"] == list(result.cited_segment_ids)
    reading = project_reading_markdown(result.markdown)
    assert "alltonote-edit-log" not in reading
    assert "42 items" in reading
    assert "<summary>AI 摘要与关键点（不属于原文）</summary>" in reading
    assert project_reading_markdown(reading) == reading


@pytest.mark.parametrize("source,target", [
    ("冷却10分钟。再说一次，冷却10分钟。", "冷却10分钟。"),
    ("使用API-v2保存。使用API-v2保存即可。", "使用API-v2保存即可。"),
    ("版本2只支持API，不支持MCP。再说一次，版本2只支持API，不支持MCP。",
     "版本2只支持API，不支持MCP。"),
])
def test_redundant_anchor_occurrences_can_be_removed(source, target):
    assessment = _assessment_for_source_and_target(source, target)
    assert assessment.metrics.number_mismatch_count == 0
    assert assessment.metrics.technical_token_mismatch_count == 0


@pytest.mark.parametrize("source,target", [
    ("等待10分钟，再等待20分钟。", "等待20分钟，再等待10分钟。"),
    ("先10分钟，再20分钟。", "先10分钟，再10分钟，再20分钟。"),
    ("先10分钟，再20分钟。", "等待10分钟。"),
    ("冷却10分钟。冷却10分钟。", "冷却15分钟。"),
])
def test_deduplication_still_rejects_numeric_changes(source, target):
    assert _assessment_for_source_and_target(source, target).metrics.number_mismatch_count == 1


@pytest.mark.parametrize("source,target", [
    ("使用API和MCP。", "使用API。"),
    ("使用API。", "使用API和API。"),
    ("使用API和API。", "使用MCP。"),
])
def test_deduplication_still_rejects_technical_token_changes(source, target):
    assert _assessment_for_source_and_target(source, target).metrics.technical_token_mismatch_count == 1


@pytest.mark.parametrize("overview,points", [(False, False), (True, False), (False, True), (True, True)])
def test_optional_overview_and_chapter_points_render_independently(tmp_path, overview, points):
    class Executor(_FaithfulExecutor):
        def complete(self, request, token):
            result = super().complete(request, token)
            if request.stage_id == "faithful-edit":
                payload = json.loads(result.text)
                if not overview:
                    payload["summary"] = {"text": "", "source_segment_ids": []}
                if not points:
                    payload["key_points"] = []
                return replace(result, text=json.dumps(payload))
            return result

    compiler, context = _compiler_context(tmp_path, Executor())
    result = compiler.compile(replace(_request(), section_input_byte_budget=8192), context)
    reading = project_reading_markdown(result.markdown)
    assert ("## 全文概览（AI）" in reading) == overview
    assert ("<details open>" in reading) == points
    assert reading.count("Section summary") == int(overview)
    assert "AI 章节摘要" not in reading
    assert "- 无" not in reading
    assert "42 items" in reading
    assert project_reading_markdown(reading) == reading


@pytest.mark.parametrize("summary", [
    {"text": "", "source_segment_ids": ["seg_000001"]},
    {"text": "Conclusion", "source_segment_ids": []},
    {"text": " ", "source_segment_ids": []},
    {"text": "Conclusion", "source_segment_ids": ["seg_999999"]},
])
def test_optional_summary_does_not_allow_unbound_text(tmp_path, summary):
    class Executor(_FaithfulExecutor):
        def complete(self, request, token):
            result = super().complete(request, token)
            if request.stage_id == "faithful-edit":
                payload = json.loads(result.text)
                payload["summary"] = summary
                return replace(result, text=json.dumps(payload))
            return result

    compiler, context = _compiler_context(tmp_path, Executor())
    with pytest.raises(DomainError, match="faithful_section_response_invalid"):
        compiler.compile(replace(_request(), max_repair_attempts=0), context)


def test_deduplication_keeps_all_source_ids_and_exact_audit(tmp_path):
    transcript = TranscriptDocument("zh-CN", (
        TranscriptSegment("seg_000001", 0, 1000, "冷却10分钟。"),
        TranscriptSegment("seg_000002", 1000, 2000, "再说一次，冷却10分钟。"),
    ))

    class Executor(_FaithfulExecutor):
        def complete(self, request, token):
            result = super().complete(request, token)
            if request.stage_id == "faithful-edit":
                payload = json.loads(result.text)
                payload["paragraphs"][0]["text"] = "冷却10分钟。"
                payload["summary"] = {"text": "", "source_segment_ids": []}
                payload["key_points"] = []
                return replace(result, text=json.dumps(payload))
            return result

    executor = Executor()
    compiler, context = _compiler_context(tmp_path, executor)
    result = compiler.compile(replace(_request(transcript=transcript), section_input_byte_budget=8192), context)
    assert result.cited_segment_ids == ("seg_000001", "seg_000002")
    audit = json.loads(result.markdown.split("```alltonote-edit-log-v1\n")[1].split("\n```")[0])
    assert audit["records"][0]["before"] == "冷却10分钟。 再说一次，冷却10分钟。"
    assert audit["records"][0]["after"] == "冷却10分钟。"
    assert project_reading_markdown(result.markdown).count("冷却10分钟。") == 1
    review = next(call for call in executor.requests if call.stage_id == "faithful-review")
    assert len(json.loads(review.user_content)["transcript"]) == 2


def test_anchor_preservation_cannot_bypass_review_of_distinct_steps(tmp_path):
    transcript = TranscriptDocument("zh-CN", (
        TranscriptSegment("seg_000001", 0, 1000, "加热10分钟，再冷却10分钟。"),
    ))

    class Executor(_FaithfulExecutor):
        def complete(self, request, token):
            result = super().complete(request, token)
            payload = json.loads(result.text)
            if request.stage_id in {"faithful-edit", "faithful-semantic-repair"}:
                payload["paragraphs"][0]["text"] = "加热10分钟。"
            elif request.stage_id == "faithful-review":
                payload.update({"pass": False, "issues": ["seg_000001: cooling step omitted"]})
            return replace(result, text=json.dumps(payload))

    executor = Executor()
    compiler, context = _compiler_context(tmp_path, executor)
    with pytest.raises(DomainError, match="faithful_review_failed"):
        compiler.compile(replace(_request(transcript=transcript), section_input_byte_budget=8192), context)
    assert len([call for call in executor.requests if call.stage_id == "faithful-review"]) == 2

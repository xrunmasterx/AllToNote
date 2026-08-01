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


def _assessment_for_source_and_target(
    source_text: str,
    target_text: str,
) -> FaithfulTextAssessmentV1:
    transcript = TranscriptDocument(
        "zh-CN",
        (TranscriptSegment("seg_000001", 0, 1_000, source_text),),
    )
    request = _request(transcript=transcript)
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


def test_plan_excludes_only_core_markers_and_confirmed_adjacent_duplicates() -> None:
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

    assert plan.excluded_segment_ids == ("seg_000001", "seg_000003")
    assert sum(value.editable_segment_count for value in plan.sections) == 2


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
    assert "### AI 章节摘要" in result.markdown
    assert "### AI 关键点" in result.markdown
    assert "### 待复核项" in result.markdown
    assert "fidelity score" not in result.markdown.casefold()
    assert result.text_assessment.metrics.order_violation_count == 0


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
    assert result.execution_summary.sequential_model_waves == 2
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

    assert all(value.stage_id == "faithful-edit" for value in executor.requests)


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

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from app.core.application.model_call_coordinator import ModelCallCoordinator
from app.core.application.video_compiler import VideoCompilationContext
from app.core.domain.video import (
    FaithfulLanguagePolicy,
    ScreenshotRequest,
    TranscriptSegment,
    VideoDocumentKind,
)
from app.core.errors import DomainError, ErrorCategory
from app.core.ports.model_executor import (
    ModelExecutionRequest,
    ModelExecutionResult,
    ModelOutputMode,
)
from app.core.recipes.video.faithful_edition.contracts import (
    FaithfulEditionPlanV1,
    FaithfulEditionRequestV1,
    FaithfulEditionSectionV1,
    FaithfulSectionRefV1,
)
from app.core.recipes.video.faithful_edition.pipeline import (
    parse_faithful_section,
    plan_faithful_edition,
)
from app.core.recipes.video.faithful_edition.prompts import (
    FaithfulEditionPrompt,
    build_faithful_section_prompt,
)
from app.core.recipes.video.faithful_edition.quality import (
    FaithfulEditionCandidateV1,
    FaithfulTextAssessmentV1,
    QualityCheckStatus,
    assess_faithful_edition,
)


@dataclass(frozen=True)
class FaithfulCompilationUsageV1:
    input_tokens: int
    output_tokens: int
    token_counts_complete: bool

    def __post_init__(self) -> None:
        if (
            type(self.input_tokens) is not int
            or self.input_tokens < 0
            or type(self.output_tokens) is not int
            or self.output_tokens < 0
            or type(self.token_counts_complete) is not bool
        ):
            raise DomainError(
                "faithful_edition_contract_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Faithful compilation usage is invalid",
            )


@dataclass(frozen=True)
class FaithfulCompilationSummaryV1:
    section_count: int
    model_operation_count: int
    sequential_model_waves: int
    repair_operation_count: int
    uncertainty_count: int
    anchor_warning_count: int
    body_segment_reference_coverage_ratio: float

    def __post_init__(self) -> None:
        if (
            type(self.section_count) is not int
            or self.section_count < 1
            or type(self.model_operation_count) is not int
            or self.model_operation_count < self.section_count
            or type(self.sequential_model_waves) is not int
            or not 1 <= self.sequential_model_waves <= 2
            or type(self.repair_operation_count) is not int
            or not 0 <= self.repair_operation_count <= self.section_count
            or type(self.uncertainty_count) is not int
            or self.uncertainty_count < 0
            or type(self.anchor_warning_count) is not int
            or self.anchor_warning_count < 0
            or type(self.body_segment_reference_coverage_ratio) is not float
            or not 0.0 <= self.body_segment_reference_coverage_ratio <= 1.0
        ):
            raise DomainError(
                "faithful_edition_contract_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Faithful compilation summary is invalid",
            )


@dataclass(frozen=True)
class FaithfulCompiledVideoDocument:
    document_kind: VideoDocumentKind
    model_identity: str
    markdown: str
    cited_segment_ids: tuple[str, ...]
    screenshot_requests: tuple[ScreenshotRequest, ...]
    plan: FaithfulEditionPlanV1
    text_assessment: FaithfulTextAssessmentV1
    execution_summary: FaithfulCompilationSummaryV1
    usage: FaithfulCompilationUsageV1
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        try:
            citations = tuple(self.cited_segment_ids)
            screenshots = tuple(self.screenshot_requests)
            warnings = tuple(self.warnings)
        except TypeError:
            raise DomainError(
                "faithful_edition_contract_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Compiled faithful edition is invalid",
            ) from None
        if (
            self.document_kind is not VideoDocumentKind.FAITHFUL_EDITION
            or type(self.model_identity) is not str
            or not self.model_identity.strip()
            or not self.markdown.strip()
            or not citations
            or len(citations) != len(set(citations))
            or screenshots
            or not isinstance(self.plan, FaithfulEditionPlanV1)
            or not isinstance(self.text_assessment, FaithfulTextAssessmentV1)
            or not isinstance(self.execution_summary, FaithfulCompilationSummaryV1)
            or not isinstance(self.usage, FaithfulCompilationUsageV1)
            or any(type(value) is not str or not value.strip() for value in warnings)
            or len(warnings) != len(set(warnings))
        ):
            raise DomainError(
                "faithful_edition_contract_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Compiled faithful edition is invalid",
            )
        object.__setattr__(self, "cited_segment_ids", citations)
        object.__setattr__(self, "screenshot_requests", screenshots)
        object.__setattr__(self, "warnings", warnings)


@dataclass(frozen=True)
class _FaithfulSectionExecution:
    section_ref: FaithfulSectionRefV1
    result: ModelExecutionResult
    section: FaithfulEditionSectionV1 | None
    response_error: DomainError | None


class FaithfulEditionCompiler:
    """Balanced faithful editing without knowledge-note reorganization."""

    def __init__(self, coordinator: ModelCallCoordinator) -> None:
        if not isinstance(coordinator, ModelCallCoordinator):
            raise DomainError(
                "faithful_edition_contract_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Faithful compiler requires ModelCallCoordinator",
            )
        self._coordinator = coordinator

    def compile(
        self,
        request: FaithfulEditionRequestV1,
        context: VideoCompilationContext,
    ) -> FaithfulCompiledVideoDocument:
        if not isinstance(request, FaithfulEditionRequestV1) or not isinstance(
            context, VideoCompilationContext
        ):
            raise DomainError(
                "faithful_edition_contract_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Faithful compiler requires the frozen Core contracts",
            )
        if not request.model_binding.supports_structured_output:
            raise DomainError(
                "model_capability_missing",
                ErrorCategory.POLICY_DENIED,
                "Balanced faithful editing requires structured model output",
            )
        plan = plan_faithful_edition(request)
        section_inputs = tuple(
            (
                section_ref,
                self._section_segments(request, plan, section_ref),
            )
            for section_ref in plan.sections
        )
        prompts = tuple(
            build_faithful_section_prompt(
                section_ref=section_ref,
                segments=segments,
                source_title=request.source_title,
                source_language=request.source_language,
                language_policy=request.language_policy,
                target_language=request.target_language,
            )
            for section_ref, segments in section_inputs
        )
        self._preflight_prompts(request, prompts)
        outcomes = self._execute_wave(
            request,
            context,
            section_inputs,
            prompts,
            stage_id="faithful-edit",
            shard_prefix="section",
            capture_response_errors=True,
        )
        results = [value.result for value in outcomes]
        sections_by_ordinal = {
            value.section_ref.ordinal: value.section
            for value in outcomes
            if value.section is not None
        }
        invalid_outcomes = tuple(
            value for value in outcomes if value.response_error is not None
        )
        repair_count = 0
        sequential_waves = 1
        response_contract_repaired = False
        if invalid_outcomes:
            if plan.max_repair_attempts != 1:
                error = invalid_outcomes[0].response_error
                assert error is not None
                raise error
            repaired_sections, repair_results = self._repair_sections(
                request,
                context,
                plan,
                {
                    value.section_ref.ordinal: ("response_contract",)
                    for value in invalid_outcomes
                },
            )
            for section in repaired_sections:
                sections_by_ordinal[section.ordinal] = section
            results.extend(repair_results)
            repair_count = len(repaired_sections)
            sequential_waves = 2
            response_contract_repaired = True
        sections: list[FaithfulEditionSectionV1] = []
        for section_ref in plan.sections:
            section = sections_by_ordinal.get(section_ref.ordinal)
            assert section is not None
            sections.append(section)
        markdown = self._assemble_markdown(request, sections)
        assessment = assess_faithful_edition(
            FaithfulEditionCandidateV1(
                transcript=request.transcript,
                plan=plan,
                sections=tuple(sections),
                markdown=markdown,
            )
        )
        if (
            not response_contract_repaired
            and assessment.repairable
            and plan.max_repair_attempts == 1
            and assessment.failed_section_ordinals
        ):
            repair_count, repair_results = self._repair_failed_sections(
                request, context, plan, sections, assessment
            )
            results.extend(repair_results)
            sequential_waves += 1
            markdown = self._assemble_markdown(request, sections)
            assessment = assess_faithful_edition(
                FaithfulEditionCandidateV1(
                    transcript=request.transcript,
                    plan=plan,
                    sections=tuple(sections),
                    markdown=markdown,
                )
            )

        citations = tuple(
            source_id
            for section in sections
            for paragraph in section.paragraphs
            for source_id in paragraph.source_segment_ids
        )
        warning_values = [
            warning
            for result in results
            for warning in result.warnings
        ]
        warning_values.extend(
            warning
            for section in sections
            for warning in section.warnings
        )
        warnings = tuple(dict.fromkeys(warning_values))
        input_complete = all(value.input_tokens is not None for value in results)
        output_complete = all(value.output_tokens is not None for value in results)
        model_identities = {value.actual_model_identity for value in results}
        if model_identities != {request.model_binding.model_identity}:
            raise DomainError(
                "model_identity_mismatch",
                ErrorCategory.RECIPE_FAILED,
                "Faithful compilation results do not match the frozen model binding",
            )
        metrics = assessment.metrics
        return FaithfulCompiledVideoDocument(
            document_kind=VideoDocumentKind.FAITHFUL_EDITION,
            model_identity=request.model_binding.model_identity,
            markdown=markdown,
            cited_segment_ids=citations,
            screenshot_requests=(),
            plan=plan,
            text_assessment=assessment,
            execution_summary=FaithfulCompilationSummaryV1(
                section_count=len(sections),
                model_operation_count=len(results),
                sequential_model_waves=sequential_waves,
                repair_operation_count=repair_count,
                uncertainty_count=metrics.uncertainty_count,
                anchor_warning_count=metrics.anchor_warning_count,
                body_segment_reference_coverage_ratio=(
                    metrics.body_segment_reference_coverage_ratio
                ),
            ),
            usage=FaithfulCompilationUsageV1(
                input_tokens=sum(value.input_tokens or 0 for value in results),
                output_tokens=sum(value.output_tokens or 0 for value in results),
                token_counts_complete=input_complete and output_complete,
            ),
            warnings=warnings,
        )

    @staticmethod
    def _section_segments(
        request: FaithfulEditionRequestV1,
        plan: FaithfulEditionPlanV1,
        section_ref: FaithfulSectionRefV1,
    ) -> tuple[TranscriptSegment, ...]:
        excluded = frozenset(plan.excluded_segment_ids)
        return tuple(
            value
            for value in request.transcript.segments[
                section_ref.start_segment_ordinal : section_ref.end_segment_ordinal_exclusive
            ]
            if value.segment_id not in excluded
        )

    @staticmethod
    def _preflight_prompts(
        request: FaithfulEditionRequestV1,
        prompts: tuple[FaithfulEditionPrompt, ...],
    ) -> None:
        if any(
            len(prompt.system_instruction.encode("utf-8"))
            + len(prompt.user_content.encode("utf-8"))
            + len(prompt.response_schema_json.encode("utf-8"))
            > request.max_request_bytes
            for prompt in prompts
        ):
            raise DomainError(
                "model_request_budget_exceeded",
                ErrorCategory.POLICY_DENIED,
                "A faithful section request exceeds the frozen byte budget",
            )

    def _execute_wave(
        self,
        request: FaithfulEditionRequestV1,
        context: VideoCompilationContext,
        section_inputs: tuple[
            tuple[FaithfulSectionRefV1, tuple[TranscriptSegment, ...]], ...
        ],
        prompts: tuple[FaithfulEditionPrompt, ...],
        *,
        stage_id: str,
        shard_prefix: str,
        capture_response_errors: bool,
    ) -> tuple[_FaithfulSectionExecution, ...]:
        def execute_one(
            value: tuple[
                tuple[FaithfulSectionRefV1, tuple[TranscriptSegment, ...]],
                FaithfulEditionPrompt,
            ],
        ) -> _FaithfulSectionExecution:
            (section_ref, segments), prompt = value
            model_request = ModelExecutionRequest(
                schema_version=1,
                stage_id=stage_id,
                stage_version=1,
                prompt_id=f"{stage_id}-balanced",
                prompt_version=1,
                system_instruction=prompt.system_instruction,
                user_content=prompt.user_content,
                output_mode=ModelOutputMode.JSON_SCHEMA,
                response_schema_json=prompt.response_schema_json,
                temperature=0 if request.model_binding.supports_temperature else None,
                max_output_tokens=request.reserved_output_tokens,
                timeout_seconds=request.model_binding.timeout_seconds,
            )
            result = self._coordinator.execute(
                request.model_binding,
                model_request,
                context.execution,
                f"{shard_prefix}-{section_ref.ordinal:04d}",
                context.cancellation_token,
            )
            try:
                section = parse_faithful_section(
                    result.text,
                    section_ref=section_ref,
                    allowed_segment_ids=tuple(
                        segment.segment_id for segment in segments
                    ),
                    limits=request.parser_limits,
                )
            except DomainError as error:
                if (
                    not capture_response_errors
                    or error.code != "faithful_section_response_invalid"
                ):
                    raise
                return _FaithfulSectionExecution(
                    section_ref=section_ref,
                    result=result,
                    section=None,
                    response_error=error,
                )
            return _FaithfulSectionExecution(
                section_ref=section_ref,
                result=result,
                section=section,
                response_error=None,
            )

        values = tuple(zip(section_inputs, prompts))
        if len(values) == 1:
            return (execute_one(values[0]),)
        with ThreadPoolExecutor(
            max_workers=min(len(values), request.model_binding.max_concurrency)
        ) as pool:
            return tuple(pool.map(execute_one, values))

    def _repair_failed_sections(
        self,
        request: FaithfulEditionRequestV1,
        context: VideoCompilationContext,
        plan: FaithfulEditionPlanV1,
        sections: list[FaithfulEditionSectionV1],
        assessment: FaithfulTextAssessmentV1,
    ) -> tuple[int, list[ModelExecutionResult]]:
        failed = frozenset(assessment.failed_section_ordinals)
        repaired_sections, results = self._repair_sections(
            request,
            context,
            plan,
            {
                section_ref.ordinal: tuple(
                    check.check_id
                    for check in assessment.checks
                    if check.status is QualityCheckStatus.FAIL
                    and section_ref.ordinal in check.failed_section_ordinals
                )
                for section_ref in plan.sections
                if section_ref.ordinal in failed
            },
        )
        for section in repaired_sections:
            sections[section.ordinal] = section
        return len(repaired_sections), results

    def _repair_sections(
        self,
        request: FaithfulEditionRequestV1,
        context: VideoCompilationContext,
        plan: FaithfulEditionPlanV1,
        failed_checks_by_ordinal: dict[int, tuple[str, ...]],
    ) -> tuple[list[FaithfulEditionSectionV1], list[ModelExecutionResult]]:
        inputs = tuple(
            (
                section_ref,
                self._section_segments(request, plan, section_ref),
            )
            for section_ref in plan.sections
            if section_ref.ordinal in failed_checks_by_ordinal
        )
        prompts = tuple(
            build_faithful_section_prompt(
                section_ref=section_ref,
                segments=segments,
                source_title=request.source_title,
                source_language=request.source_language,
                language_policy=request.language_policy,
                target_language=request.target_language,
                failed_checks=failed_checks_by_ordinal[section_ref.ordinal],
            )
            for section_ref, segments in inputs
        )
        self._preflight_prompts(request, prompts)
        try:
            outcomes = self._execute_wave(
                request,
                context,
                inputs,
                prompts,
                stage_id="faithful-repair",
                shard_prefix="repair-section",
                capture_response_errors=False,
            )
        except DomainError as error:
            if error.code != "faithful_section_response_invalid":
                raise
            raise DomainError(
                "faithful_section_repair_failed",
                ErrorCategory.RECIPE_FAILED,
                "A faithful section still violated the strict response contract after repair",
            ) from None
        return (
            [
                outcome.section
                for outcome in outcomes
                if outcome.section is not None
            ],
            [outcome.result for outcome in outcomes],
        )

    @staticmethod
    def _assemble_markdown(
        request: FaithfulEditionRequestV1,
        sections: list[FaithfulEditionSectionV1],
    ) -> str:
        lines = [f"# {request.source_title} — 高保真精编稿", ""]
        if request.language_policy is FaithfulLanguagePolicy.TRANSLATE_TO_OUTPUT:
            lines.extend(
                (
                    "> 翻译型高保真精编稿：正文依据来源 Transcript 保守翻译，不能视为讲者逐字表达。",
                    f"> 源语言：{request.source_language}",
                    f"> 目标语言：{request.target_language}",
                    "",
                )
            )
        else:
            lines.extend((f"> 来源语言：{request.source_language}", ""))
        lines.extend(("## 精编正文", ""))
        for section in sections:
            lines.extend(
                (
                    f"### {section.title}",
                    "",
                    f"<!-- time:{section.start_ms}-{section.end_ms} -->",
                    "",
                )
            )
            for paragraph in section.paragraphs:
                citations = "".join(
                    f"[^{value}]" for value in paragraph.source_segment_ids
                )
                lines.extend((f"{paragraph.text}{citations}", ""))
            summary_citations = "".join(
                f"[^{value}]" for value in section.summary.source_segment_ids
            )
            lines.extend(
                (
                    "#### AI 章节摘要",
                    "",
                    f"{section.summary.text}{summary_citations}",
                    "",
                )
            )
            lines.extend(("#### AI 关键点", ""))
            lines.extend(
                f"- {value.text}"
                + "".join(f"[^{source}]" for source in value.source_segment_ids)
                for value in section.key_points
            )
            if not section.key_points:
                lines.append("- 无")
            lines.extend(("", "#### 待复核项", ""))
            lines.extend(
                f"- [{value.category.value}] {value.description}"
                + "".join(f"[^{source}]" for source in value.source_segment_ids)
                for value in section.uncertainties
            )
            if not section.uncertainties:
                lines.append("- 无")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "FaithfulCompilationSummaryV1",
    "FaithfulCompilationUsageV1",
    "FaithfulCompiledVideoDocument",
    "FaithfulEditionCompiler",
    "FaithfulEditionRequestV1",
]

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
import json

from app.core.application.model_call_coordinator import ModelCallCoordinator
from app.core.application.video_compiler import VideoCompilationContext
from app.core.domain.video import (
    FaithfulLanguagePolicy,
    QualityOverall,
    ScreenshotRequest,
    TranscriptSegment,
    VideoDocumentKind,
)
from app.core.domain.visual_frame import VisualFrame
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
    FaithfulUncertaintyCategory,
    FaithfulUncertaintyV1,
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
    FaithfulQualityCheckV1,
    QualityCheckMethod,
    QualityCheckStatus,
    assess_faithful_edition,
)
from app.core.recipes.video.faithful_edition.review import (
    REVIEW_INSTRUCTION, FaithfulSectionReview, edit_log_markdown, parse_review,
    plain_markdown, review_payload, review_schema,
)
from app.core.recipes.video.faithful_edition.source_grounding import (
    GROUNDING_INSTRUCTION, GROUNDING_CHECK_INSTRUCTION, GROUNDING_REPAIR_INSTRUCTION, SourceGrounding,
    grounding_payload, grounding_schema, parse_grounding,
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
            or not 1 <= self.sequential_model_waves <= 9
            or type(self.repair_operation_count) is not int
            or not 0 <= self.repair_operation_count <= 2 * self.section_count
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
            or any(not isinstance(value, ScreenshotRequest) for value in screenshots)
            or len({value.segment_id for value in screenshots}) != len(screenshots)
            or any(value.segment_id not in citations for value in screenshots)
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
    review: tuple[FaithfulSectionReview, ModelExecutionResult] | None = None


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
        if request.visual_frames and request.model_binding.provider_type != "codex-app-server":
            raise DomainError("model_capability_missing", ErrorCategory.POLICY_DENIED,
                              "Faithful image review requires the wired codex-app-server image transport")
        preparation_results: list[ModelExecutionResult] = []
        preparation_waves = 0
        if request.visual_frames:
            request, preparation_results, preparation_waves = self._ground_source(request, context)
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
                max_segment_refs_per_paragraph=request.parser_limits.max_segment_refs_per_paragraph,
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
            review_plan=plan,
        )
        results = [*preparation_results, *(value.result for value in outcomes)]
        early_reviews = {
            value.section_ref.ordinal: (value.section, value.review)
            for value in outcomes if value.review is not None
        }
        results.extend(value.review[1] for value in outcomes if value.review is not None)
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
                source_corrections=request.source_corrections,
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
                    source_corrections=request.source_corrections,
                )
            )

        captions: dict[str, str] = {}
        excluded_auxiliaries: frozenset[int] = frozenset()
        if assessment.overall is not QualityOverall.FAIL:
            # A repaired section must never inherit the review of its earlier text.
            pending = [section for section in sections
                       if section.ordinal not in early_reviews
                       or early_reviews[section.ordinal][0] != section]
            late_reviews = self._review_sections(request, context, plan, pending) if pending else ()
            results.extend(result for _, result in late_reviews)
            by_ordinal = {section.ordinal: review for section, review in zip(pending, late_reviews)}
            reviews = tuple(by_ordinal[section.ordinal] if section.ordinal in by_ordinal
                            else early_reviews[section.ordinal][1] for section in sections)
            sequential_waves += 1
            failed_reviews = {section.ordinal: review for section, (review, _) in zip(sections, reviews)
                              if not review.body_pass or not review.auxiliary_pass}
            if failed_reviews and plan.max_repair_attempts == 1:
                repaired, repair_results = self._repair_reviewed_sections(
                    request, context, plan, sections, failed_reviews,
                )
                for section in repaired:
                    sections[section.ordinal] = section
                results.extend(repair_results)
                repair_count += len(repaired)
                sequential_waves += 1
                repaired_assessment = assess_faithful_edition(FaithfulEditionCandidateV1(
                    transcript=request.transcript, plan=plan, sections=tuple(sections),
                    markdown=self._assemble_markdown(request, sections),
                    source_corrections=request.source_corrections,
                ))
                if repaired_assessment.overall is QualityOverall.FAIL:
                    raise DomainError("faithful_semantic_repair_failed", ErrorCategory.RECIPE_FAILED,
                                      "Semantic repair violated deterministic fidelity checks")
                rechecks = self._review_sections(request, context, plan, repaired,
                                                shard_prefix="recheck-section")
                results.extend(result for _, result in rechecks)
                sequential_waves += 1
                updated_reviews = dict(zip((section.ordinal for section in repaired), rechecks))
                reviews = tuple(updated_reviews.get(section.ordinal, review)
                                for section, review in zip(sections, reviews))
            if any(not review.body_pass for review, _ in reviews):
                raise DomainError("faithful_review_failed", ErrorCategory.RECIPE_FAILED,
                                  "Faithful body still failed semantic review after the bounded repair policy")
            for index, (review, _) in enumerate(reviews):
                section = sections[index]
                uncertainties = list(section.uncertainties)
                for ordinal, description in review.uncertainties:
                    uncertainties.append(FaithfulUncertaintyV1(
                        len(uncertainties), FaithfulUncertaintyCategory.OTHER, description,
                        section.paragraphs[ordinal].source_segment_ids,
                    ))
                sections[index] = replace(section, uncertainties=tuple(uncertainties))
                captions.update(review.captions)
            excluded_auxiliaries = frozenset(index for index, (review, _) in enumerate(reviews)
                                            if not review.auxiliary_pass)
            markdown = self._assemble_markdown(request, sections, captions=captions,
                                               excluded_auxiliaries=excluded_auxiliaries)
            assessment = assess_faithful_edition(FaithfulEditionCandidateV1(
                transcript=request.transcript, plan=plan, sections=tuple(sections), markdown=markdown,
                source_corrections=request.source_corrections,
            ))
            assessment = replace(assessment, overall=(QualityOverall.PASS_WITH_WARNINGS
                if excluded_auxiliaries and assessment.overall is QualityOverall.PASS else assessment.overall),
                checks=(*assessment.checks, FaithfulQualityCheckV1(
                check_id="faithful_semantic_review", method=QualityCheckMethod.MODEL,
                status=QualityCheckStatus.PASS, severity="error", scope="document",
                safe_details="Each edited section was reviewed against all mapped source segments and supplied frames; not audio verification",
            ), FaithfulQualityCheckV1(
                check_id="faithful_auxiliary_review", method=QualityCheckMethod.MODEL,
                status=QualityCheckStatus.WARNING if excluded_auxiliaries else QualityCheckStatus.PASS,
                severity="warning", scope="document",
                safe_details="Optional summaries failing review were omitted; the faithful body is assessed independently",
            )))

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
        if excluded_auxiliaries:
            warning_values.append("faithful_auxiliary_omitted_after_review")
        warnings = tuple(dict.fromkeys(warning_values))
        if request.source_corrections:
            assessment = replace(assessment, checks=(*assessment.checks, FaithfulQualityCheckV1(
                check_id="source_correction_review", method=QualityCheckMethod.MODEL,
                status=QualityCheckStatus.PASS, severity="error", scope="document",
                safe_details="ASR corrections were separately checked against original segments and cited frames; original transcript retained",
            )))
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
            screenshot_requests=tuple(ScreenshotRequest(source_id) for source_id in captions),
            plan=plan,
            text_assessment=assessment,
            execution_summary=FaithfulCompilationSummaryV1(
                section_count=len(sections),
                model_operation_count=len(results),
                sequential_model_waves=sequential_waves + preparation_waves,
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

    def _ground_source(
        self, request: FaithfulEditionRequestV1, context: VideoCompilationContext,
    ) -> tuple[FaithfulEditionRequestV1, list[ModelExecutionResult], int]:
        segments = request.transcript.segments
        frames_by_id = {frame.segment_id: frame for frame in request.visual_frames}
        check_schema = json.dumps({
            "type": "object", "additionalProperties": False, "required": ["pass", "issues"],
            "properties": {"pass": {"type": "boolean"},
                           "issues": {"type": "array", "items": {"type": "string"}}},
        })

        def prepare(start: int) -> tuple[SourceGrounding, tuple[ModelExecutionResult, ...]]:
            owned = segments[start:start + 16]
            frames = tuple(frames_by_id[value.segment_id] for value in owned if value.segment_id in frames_by_id)
            context_before = segments[max(0, start - 4):start]
            context_after = segments[start + 16:start + 20]
            payload = grounding_payload(owned, frames, context_before, context_after)
            payload["source_title"] = request.source_title

            def call(stage: str, instruction: str, schema: str) -> ModelExecutionResult:
                content = json.dumps(payload, ensure_ascii=False)
                if (len(content.encode("utf-8")) + len(instruction.encode("utf-8"))
                        + len(schema.encode("utf-8")) + 2048 * len(frames) > request.max_request_bytes):
                    raise DomainError("model_request_budget_exceeded", ErrorCategory.POLICY_DENIED,
                                      "Source grounding exceeds the frozen request budget")
                return self._coordinator.execute(request.model_binding, ModelExecutionRequest(
                    schema_version=1, stage_id=stage, stage_version=6, prompt_id=stage, prompt_version=6,
                    system_instruction=instruction, user_content=content,
                    output_mode=ModelOutputMode.JSON_SCHEMA, response_schema_json=schema,
                    image_webp=tuple(frame.payload for frame in frames),
                    temperature=0 if request.model_binding.supports_temperature else None,
                    max_output_tokens=request.reserved_output_tokens,
                    timeout_seconds=request.model_binding.timeout_seconds,
                ), context.execution, f"{stage}-{start:06d}", context.cancellation_token)

            results = []
            for attempt in range(1 + request.max_repair_attempts):
                proposed = call(
                    "faithful-source-prepare" if attempt == 0 else "faithful-source-repair",
                    GROUNDING_INSTRUCTION if attempt == 0 else GROUNDING_REPAIR_INSTRUCTION,
                    grounding_schema(),
                )
                results.append(proposed)
                try:
                    grounding = parse_grounding(proposed.text, owned, frames,
                                                max_response_bytes=request.parser_limits.max_response_bytes,
                                                ignored_segment_ids=frozenset(
                                                    value.segment_id for value in (*context_before, *context_after)
                                                ))
                except DomainError as error:
                    if error.code != "faithful_source_grounding_invalid" or attempt == request.max_repair_attempts:
                        raise
                    payload["invalid_proposal"] = proposed.text
                    payload["review_feedback"] = [
                        "Proposal failed the source contract. Return only schema fields and owned IDs; "
                        f"the only writable IDs are {[value.segment_id for value in owned]}. "
                        "copy each complete before text EXACTLY from segments, never truncate it. "
                        "Preserve its whole meaning in after. Use only supplied nearby frame IDs; "
                        "numeric changes require a visible quote containing the new number. "
                        "Do not include unchanged corrections, duplicate IDs or more than two illustrations."]
                    continue
                payload["proposal"] = asdict(grounding)
                checked = call("faithful-source-check" if attempt == 0 else "faithful-source-recheck",
                               GROUNDING_CHECK_INSTRUCTION, check_schema)
                results.append(checked)
                try:
                    from app.core.recipes.video.faithful_edition.review import _unique_object
                    if len(checked.text.encode("utf-8")) > request.parser_limits.max_response_bytes:
                        raise ValueError
                    verdict = json.loads(checked.text, object_pairs_hook=_unique_object)
                    if (type(verdict) is not dict or set(verdict) != {"pass", "issues"}
                            or type(verdict["pass"]) is not bool or type(verdict["issues"]) is not list
                            or len(verdict["issues"]) > 64
                            or any(type(issue) is not str or not 1 <= len(issue.strip()) <= 2000 for issue in verdict["issues"])
                            or verdict["pass"] != (not verdict["issues"])):
                        raise ValueError
                except (ValueError, TypeError, RecursionError):
                    raise DomainError("faithful_source_review_invalid", ErrorCategory.RECIPE_FAILED,
                                      "Source correction review violated its bounded response contract") from None
                if verdict["pass"]:
                    return grounding, tuple(results)
                payload["review_feedback"] = verdict["issues"]
            raise DomainError("faithful_source_review_failed", ErrorCategory.RECIPE_FAILED,
                              "Source corrections or topic boundaries still lack video evidence after bounded repair")

        with ThreadPoolExecutor(max_workers=request.model_binding.max_concurrency) as pool:
            outcomes = tuple(pool.map(prepare, range(0, len(segments), 16)))
        corrections = tuple(value for grounding, _ in outcomes for value in grounding.corrections)
        starts = tuple(dict.fromkeys((segments[0].segment_id,
            *(value for grounding, _ in outcomes for value in grounding.chapter_start_ids))))
        selected = {value for grounding, _ in outcomes for value in grounding.illustration_ids}
        frames = tuple(frame for frame in request.visual_frames if frame.segment_id in selected)
        if len(frames) > 24:
            frames = tuple(frames[index * len(frames) // 24] for index in range(24))
        return replace(request, source_corrections=corrections, chapter_start_ids=starts,
                       visual_frames=frames), [result for _, results in outcomes for result in results], max(len(results) for _, results in outcomes)

    @staticmethod
    def _section_segments(
        request: FaithfulEditionRequestV1,
        plan: FaithfulEditionPlanV1,
        section_ref: FaithfulSectionRefV1,
    ) -> tuple[TranscriptSegment, ...]:
        excluded = frozenset(plan.excluded_segment_ids)
        corrections = {value.segment_id: value.after for value in request.source_corrections}
        return tuple(
            replace(value, text=corrections.get(value.segment_id, value.text))
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
        images_by_ordinal: dict[int, tuple[bytes, ...]] | None = None,
        review_plan: FaithfulEditionPlanV1 | None = None,
    ) -> tuple[_FaithfulSectionExecution, ...]:
        def execute_one(
            value: tuple[
                tuple[FaithfulSectionRefV1, tuple[TranscriptSegment, ...]],
                FaithfulEditionPrompt,
            ],
        ) -> _FaithfulSectionExecution:
            (section_ref, segments), prompt = value
            ids = {segment.segment_id for segment in segments}
            frames = tuple(frame for frame in request.visual_frames if frame.segment_id in ids)
            payload = json.loads(prompt.user_content)
            if frames:
                payload["frames"] = [{"image_index": index + 1, "segment_id": frame.segment_id,
                                      "timestamp_ms": frame.timestamp_ms} for index, frame in enumerate(frames)]
            model_request = ModelExecutionRequest(
                schema_version=1,
                stage_id=stage_id,
                stage_version=4,
                prompt_id=f"{stage_id}-balanced",
                prompt_version=6,
                system_instruction=prompt.system_instruction,
                user_content=json.dumps(payload, ensure_ascii=False),
                output_mode=ModelOutputMode.JSON_SCHEMA,
                response_schema_json=prompt.response_schema_json,
                image_webp=(images_by_ordinal or {}).get(section_ref.ordinal, tuple(frame.payload for frame in frames)),
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
            review = None
            if review_plan is not None:
                # Run the same deterministic checks on this section's immutable
                # source before reviewing it. The full-document gate still runs.
                local_assessment = assess_faithful_edition(FaithfulEditionCandidateV1(
                    transcript=replace(request.transcript, segments=tuple(
                        segment for segment in request.transcript.segments if segment.segment_id in ids
                    )),
                    plan=review_plan, sections=(section,),
                    markdown=self._assemble_markdown(request, [section]),
                    source_corrections=tuple(value for value in request.source_corrections
                                             if value.segment_id in ids),
                ))
                if local_assessment.overall is not QualityOverall.FAIL:
                    review = self._review_sections(request, context, review_plan, [section])[0]
            return _FaithfulSectionExecution(
                section_ref=section_ref,
                result=result,
                section=section,
                response_error=None,
                review=review,
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
                max_segment_refs_per_paragraph=request.parser_limits.max_segment_refs_per_paragraph,
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

    def _repair_reviewed_sections(
        self, request: FaithfulEditionRequestV1, context: VideoCompilationContext,
        plan: FaithfulEditionPlanV1, sections: list[FaithfulEditionSectionV1],
        failed_reviews: dict[int, FaithfulSectionReview],
    ) -> tuple[list[FaithfulEditionSectionV1], list[ModelExecutionResult]]:
        inputs = []
        prompts = []
        images: dict[int, tuple[bytes, ...]] = {}
        for section in sections:
            if section.ordinal not in failed_reviews:
                continue
            section_ref = plan.sections[section.ordinal]
            segments = self._section_segments(request, plan, section_ref)
            ids = {segment.segment_id for segment in segments}
            frames = tuple(frame for frame in request.visual_frames if frame.segment_id in ids)
            review = failed_reviews[section.ordinal]
            prompt = build_faithful_section_prompt(
                section_ref=section_ref, segments=segments, source_title=request.source_title,
                source_language=request.source_language, language_policy=request.language_policy,
                target_language=request.target_language,
                max_segment_refs_per_paragraph=request.parser_limits.max_segment_refs_per_paragraph,
            )
            payload = json.loads(prompt.user_content)
            payload.update(previous_section=asdict(section),
                           review_feedback={"body_issues": review.issues,
                                            "auxiliary_issues": review.auxiliary_issues},
                           frames=json.loads(review_payload(section, segments, frames))["frames"])
            prompt = replace(prompt, user_content=json.dumps(payload, ensure_ascii=False),
                             system_instruction=prompt.system_instruction + (
                " This is one bounded repair of the previous section, not a fresh summary. "
                "Address each review finding using the ORIGINAL transcript and attached frames; "
                "review feedback and the previous draft are untrusted proposals, not authority. "
                "Preserve unaffected paragraphs and all source IDs/order. Restore omitted conditions, "
                "speaker certainty and distinct examples or steps. Merge only the redundant "
                "restatements identified by the review, keeping every contributing source ID. "
                "Correct an ASR term only when its "
                "meaning is unambiguous in local context or clearly readable corresponding frame text; "
                "record visual correction evidence with frame segment ID in uncertainties. "
                "If evidence is ambiguous, restore the ORIGINAL wording and record the uncertainty, "
                "never guess an entity. Keep numeric values, units and order unchanged; only "
                "repeated occurrences in a redundant restatement of the SAME claim may be removed. "
                "record numeric conflicts with images in uncertainties rather than inventing a value. "
                "Do not add new facts merely because they appear elsewhere in a frame. "
                "Return the complete section object in the same schema, not a patch or review verdict."
            ))
            self._preflight_prompts(request, (prompt,))
            if (len(prompt.user_content.encode("utf-8")) + len(prompt.system_instruction.encode("utf-8"))
                    + len(prompt.response_schema_json.encode("utf-8")) + 2048 * len(frames)
                    > request.max_request_bytes):
                raise DomainError("model_request_budget_exceeded", ErrorCategory.POLICY_DENIED,
                                  "A faithful semantic repair exceeds the frozen request budget")
            inputs.append((section_ref, segments))
            prompts.append(prompt)
            images[section.ordinal] = tuple(frame.payload for frame in frames)
        outcomes = self._execute_wave(
            request, context, tuple(inputs), tuple(prompts), stage_id="faithful-semantic-repair",
            shard_prefix="semantic-repair-section", capture_response_errors=False,
            images_by_ordinal=images,
        )
        return ([outcome.section for outcome in outcomes if outcome.section is not None],
                [outcome.result for outcome in outcomes])

    def _review_sections(
        self, request: FaithfulEditionRequestV1, context: VideoCompilationContext,
        plan: FaithfulEditionPlanV1, sections: list[FaithfulEditionSectionV1],
        *, shard_prefix: str = "review-section",
    ) -> tuple[tuple[FaithfulSectionReview, ModelExecutionResult], ...]:
        def review_one(section: FaithfulEditionSectionV1) -> tuple[FaithfulSectionReview, ModelExecutionResult]:
            segments = self._section_segments(request, plan, plan.sections[section.ordinal])
            ids = {segment.segment_id for segment in segments}
            frames: tuple[VisualFrame, ...] = tuple(
                frame for frame in request.visual_frames if frame.segment_id in ids
            )
            payload = review_payload(section, segments, frames)
            schema = review_schema()
            if (len(payload.encode("utf-8")) + len(schema.encode("utf-8"))
                    + len(REVIEW_INSTRUCTION.encode("utf-8")) + 2048 * len(frames)
                    > request.max_request_bytes):
                raise DomainError("model_request_budget_exceeded", ErrorCategory.POLICY_DENIED,
                                  "A faithful review exceeds the frozen request budget")
            model_request = ModelExecutionRequest(
                schema_version=1, stage_id="faithful-review", stage_version=1,
                prompt_id="faithful-review-balanced", prompt_version=6,
                system_instruction=REVIEW_INSTRUCTION, user_content=payload,
                output_mode=ModelOutputMode.JSON_SCHEMA, response_schema_json=schema,
                temperature=0 if request.model_binding.supports_temperature else None,
                max_output_tokens=request.reserved_output_tokens,
                timeout_seconds=request.model_binding.timeout_seconds,
                image_webp=tuple(frame.payload for frame in frames),
            )
            result = self._coordinator.execute(
                request.model_binding, model_request, context.execution,
                f"{shard_prefix}-{section.ordinal:04d}", context.cancellation_token,
            )
            return parse_review(result.text, section, frames,
                                max_response_bytes=request.parser_limits.max_response_bytes), result

        if len(sections) == 1:
            return (review_one(sections[0]),)
        with ThreadPoolExecutor(max_workers=min(len(sections), request.model_binding.max_concurrency)) as pool:
            return tuple(pool.map(review_one, sections))

    @staticmethod
    def _assemble_markdown(
        request: FaithfulEditionRequestV1,
        sections: list[FaithfulEditionSectionV1],
        *, captions: dict[str, str] | None = None,
        excluded_auxiliaries: frozenset[int] = frozenset(),
    ) -> str:
        captions = captions or {}
        by_id = {segment.segment_id: segment for segment in request.transcript.segments}
        frame_times = {frame.segment_id: frame.timestamp_ms for frame in request.visual_frames}
        def clock(milliseconds: int) -> str:
            seconds = milliseconds // 1000
            return f"{seconds // 60:02d}:{seconds % 60:02d}"

        lines = [f"# {plain_markdown(request.source_title)} — 高保真精编稿", "",
                 "> 正文仅依据转录稿保守整理；疑似识别错误保留待核对，不代表已核验原音频。", ""]
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
        if request.source_corrections:
            lines[2] = "> 正文依据转录与对应画面校正整理；未逐句核验原音频。"
        overview_sections = [section for section in sections
                             if section.summary.text and section.ordinal not in excluded_auxiliaries]
        if overview_sections:
            lines.extend(("## 全文概览（AI）", ""))
            lines.extend(f"- {plain_markdown(section.summary.text)}"
                         + "".join(f"[^{source}]" for source in section.summary.source_segment_ids)
                         for section in overview_sections)
            lines.append("")
        lines.extend(("## 精编正文", ""))
        for section in sections:
            lines.extend(
                (
                    f"### {clock(section.start_ms)}–{clock(section.end_ms)} {plain_markdown(section.title)}",
                    "",
                    f"<!-- time:{section.start_ms}-{section.end_ms} -->",
                    "",
                )
            )
            for paragraph in section.paragraphs:
                citations = "".join(
                    f"[^{value}]" for value in paragraph.source_segment_ids
                )
                lines.extend((f"{plain_markdown(paragraph.text)}{citations}", ""))
                for source_id in paragraph.source_segment_ids:
                    if source_id in captions:
                        lines.extend((f"[SCREENSHOT:{source_id}]", "",
                                      f"*画面 {clock(frame_times.get(source_id, by_id[source_id].start_ms))}：{plain_markdown(captions[source_id])}*", ""))
                local_uncertainties = [value for value in section.uncertainties
                                       if set(value.source_segment_ids).intersection(paragraph.source_segment_ids)]
                if local_uncertainties:
                    lines.extend(("> 待核对：" + "；".join(dict.fromkeys(
                        plain_markdown(value.description) for value in local_uncertainties
                    )), ""))

        lines.extend(("## AI 辅助摘要（不属于原文）", ""))
        if excluded_auxiliaries:
            lines.extend(("> 部分章节的 AI 摘要未通过复核，已省略；不影响上方已独立复核的正文。", ""))
        for section in sections:
            if section.ordinal in excluded_auxiliaries or not section.key_points:
                continue
            lines.extend((f"### {clock(section.start_ms)}–{clock(section.end_ms)} {plain_markdown(section.title)}", ""))
            lines.extend(("**AI 关键点**", ""))
            lines.extend(
                f"- {plain_markdown(value.text)}"
                + "".join(f"[^{source}]" for source in value.source_segment_ids)
                for value in section.key_points
            )
            lines.extend(("", "#### 待复核项", ""))
            lines.extend(
                f"- [{value.category.value}] {plain_markdown(value.description)}"
                + "".join(f"[^{source}]" for source in value.source_segment_ids)
                for value in section.uncertainties
            )
            if not section.uncertainties:
                lines.append("- 无")
            lines.append("")
        lines.extend((edit_log_markdown(sections, request.transcript.segments,
                                      source_corrections=request.source_corrections), ""))
        return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "FaithfulCompilationSummaryV1",
    "FaithfulCompilationUsageV1",
    "FaithfulCompiledVideoDocument",
    "FaithfulEditionCompiler",
    "FaithfulEditionRequestV1",
]

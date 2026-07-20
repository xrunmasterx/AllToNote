from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace

from app.core.application.model_call_coordinator import (
    ModelCallCoordinator,
    ModelCallExecution,
)
from app.core.domain.ids import sha256_digest
from app.core.domain.transcript import transcript_sha256
from app.core.domain.video import (
    ScreenshotPolicy,
    ScreenshotRequest,
    TranscriptDocument,
    TranscriptSegment,
    VideoDocumentKind,
)
from app.core.errors import DomainError, ErrorCategory
from app.core.portable.jsonio import encode_json
from app.core.ports.model_executor import (
    ModelExecutionRequest,
    ModelExecutionResult,
    ModelOutputMode,
)
from app.core.ports.source import CancellationTokenPort
from app.core.recipes.video.compilation.contracts import (
    ChunkKnowledgeMapV1,
    CompilationQualityProfile,
    CompilationTopology,
    ComposerParserLimitsV1,
    ComposedKnowledgeDraftV1,
    CompositionCoverageLedgerV1,
    CoverageInputKind,
    CoverageOmissionV1,
    KnowledgeItemV1,
    KnowledgeMapParserLimitsV1,
    VideoCompilationPlanV1,
    VideoCompilationPlanningRequestV1,
)
from app.core.recipes.video.compilation.pipeline import (
    parse_chunk_knowledge_map,
    parse_composed_knowledge_draft,
    parse_consolidated_knowledge,
    plan_video_compilation,
)
from app.core.recipes.video.compilation.prompts import (
    CompilationPrompt,
    build_consolidation_prompt,
    build_global_composer_prompt,
    build_knowledge_map_prompt,
    build_knowledge_repair_prompt,
)
from app.core.recipes.video.compilation.quality import (
    CoverageOmissionV1 as TextCoverageOmissionV1,
    KnowledgeNoteCandidateV1,
    KnowledgeNoteRepairRequestV1,
    evaluate_knowledge_note,
)


_CONSOLIDATION_STAGE_VERSION = 2
_CONSOLIDATION_PROMPT_VERSION = 2
_CONSOLIDATION_PARSER_VERSION = 1
_COMPOSER_STAGE_VERSION = 2
_COMPOSER_PROMPT_VERSION = 5
_COMPOSER_PARSER_VERSION = 1
_REPAIR_STAGE_VERSION = 2
_REPAIR_PROMPT_VERSION = 5
_QUALITY_BEHAVIOR_VERSION = 1
_CITATION_FREEZE_BEHAVIOR_VERSION = 2
_KNOWLEDGE_ITEM_PROJECTION_BYTES = 192


def _invalid(message: str) -> DomainError:
    return DomainError(
        "knowledge_compilation_contract_invalid",
        ErrorCategory.INVALID_REQUEST,
        message,
    )


@dataclass(frozen=True)
class KnowledgeCompilationRequestV1:
    schema_version: int
    planning_request: VideoCompilationPlanningRequestV1 = field(repr=False)
    source_title: str
    output_language: str
    style: str
    screenshot_policy: ScreenshotPolicy
    map_parser_limits: KnowledgeMapParserLimitsV1
    composer_parser_limits: ComposerParserLimitsV1

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or not isinstance(
                self.planning_request, VideoCompilationPlanningRequestV1
            )
            or self.planning_request.quality_profile
            is not CompilationQualityProfile.BALANCED
            or any(
                type(value) is not str or not value.strip()
                for value in (self.source_title, self.output_language, self.style)
            )
            or not isinstance(self.screenshot_policy, ScreenshotPolicy)
            or not isinstance(self.map_parser_limits, KnowledgeMapParserLimitsV1)
            or not isinstance(self.composer_parser_limits, ComposerParserLimitsV1)
            or self.map_parser_limits.max_response_bytes
            > self.planning_request.map_output_byte_budget_per_chunk
        ):
            raise _invalid("Knowledge compilation request is invalid")


@dataclass(frozen=True)
class VideoCompilationContext:
    execution: ModelCallExecution
    cancellation_token: CancellationTokenPort = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.execution, ModelCallExecution) or not callable(
            getattr(self.cancellation_token, "raise_if_cancelled", None)
        ):
            raise _invalid("Video compilation context is invalid")


@dataclass(frozen=True)
class DocumentCompilationSummaryV1:
    topology: CompilationTopology
    chunk_count: int
    knowledge_item_count: int
    model_operation_count: int
    sequential_model_waves: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.topology, CompilationTopology)
            or type(self.chunk_count) is not int
            or self.chunk_count < 1
            or type(self.knowledge_item_count) is not int
            or self.knowledge_item_count < 0
            or type(self.model_operation_count) is not int
            or self.model_operation_count < 1
            or type(self.sequential_model_waves) is not int
            or not 1 <= self.sequential_model_waves <= self.model_operation_count
        ):
            raise _invalid("Document compilation summary is invalid")


@dataclass(frozen=True)
class CompilationUsageV1:
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
            raise _invalid("Compilation usage is invalid")


@dataclass(frozen=True)
class CompiledVideoDocument:
    document_kind: VideoDocumentKind
    model_identity: str
    markdown: str
    cited_segment_ids: tuple[str, ...]
    screenshot_requests: tuple[ScreenshotRequest, ...]
    coverage: CompositionCoverageLedgerV1
    plan: VideoCompilationPlanV1
    execution_summary: DocumentCompilationSummaryV1
    usage: CompilationUsageV1
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        try:
            citations = tuple(self.cited_segment_ids)
            screenshots = tuple(self.screenshot_requests)
            warnings = tuple(self.warnings)
        except TypeError:
            raise _invalid("Compiled video document is invalid") from None
        if (
            self.document_kind is not VideoDocumentKind.KNOWLEDGE_NOTE
            or type(self.model_identity) is not str
            or not self.model_identity.strip()
            or type(self.markdown) is not str
            or not self.markdown.strip()
            or any(type(value) is not str or not value for value in citations)
            or len(citations) != len(set(citations))
            or any(not isinstance(value, ScreenshotRequest) for value in screenshots)
            or not isinstance(self.coverage, CompositionCoverageLedgerV1)
            or not isinstance(self.plan, VideoCompilationPlanV1)
            or not isinstance(self.execution_summary, DocumentCompilationSummaryV1)
            or not isinstance(self.usage, CompilationUsageV1)
            or any(type(value) is not str or not value.strip() for value in warnings)
            or len(warnings) != len(set(warnings))
        ):
            raise _invalid("Compiled video document is invalid")
        object.__setattr__(self, "cited_segment_ids", citations)
        object.__setattr__(self, "screenshot_requests", screenshots)
        object.__setattr__(self, "warnings", warnings)


@dataclass(frozen=True)
class _ConsolidationOutcome:
    items: tuple[KnowledgeItemV1, ...]
    root_lineage: dict[str, tuple[str, ...]]
    root_omissions: tuple[CoverageOmissionV1, ...]
    results: tuple[ModelExecutionResult, ...]
    warnings: tuple[str, ...]
    wave_count: int


class VideoKnowledgeCompiler:
    """Balanced knowledge compiler; Provider execution remains behind Coordinator."""

    def __init__(self, coordinator: ModelCallCoordinator) -> None:
        if not isinstance(coordinator, ModelCallCoordinator):
            raise _invalid("Knowledge compiler requires ModelCallCoordinator")
        self._coordinator = coordinator

    @staticmethod
    def behavior_identity() -> str:
        return sha256_digest(
            encode_json(
                {
                    "citation_freeze": _CITATION_FREEZE_BEHAVIOR_VERSION,
                    "composer": {
                        "parser": _COMPOSER_PARSER_VERSION,
                        "prompt": _COMPOSER_PROMPT_VERSION,
                        "stage": _COMPOSER_STAGE_VERSION,
                    },
                    "consolidation": {
                        "parser": _CONSOLIDATION_PARSER_VERSION,
                        "prompt": _CONSOLIDATION_PROMPT_VERSION,
                        "stage": _CONSOLIDATION_STAGE_VERSION,
                    },
                    "quality": _QUALITY_BEHAVIOR_VERSION,
                    "repair": {
                        "prompt": _REPAIR_PROMPT_VERSION,
                        "stage": _REPAIR_STAGE_VERSION,
                    },
                    "schema_version": 1,
                }
            )
        )

    def compile(
        self,
        request: KnowledgeCompilationRequestV1,
        context: VideoCompilationContext,
    ) -> CompiledVideoDocument:
        if not isinstance(request, KnowledgeCompilationRequestV1) or not isinstance(
            context, VideoCompilationContext
        ):
            raise _invalid("Knowledge compiler requires the v1 contracts")
        binding = request.planning_request.model_binding
        if not binding.supports_structured_output:
            raise DomainError(
                "model_capability_missing",
                ErrorCategory.POLICY_DENIED,
                "Balanced knowledge compilation requires structured model output",
            )
        plan = self._freeze_feasible_topology(
            request, plan_video_compilation(request.planning_request)
        )
        transcript = request.planning_request.transcript
        chunk_segments = self._resolve_chunks(plan, transcript)
        model_results: list[ModelExecutionResult] = []
        warnings: list[str] = []

        if plan.topology is CompilationTopology.DIRECT:
            quality_input_kind = CoverageInputKind.SEGMENT
            quality_input_ids = tuple(
                segment.segment_id for segment in transcript.segments
            )
            quality_allowed_segment_ids = quality_input_ids
            composition, result = self._compose(
                request,
                context,
                plan,
                input_kind=CoverageInputKind.SEGMENT,
                input_ids=tuple(segment.segment_id for segment in transcript.segments),
                items=(),
                segments=transcript.segments,
            )
            model_results.append(result)
            sequential_waves = 1
            knowledge_item_count = 0
        else:
            self._preflight_map_requests(request, plan, chunk_segments)
            mapped = self._map_chunks(
                request, context, plan, chunk_segments
            )
            maps = [value[0] for value in mapped]
            model_results.extend(value[1] for value in mapped)
            for knowledge_map in maps:
                warnings.extend(knowledge_map.warnings)
            items = tuple(item for knowledge_map in maps for item in knowledge_map.items)
            root_items = items
            sequential_waves = 2
            if plan.topology is CompilationTopology.HIERARCHICAL_COMPOSE:
                consolidation = self._consolidate(
                    request, context, plan, items
                )
                items = consolidation.items
                model_results.extend(consolidation.results)
                warnings.extend(consolidation.warnings)
                sequential_waves += consolidation.wave_count
            quality_allowed_segment_ids = self._composition_segment_ids(
                request,
                input_kind=CoverageInputKind.KNOWLEDGE_ITEM,
                items=items,
                segments=(),
            )
            composition, result = self._compose(
                request,
                context,
                plan,
                input_kind=CoverageInputKind.KNOWLEDGE_ITEM,
                input_ids=tuple(item.knowledge_item_id for item in items),
                items=items,
                segments=(),
            )
            if plan.topology is CompilationTopology.HIERARCHICAL_COMPOSE:
                composition = self._expand_root_coverage(
                    composition,
                    root_items=root_items,
                    final_lineage=consolidation.root_lineage,
                    consolidation_omissions=consolidation.root_omissions,
                )
            quality_input_kind = CoverageInputKind.KNOWLEDGE_ITEM
            quality_input_ids = tuple(
                item.knowledge_item_id for item in root_items
            )
            model_results.append(result)
            knowledge_item_count = len(root_items)

        composition, repair_result = self._apply_quality_gate(
            request,
            context,
            plan,
            composition,
            input_kind=quality_input_kind,
            input_ids=quality_input_ids,
            allowed_segment_ids=quality_allowed_segment_ids,
        )
        if repair_result is not None:
            model_results.append(repair_result)
            sequential_waves += 1
        warnings.extend(composition.warnings)
        for result in model_results:
            warnings.extend(result.warnings)
        token_counts_complete = all(
            result.input_tokens is not None and result.output_tokens is not None
            for result in model_results
        )
        model_identities = {
            result.actual_model_identity for result in model_results
        }
        if model_identities != {binding.model_identity}:
            raise DomainError(
                "model_identity_mismatch",
                ErrorCategory.RECIPE_FAILED,
                "Knowledge compilation results do not match the frozen model binding",
            )
        return CompiledVideoDocument(
            document_kind=VideoDocumentKind.KNOWLEDGE_NOTE,
            model_identity=binding.model_identity,
            markdown=composition.markdown,
            cited_segment_ids=composition.cited_segment_ids,
            screenshot_requests=composition.screenshot_requests,
            coverage=composition.coverage,
            plan=plan,
            execution_summary=DocumentCompilationSummaryV1(
                topology=plan.topology,
                chunk_count=len(plan.transcript_chunks),
                knowledge_item_count=knowledge_item_count,
                model_operation_count=len(model_results),
                sequential_model_waves=sequential_waves,
            ),
            usage=CompilationUsageV1(
                input_tokens=sum(result.input_tokens or 0 for result in model_results),
                output_tokens=sum(result.output_tokens or 0 for result in model_results),
                token_counts_complete=token_counts_complete,
            ),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def _apply_quality_gate(
        self,
        request: KnowledgeCompilationRequestV1,
        context: VideoCompilationContext,
        plan: VideoCompilationPlanV1,
        composition: ComposedKnowledgeDraftV1,
        *,
        input_kind: CoverageInputKind,
        input_ids: tuple[str, ...],
        allowed_segment_ids: tuple[str, ...],
    ) -> tuple[ComposedKnowledgeDraftV1, ModelExecutionResult | None]:
        candidate = KnowledgeNoteCandidateV1(
            markdown=composition.markdown,
            allowed_segment_ids=allowed_segment_ids,
            required_coverage_input_ids=input_ids,
            covered_coverage_input_ids=composition.coverage.covered_input_ids,
            omissions=tuple(
                TextCoverageOmissionV1(value.input_id, value.reason)
                for value in composition.coverage.omissions
            ),
        )
        repair_result: ModelExecutionResult | None = None

        def repair(
            repair_request: KnowledgeNoteRepairRequestV1,
        ) -> KnowledgeNoteCandidateV1:
            nonlocal repair_result
            prompt = build_knowledge_repair_prompt(
                markdown=candidate.markdown,
                allowed_segment_ids=allowed_segment_ids,
                covered_input_ids=composition.coverage.covered_input_ids,
                omissions=tuple(
                    (value.input_id, value.reason)
                    for value in composition.coverage.omissions
                ),
                failed_checks=tuple(
                    {
                        "check_id": check.check_id,
                        "line_numbers": list(check.line_numbers),
                        "reason": check.reason,
                        "related_ids": list(check.related_ids),
                    }
                    for check in repair_request.failed_checks
                ),
                limits=request.composer_parser_limits,
            )
            model_request = ModelExecutionRequest(
                schema_version=1,
                stage_id="knowledge-text-repair",
                stage_version=_REPAIR_STAGE_VERSION,
                prompt_id="knowledge-text-repair-balanced",
                prompt_version=_REPAIR_PROMPT_VERSION,
                system_instruction=prompt.system_instruction,
                user_content=prompt.user_content,
                output_mode=ModelOutputMode.JSON_SCHEMA,
                response_schema_json=prompt.response_schema_json,
                temperature=(
                    0
                    if request.planning_request.model_binding.supports_temperature
                    else None
                ),
                max_output_tokens=request.planning_request.reserved_output_tokens,
                timeout_seconds=(
                    request.planning_request.model_binding.timeout_seconds
                ),
            )
            self._ensure_request_fits(plan, model_request)
            result = self._coordinator.execute(
                request.planning_request.model_binding,
                model_request,
                context.execution,
                "repair-final",
                context.cancellation_token,
            )
            repaired = parse_composed_knowledge_draft(
                result.text,
                input_kind=input_kind,
                allowed_input_ids=input_ids,
                allowed_segment_ids=allowed_segment_ids,
                allow_screenshots=(
                    request.screenshot_policy is ScreenshotPolicy.ON_DEMAND
                ),
                limits=request.composer_parser_limits,
            )
            if (
                repaired.coverage != composition.coverage
                or repaired.cited_segment_ids != composition.cited_segment_ids
                or repaired.screenshot_requests != composition.screenshot_requests
            ):
                raise DomainError(
                    "knowledge_note_repair_context_changed",
                    ErrorCategory.RECIPE_FAILED,
                    "Knowledge note repair changed the frozen evidence context",
                )
            repair_result = result
            return KnowledgeNoteCandidateV1(
                markdown=repaired.markdown,
                allowed_segment_ids=allowed_segment_ids,
                required_coverage_input_ids=input_ids,
                covered_coverage_input_ids=repaired.coverage.covered_input_ids,
                omissions=tuple(
                    TextCoverageOmissionV1(value.input_id, value.reason)
                    for value in repaired.coverage.omissions
                ),
            )

        outcome = evaluate_knowledge_note(
            candidate,
            repair=(
                repair
                if request.planning_request.max_repair_attempts == 1
                else None
            ),
        )
        if not outcome.publish_eligible or outcome.execution_error is not None:
            raise DomainError(
                "knowledge_note_quality_failed",
                ErrorCategory.RECIPE_FAILED,
                "Knowledge note did not pass the required text quality gates",
            )
        final = outcome.final_candidate
        return (
            replace(
                composition,
                markdown=final.markdown,
                cited_segment_ids=outcome.assessment.cited_segment_ids,
            ),
            repair_result,
        )

    def _freeze_feasible_topology(
        self,
        request: KnowledgeCompilationRequestV1,
        plan: VideoCompilationPlanV1,
    ) -> VideoCompilationPlanV1:
        """Close known downstream capacity constraints before the first paid call."""

        capacity = (
            request.composer_parser_limits.max_coverage_items
            + request.composer_parser_limits.max_omissions
        )
        chunk_count = len(plan.transcript_chunks)
        worst_map_item_count = chunk_count * request.map_parser_limits.max_items
        planning = request.planning_request
        aggregate_map_tokens = (
            chunk_count * planning.estimated_map_output_tokens_per_chunk
        )
        aggregate_map_bytes = chunk_count * self._projected_map_output_byte_budget(
            request
        )
        composer_token_budget = (
            plan.chunk_input_token_budget - planning.prompt_overhead_tokens
        )
        composer_byte_budget = (
            plan.chunk_input_byte_budget - planning.prompt_overhead_bytes
        )
        base_composer = self._build_compose_request(
            request,
            input_kind=CoverageInputKind.KNOWLEDGE_ITEM,
            input_ids=(),
            items=(),
            segments=(),
        )
        safe_input_bytes = min(
            plan.chunk_input_token_budget,
            plan.chunk_input_byte_budget,
        )
        map_compose_is_feasible = (
            worst_map_item_count <= capacity
            and aggregate_map_tokens <= composer_token_budget
            and aggregate_map_bytes <= composer_byte_budget
            and self._encoded_request_bytes(base_composer) + aggregate_map_bytes
            <= safe_input_bytes
        )

        if plan.topology is CompilationTopology.DIRECT:
            transcript = planning.transcript
            direct_request = self._build_compose_request(
                request,
                input_kind=CoverageInputKind.SEGMENT,
                input_ids=tuple(
                    segment.segment_id for segment in transcript.segments
                ),
                items=(),
                segments=transcript.segments,
            )
            if len(transcript.segments) <= capacity and self._request_fits(
                plan, direct_request
            ):
                return plan
            topology = (
                CompilationTopology.MAP_COMPOSE
                if map_compose_is_feasible
                else CompilationTopology.HIERARCHICAL_COMPOSE
            )
            frozen = replace(
                plan,
                topology=topology,
                expected_sequential_model_waves=(
                    2 if topology is CompilationTopology.MAP_COMPOSE else 3
                ),
            )
            self._ensure_hierarchy_feasible(request, frozen)
            return frozen

        if (
            plan.topology is CompilationTopology.MAP_COMPOSE
            and not map_compose_is_feasible
        ):
            frozen = replace(
                plan,
                topology=CompilationTopology.HIERARCHICAL_COMPOSE,
                expected_sequential_model_waves=max(
                    3, plan.expected_sequential_model_waves
                ),
            )
            self._ensure_hierarchy_feasible(request, frozen)
            return frozen
        self._ensure_hierarchy_feasible(request, plan)
        return plan

    def _ensure_hierarchy_feasible(
        self,
        request: KnowledgeCompilationRequestV1,
        plan: VideoCompilationPlanV1,
    ) -> None:
        if plan.topology is not CompilationTopology.HIERARCHICAL_COMPOSE:
            return
        if (
            request.composer_parser_limits.max_coverage_items < 2
            or self._consolidation_fan_in(request, plan) < 2
        ):
            raise DomainError(
                "model_context_insufficient",
                ErrorCategory.INVALID_REQUEST,
                "Frozen budgets cannot make progress consolidating Knowledge Maps",
            )

    @staticmethod
    def _expand_root_coverage(
        composition: ComposedKnowledgeDraftV1,
        *,
        root_items: tuple[KnowledgeItemV1, ...],
        final_lineage: dict[str, tuple[str, ...]],
        consolidation_omissions: tuple[CoverageOmissionV1, ...],
    ) -> ComposedKnowledgeDraftV1:
        root_order = tuple(item.knowledge_item_id for item in root_items)
        outcomes: dict[str, str | None] = {}

        def record(root_id: str, reason: str | None) -> None:
            if root_id in outcomes:
                raise DomainError(
                    "knowledge_consolidation_coverage_invalid",
                    ErrorCategory.RECIPE_FAILED,
                    "A root knowledge item has multiple hierarchy outcomes",
                )
            outcomes[root_id] = reason

        for omission in consolidation_omissions:
            record(omission.input_id, omission.reason)
        for final_id in composition.coverage.covered_input_ids:
            for root_id in final_lineage.get(final_id, ()):
                record(root_id, None)
        for omission in composition.coverage.omissions:
            for root_id in final_lineage.get(omission.input_id, ()):
                record(root_id, omission.reason)
        if set(outcomes) != set(root_order):
            raise DomainError(
                "knowledge_consolidation_coverage_invalid",
                ErrorCategory.RECIPE_FAILED,
                "Hierarchy coverage does not close over original knowledge items",
            )
        return replace(
            composition,
            coverage=CompositionCoverageLedgerV1(
                input_kind=CoverageInputKind.KNOWLEDGE_ITEM,
                covered_input_ids=tuple(
                    root_id for root_id in root_order if outcomes[root_id] is None
                ),
                omissions=tuple(
                    CoverageOmissionV1(root_id, outcomes[root_id] or "omitted")
                    for root_id in root_order
                    if outcomes[root_id] is not None
                ),
            ),
        )

    @staticmethod
    def _resolve_chunks(
        plan: VideoCompilationPlanV1,
        transcript: TranscriptDocument,
    ) -> tuple[tuple[TranscriptSegment, ...], ...]:
        if plan.transcript_sha256 != transcript_sha256(transcript):
            raise DomainError(
                "video_compilation_plan_invalid",
                ErrorCategory.CONFLICT,
                "Compilation plan does not match the Transcript",
            )
        resolved: list[tuple[TranscriptSegment, ...]] = []
        for reference in plan.transcript_chunks:
            segments = transcript.segments[
                reference.start_segment_ordinal : reference.end_segment_ordinal_exclusive
            ]
            ids = [segment.segment_id for segment in segments]
            if (
                not segments
                or ids[0] != reference.start_segment_id
                or ids[-1] != reference.end_segment_id
                or segments[0].start_ms != reference.start_ms
                or max(segment.end_ms for segment in segments) != reference.end_ms
                or sha256_digest(encode_json(ids)) != reference.segment_ids_sha256
            ):
                raise DomainError(
                    "video_compilation_plan_invalid",
                    ErrorCategory.CONFLICT,
                    "Compilation chunk reference does not match the Transcript",
                )
            resolved.append(segments)
        if sum(len(value) for value in resolved) != len(transcript.segments):
            raise DomainError(
                "video_compilation_plan_invalid",
                ErrorCategory.CONFLICT,
                "Compilation chunks do not cover the Transcript",
            )
        return tuple(resolved)

    def _map_chunks(
        self,
        request: KnowledgeCompilationRequestV1,
        context: VideoCompilationContext,
        plan: VideoCompilationPlanV1,
        chunks: tuple[tuple[TranscriptSegment, ...], ...],
    ) -> list[tuple[ChunkKnowledgeMapV1, ModelExecutionResult]]:
        def execute(value: tuple[int, tuple[TranscriptSegment, ...]]):
            ordinal, segments = value
            context.cancellation_token.raise_if_cancelled()
            prompt = build_knowledge_map_prompt(
                chunk_ordinal=ordinal,
                source_title=request.source_title,
                output_language=request.output_language,
                segments=segments,
                limits=request.map_parser_limits,
            )
            model_request = self._model_request(
                request,
                stage_id=plan.stage_id,
                stage_version=plan.stage_version,
                prompt_id=plan.prompt_id,
                prompt_version=plan.prompt_version,
                max_output_tokens=request.planning_request.estimated_map_output_tokens_per_chunk,
                prompt=prompt,
            )
            self._ensure_request_fits(plan, model_request)
            result = self._coordinator.execute(
                request.planning_request.model_binding,
                model_request,
                context.execution,
                f"chunk-{ordinal:04d}",
                context.cancellation_token,
            )
            reference = plan.transcript_chunks[ordinal]
            parsed = parse_chunk_knowledge_map(
                result.text,
                stage_id=plan.stage_id,
                stage_version=plan.stage_version,
                transcript_sha256=plan.transcript_sha256,
                chunk_ordinal=ordinal,
                chunk_sha256=reference.segment_ids_sha256,
                allowed_segment_ids=tuple(item.segment_id for item in segments),
                limits=request.map_parser_limits,
            )
            return parsed, result

        indexed = list(enumerate(chunks))
        if plan.extraction_concurrency == 1:
            return [execute(value) for value in indexed]
        with ThreadPoolExecutor(max_workers=plan.extraction_concurrency) as pool:
            return list(pool.map(execute, indexed))

    def _preflight_map_requests(
        self,
        request: KnowledgeCompilationRequestV1,
        plan: VideoCompilationPlanV1,
        chunks: tuple[tuple[TranscriptSegment, ...], ...],
    ) -> None:
        for ordinal, segments in enumerate(chunks):
            prompt = build_knowledge_map_prompt(
                chunk_ordinal=ordinal,
                source_title=request.source_title,
                output_language=request.output_language,
                segments=segments,
                limits=request.map_parser_limits,
            )
            model_request = self._model_request(
                request,
                stage_id=plan.stage_id,
                stage_version=plan.stage_version,
                prompt_id=plan.prompt_id,
                prompt_version=plan.prompt_version,
                max_output_tokens=(
                    request.planning_request.estimated_map_output_tokens_per_chunk
                ),
                prompt=prompt,
            )
            self._ensure_request_fits(plan, model_request)

    def _consolidate(
        self,
        request: KnowledgeCompilationRequestV1,
        context: VideoCompilationContext,
        plan: VideoCompilationPlanV1,
        initial_items: tuple[KnowledgeItemV1, ...],
    ) -> _ConsolidationOutcome:
        items = initial_items
        results: list[ModelExecutionResult] = []
        warnings: list[str] = []
        root_lineage = {
            item.knowledge_item_id: (item.knowledge_item_id,)
            for item in initial_items
        }
        root_omissions: list[CoverageOmissionV1] = []
        if self._final_composition_fits(request, plan, items):
            return _ConsolidationOutcome(
                items=items,
                root_lineage=root_lineage,
                root_omissions=(),
                results=(),
                warnings=(),
                wave_count=0,
            )
        fan_in = self._consolidation_fan_in(request, plan)
        batch_size = min(
            request.composer_parser_limits.max_coverage_items,
            max(2, fan_in * request.map_parser_limits.max_items),
        )
        if batch_size < 2:
            raise DomainError(
                "model_context_insufficient",
                ErrorCategory.INVALID_REQUEST,
                "Knowledge consolidation requires coverage capacity for two inputs",
            )
        batch_ordinal = 0
        wave_count = 0
        maximum_waves = max(1, len(initial_items) - 1)
        while not self._final_composition_fits(request, plan, items):
            if len(items) < 2 or wave_count >= maximum_waves:
                raise DomainError(
                    "model_context_insufficient",
                    ErrorCategory.RECIPE_FAILED,
                    "Knowledge items cannot fit the frozen Global Composer budget",
                )
            previous_count = len(items)
            next_items: list[KnowledgeItemV1] = []
            next_lineage: dict[str, tuple[str, ...]] = {}
            cursor = 0
            while cursor < len(items):
                batch = items[cursor : cursor + batch_size]
                if len(batch) == 1:
                    next_items.extend(batch)
                    item_id = batch[0].knowledge_item_id
                    next_lineage[item_id] = root_lineage[item_id]
                    cursor += 1
                    continue
                prompt = build_consolidation_prompt(
                    batch_ordinal=batch_ordinal,
                    output_language=request.output_language,
                    items=batch,
                    item_limits=request.map_parser_limits,
                    coverage_limits=request.composer_parser_limits,
                )
                model_request = self._model_request(
                    request,
                    stage_id="knowledge-consolidate",
                    stage_version=_CONSOLIDATION_STAGE_VERSION,
                    prompt_id="knowledge-consolidate-balanced",
                    prompt_version=_CONSOLIDATION_PROMPT_VERSION,
                    max_output_tokens=(
                        request.planning_request.estimated_map_output_tokens_per_chunk
                    ),
                    prompt=prompt,
                )
                while len(batch) > 2 and not self._request_fits(plan, model_request):
                    batch = batch[:-1]
                    prompt = build_consolidation_prompt(
                        batch_ordinal=batch_ordinal,
                        output_language=request.output_language,
                        items=batch,
                        item_limits=request.map_parser_limits,
                        coverage_limits=request.composer_parser_limits,
                    )
                    model_request = self._model_request(
                        request,
                        stage_id="knowledge-consolidate",
                        stage_version=_CONSOLIDATION_STAGE_VERSION,
                        prompt_id="knowledge-consolidate-balanced",
                        prompt_version=_CONSOLIDATION_PROMPT_VERSION,
                        max_output_tokens=(
                            request.planning_request.estimated_map_output_tokens_per_chunk
                        ),
                        prompt=prompt,
                    )
                self._ensure_request_fits(plan, model_request)
                input_ids = tuple(item.knowledge_item_id for item in batch)
                batch_sha256 = sha256_digest(encode_json(list(input_ids)))
                result = self._coordinator.execute(
                    request.planning_request.model_binding,
                    model_request,
                    context.execution,
                    f"consolidate-{wave_count:02d}-{batch_ordinal:04d}",
                    context.cancellation_token,
                )
                parsed = parse_consolidated_knowledge(
                    result.text,
                    stage_id="knowledge-consolidate",
                    stage_version=_CONSOLIDATION_STAGE_VERSION,
                    transcript_sha256=plan.transcript_sha256,
                    batch_ordinal=batch_ordinal,
                    batch_sha256=batch_sha256,
                    input_items=batch,
                    item_limits=request.map_parser_limits,
                    coverage_limits=request.composer_parser_limits,
                )
                for value in parsed.items:
                    roots = tuple(
                        root_id
                        for input_id in value.merged_from
                        for root_id in root_lineage[input_id]
                    )
                    next_items.append(value.item)
                    next_lineage[value.item.knowledge_item_id] = roots
                for omission in parsed.omissions:
                    root_omissions.extend(
                        CoverageOmissionV1(root_id, omission.reason)
                        for root_id in root_lineage[omission.input_id]
                    )
                warnings.extend(parsed.warnings)
                warnings.extend(
                    f"consolidation-omission:{value.input_id}"
                    for value in parsed.omissions
                )
                results.append(result)
                batch_ordinal += 1
                cursor += len(batch)
            items = tuple(next_items)
            root_lineage = next_lineage
            if len(items) >= previous_count:
                raise DomainError(
                    "knowledge_consolidation_no_progress",
                    ErrorCategory.RECIPE_FAILED,
                    "Knowledge consolidation did not reduce its input",
                )
            wave_count += 1
        return _ConsolidationOutcome(
            items=items,
            root_lineage=root_lineage,
            root_omissions=tuple(root_omissions),
            results=tuple(results),
            warnings=tuple(warnings),
            wave_count=wave_count,
        )

    @staticmethod
    def _consolidation_fan_in(
        request: KnowledgeCompilationRequestV1,
        plan: VideoCompilationPlanV1,
    ) -> int:
        base = VideoKnowledgeCompiler._model_request(
            request,
            stage_id="knowledge-consolidate",
            stage_version=_CONSOLIDATION_STAGE_VERSION,
            prompt_id="knowledge-consolidate-balanced",
            prompt_version=_CONSOLIDATION_PROMPT_VERSION,
            max_output_tokens=(
                request.planning_request.estimated_map_output_tokens_per_chunk
            ),
            prompt=build_consolidation_prompt(
                batch_ordinal=0,
                output_language=request.output_language,
                items=(),
                item_limits=request.map_parser_limits,
                coverage_limits=request.composer_parser_limits,
            ),
        )
        base_bytes = VideoKnowledgeCompiler._encoded_request_bytes(base)
        safe_budget = min(
            plan.chunk_input_token_budget,
            plan.chunk_input_byte_budget,
        )
        available = safe_budget - base_bytes
        if available <= 0:
            return 0
        return available // VideoKnowledgeCompiler._projected_map_output_byte_budget(
            request
        )

    @staticmethod
    def _projected_map_output_byte_budget(
        request: KnowledgeCompilationRequestV1,
    ) -> int:
        return (
            request.map_parser_limits.max_response_bytes
            + request.map_parser_limits.max_items
            * _KNOWLEDGE_ITEM_PROJECTION_BYTES
        )

    def _compose(
        self,
        request: KnowledgeCompilationRequestV1,
        context: VideoCompilationContext,
        plan: VideoCompilationPlanV1,
        *,
        input_kind: CoverageInputKind,
        input_ids: tuple[str, ...],
        items: tuple[KnowledgeItemV1, ...],
        segments: tuple[TranscriptSegment, ...],
    ):
        model_request = self._build_compose_request(
            request,
            input_kind=input_kind,
            input_ids=input_ids,
            items=items,
            segments=segments,
        )
        self._ensure_request_fits(plan, model_request)
        result = self._coordinator.execute(
            request.planning_request.model_binding,
            model_request,
            context.execution,
            "compose-final",
            context.cancellation_token,
        )
        composition = parse_composed_knowledge_draft(
            result.text,
            input_kind=input_kind,
            allowed_input_ids=input_ids,
            allowed_segment_ids=self._composition_segment_ids(
                request, input_kind=input_kind, items=items, segments=segments
            ),
            allow_screenshots=request.screenshot_policy is ScreenshotPolicy.ON_DEMAND,
            limits=request.composer_parser_limits,
        )
        return composition, result

    @staticmethod
    def _composition_segment_ids(
        request: KnowledgeCompilationRequestV1,
        *,
        input_kind: CoverageInputKind,
        items: tuple[KnowledgeItemV1, ...],
        segments: tuple[TranscriptSegment, ...],
    ) -> tuple[str, ...]:
        if input_kind is CoverageInputKind.SEGMENT:
            return tuple(segment.segment_id for segment in segments)
        supplied = {
            segment_id
            for item in items
            for segment_id in item.source_segment_ids
        }
        return tuple(
            segment.segment_id
            for segment in request.planning_request.transcript.segments
            if segment.segment_id in supplied
        )

    def _final_composition_fits(
        self,
        request: KnowledgeCompilationRequestV1,
        plan: VideoCompilationPlanV1,
        items: tuple[KnowledgeItemV1, ...],
    ) -> bool:
        if len(items) > request.composer_parser_limits.max_coverage_items:
            return False
        model_request = self._build_compose_request(
            request,
            input_kind=CoverageInputKind.KNOWLEDGE_ITEM,
            input_ids=tuple(item.knowledge_item_id for item in items),
            items=items,
            segments=(),
        )
        return self._request_fits(plan, model_request)

    @staticmethod
    def _build_compose_request(
        request: KnowledgeCompilationRequestV1,
        *,
        input_kind: CoverageInputKind,
        input_ids: tuple[str, ...],
        items: tuple[KnowledgeItemV1, ...],
        segments: tuple[TranscriptSegment, ...],
    ) -> ModelExecutionRequest:
        prompt = build_global_composer_prompt(
            input_kind=input_kind,
            input_ids=input_ids,
            source_title=request.source_title,
            output_language=request.output_language,
            style=request.style,
            screenshot_policy=request.screenshot_policy,
            items=items,
            segments=segments,
            limits=request.composer_parser_limits,
        )
        return VideoKnowledgeCompiler._model_request(
            request,
            stage_id="global-compose",
            stage_version=_COMPOSER_STAGE_VERSION,
            prompt_id="global-compose-balanced",
            prompt_version=_COMPOSER_PROMPT_VERSION,
            max_output_tokens=request.planning_request.reserved_output_tokens,
            prompt=prompt,
        )

    @staticmethod
    def _model_request(
        request: KnowledgeCompilationRequestV1,
        *,
        stage_id: str,
        stage_version: int,
        prompt_id: str,
        prompt_version: int,
        max_output_tokens: int,
        prompt: CompilationPrompt,
    ) -> ModelExecutionRequest:
        binding = request.planning_request.model_binding
        return ModelExecutionRequest(
            schema_version=1,
            stage_id=stage_id,
            stage_version=stage_version,
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            system_instruction=prompt.system_instruction,
            user_content=prompt.user_content,
            output_mode=ModelOutputMode.JSON_SCHEMA,
            max_output_tokens=max_output_tokens,
            timeout_seconds=binding.timeout_seconds,
            response_schema_json=prompt.response_schema_json,
            temperature=0 if binding.supports_temperature else None,
        )

    @staticmethod
    def _ensure_request_fits(
        plan: VideoCompilationPlanV1,
        request: ModelExecutionRequest,
    ) -> None:
        if not VideoKnowledgeCompiler._request_fits(plan, request):
            raise DomainError(
                "model_request_budget_exceeded",
                ErrorCategory.RECIPE_FAILED,
                "Knowledge compilation request exceeds the frozen safe input budget",
            )

    @staticmethod
    def _request_fits(
        plan: VideoCompilationPlanV1,
        request: ModelExecutionRequest,
    ) -> bool:
        encoded_bytes = VideoKnowledgeCompiler._encoded_request_bytes(request)
        return not (
            encoded_bytes > plan.chunk_input_token_budget
            or encoded_bytes > plan.chunk_input_byte_budget
        )

    @staticmethod
    def _encoded_request_bytes(request: ModelExecutionRequest) -> int:
        return sum(
            len(value.encode("utf-8"))
            for value in (
                request.system_instruction,
                request.user_content,
                request.response_schema_json or "",
            )
        )


__all__ = [
    "CompilationUsageV1",
    "CompiledVideoDocument",
    "DocumentCompilationSummaryV1",
    "KnowledgeCompilationRequestV1",
    "VideoCompilationContext",
    "VideoKnowledgeCompiler",
]

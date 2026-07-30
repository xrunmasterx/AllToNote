from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

import app.core.application.video_compiler as video_compiler_module
from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.adapters.models.model_result_store import ModelOperationResultStore
from app.core.application.model_call_coordinator import (
    ModelCallCoordinator,
    ModelCallExecution,
)
from app.core.application.video_compiler import (
    KnowledgeCompilationRequestV1,
    VideoCompilationContext,
    VideoKnowledgeCompiler,
)
from app.core.domain.video import (
    JobState,
    ScreenshotPolicy,
    TranscriptDocument,
    TranscriptSegment,
)
from app.core.errors import DomainError
from app.core.jobs.cancellation import CancellationToken
from app.core.ports.model_executor import (
    ModelExecutionBinding,
    ModelExecutionRequest,
    ModelExecutionResult,
    ModelFinishReason,
)
from app.core.recipes.video.compilation.contracts import (
    CompilationQualityProfile,
    CompilationTopology,
    ComposerParserLimitsV1,
    KnowledgeMapParserLimitsV1,
    TranscriptBasis,
    TranscriptQualityInputV1,
    VideoCompilationPlanningRequestV1,
)
from app.core.recipes.video.compilation.pipeline import assess_transcript_quality
from app.core.recipes.video.compilation.prompts import (
    build_knowledge_repair_prompt,
)


class _DeterministicExecutor:
    def __init__(self, *, forbidden: bool = False) -> None:
        self._forbidden = forbidden
        self._lock = threading.Lock()
        self.requests: list[ModelExecutionRequest] = []

    def complete(self, request: ModelExecutionRequest, token: object) -> ModelExecutionResult:
        if self._forbidden:
            raise AssertionError("recovery must not replay a successful model call")
        payload = json.loads(request.user_content)
        if request.stage_id == "knowledge-map":
            text = self._map_response(payload)
        elif request.stage_id == "knowledge-consolidate":
            text = self._consolidation_response(payload)
        elif request.stage_id == "global-compose":
            text = self._composition_response(payload)
        elif request.stage_id == "knowledge-text-repair":
            text = self._repair_response(payload)
        else:  # pragma: no cover - makes unexpected stage additions fail loudly
            raise AssertionError(request.stage_id)
        with self._lock:
            self.requests.append(request)
            request_ordinal = len(self.requests)
        return ModelExecutionResult(
            text=json.dumps(text, ensure_ascii=False),
            actual_model_identity="fixture/model-v1",
            input_tokens=100,
            output_tokens=20,
            finish_reason=ModelFinishReason.STOP,
            provider_request_id=f"req_{request_ordinal}",
            warnings=(),
        )

    @staticmethod
    def _map_response(payload: dict[str, object]) -> dict[str, object]:
        segments = payload["segments"]
        assert isinstance(segments, list) and segments
        first = segments[0]
        assert isinstance(first, dict)
        statement = " ".join(str(value["text"]) for value in segments)[:800]
        return {
            "schema_version": 1,
            "chunk_ordinal": payload["chunk_ordinal"],
            "items": [
                {
                    "item_ordinal": 0,
                    "kind": "claim",
                    "title": f"Chunk {payload['chunk_ordinal']}",
                    "statement": statement,
                    "importance": "core",
                    "source_segment_ids": [first["segment_id"]],
                }
            ],
            "term_candidates": [],
            "warnings": [],
        }

    @staticmethod
    def _consolidation_response(payload: dict[str, object]) -> dict[str, object]:
        items = payload["input_items"]
        assert isinstance(items, list) and len(items) >= 2
        source_ids = list(
            dict.fromkeys(
                segment_id
                for item in items
                for segment_id in item["source_segment_ids"]
            )
        )
        return {
            "schema_version": 1,
            "batch_ordinal": payload["batch_ordinal"],
            "items": [
                {
                    "item_ordinal": 0,
                    "kind": "claim",
                    "title": f"Consolidated {payload['batch_ordinal']}",
                    "statement": "Consolidated knowledge supported by the source.",
                    "importance": "core",
                    "source_segment_ids": source_ids,
                }
            ],
            "term_candidates": [],
            "warnings": [],
            "lineage": [
                {
                    "output_item_ordinal": 0,
                    "merged_from": [item["knowledge_item_id"] for item in items],
                }
            ],
            "omissions": [],
        }

    @staticmethod
    def _composition_response(payload: dict[str, object]) -> dict[str, object]:
        if "segments" in payload:
            segments = payload["segments"]
            segment_ids = [value["segment_id"] for value in segments]
        else:
            items = payload["knowledge_items"]
            segment_ids = list(
                dict.fromkeys(
                    segment_id
                    for item in items
                    for segment_id in item["source_segment_ids"]
                )
            )
        citation = segment_ids[0]
        return {
            "schema_version": 1,
            "markdown": (
                f"# One coherent article\n\n"
                f"## Main lesson\n\nA unified explanation.[^{citation}]"
            ),
            "covered_input_ids": payload["coverage_input_ids"],
            "omissions": [],
            "warnings": [],
        }

    @staticmethod
    def _repair_response(payload: dict[str, object]) -> dict[str, object]:
        raise AssertionError("fixture did not expect a knowledge-text-repair call")


class _KnowledgeQualityRepairExecutor(_DeterministicExecutor):
    def __init__(self, *, repair_mode: str = "pass") -> None:
        super().__init__()
        self._repair_mode = repair_mode

    @staticmethod
    def _invalid_markdown(citation: str) -> str:
        return (
            f"# First article\n\n"
            f"## Repeated lesson\n\nFirst explanation.[^{citation}]\n\n"
            f"# Second article\n\n"
            f"## Repeated lesson\n\nSecond explanation.[^{citation}]\n\n"
            f"# Third article\n\n"
            f"## Repeated lesson\n\nThird explanation.[^{citation}]"
        )

    @staticmethod
    def _composition_segment_ids(payload: dict[str, object]) -> list[str]:
        items = payload["knowledge_items"]
        assert isinstance(items, list) and items
        return list(
            dict.fromkeys(
                segment_id
                for item in items
                for segment_id in item["source_segment_ids"]
            )
        )

    def _composition_response(self, payload: dict[str, object]) -> dict[str, object]:
        segment_ids = self._composition_segment_ids(payload)
        return {
            "schema_version": 1,
            "markdown": self._invalid_markdown(segment_ids[0]),
            "covered_input_ids": payload["coverage_input_ids"],
            "omissions": [],
            "warnings": [],
        }

    def _repair_response(self, payload: dict[str, object]) -> dict[str, object]:
        allowed_segment_ids = payload["allowed_segment_ids"]
        covered_input_ids = payload["covered_input_ids"]
        omissions = payload["omissions"]
        assert isinstance(allowed_segment_ids, list) and allowed_segment_ids
        assert isinstance(covered_input_ids, list) and covered_input_ids
        assert isinstance(omissions, list)
        citation = str(allowed_segment_ids[0])
        markdown = (
            f"# One repaired article\n\n"
            f"## Main lesson\n\nA unified explanation.[^{citation}]"
        )
        if self._repair_mode == "still-invalid":
            markdown = self._invalid_markdown(citation)
        elif self._repair_mode == "expand-coverage":
            covered_input_ids = [*covered_input_ids, "ki_repair_foreign"]
        elif self._repair_mode == "expand-citations":
            markdown = (
                "# One repaired article\n\n"
                "## Main lesson\n\n"
                "An explanation from outside the frozen evidence set."
                "[^seg_999999]"
            )
        elif self._repair_mode == "replace-citations":
            citation = str(allowed_segment_ids[1])
            markdown = (
                f"# One repaired article\n\n"
                f"## Main lesson\n\nA different legal source.[^{citation}]"
            )
        elif self._repair_mode == "remove-citations":
            markdown = "# One repaired article\n\n## Main lesson\n\nNo source marker."
        elif self._repair_mode == "add-citations":
            second_citation = str(allowed_segment_ids[1])
            markdown = (
                f"# One repaired article\n\n"
                f"## Main lesson\n\nA broader legal source set."
                f"[^{citation}][^{second_citation}]"
            )
        elif self._repair_mode != "pass":  # pragma: no cover - fixture contract
            raise AssertionError(self._repair_mode)
        return {
            "schema_version": 1,
            "markdown": markdown,
            "covered_input_ids": covered_input_ids,
            "omissions": omissions,
            "warnings": [],
        }


class _CrossLineageCitationExecutor(_DeterministicExecutor):
    @staticmethod
    def _consolidation_response(payload: dict[str, object]) -> dict[str, object]:
        items = payload["input_items"]
        assert isinstance(items, list) and len(items) >= 2
        first = items[0]
        second = items[1]
        return {
            "schema_version": 1,
            "batch_ordinal": payload["batch_ordinal"],
            "items": [
                {
                    "item_ordinal": 0,
                    "kind": "claim",
                    "title": "Invalid cross-lineage merge",
                    "statement": "This output cites evidence outside its lineage.",
                    "importance": "core",
                    "source_segment_ids": list(second["source_segment_ids"]),
                }
            ],
            "term_candidates": [],
            "warnings": [],
            "lineage": [
                {
                    "output_item_ordinal": 0,
                    "merged_from": [first["knowledge_item_id"]],
                }
            ],
            "omissions": [
                {
                    "input_id": item["knowledge_item_id"],
                    "reason": "Fixture omission",
                }
                for item in items[1:]
            ],
        }


class _OmittingHierarchyExecutor(_DeterministicExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.root_input_ids: set[str] = set()
        self.expected_root_omissions: dict[str, str] = {}

    def _consolidation_response(
        self, payload: dict[str, object]
    ) -> dict[str, object]:
        items = payload["input_items"]
        assert isinstance(items, list) and len(items) >= 2
        for item in items:
            if str(item["title"]).startswith("Chunk "):
                self.root_input_ids.add(str(item["knowledge_item_id"]))
        merged = items[:-1]
        omitted = items[-1]
        omission_reason = f"low-confidence duplicate:{omitted['knowledge_item_id']}"
        if str(omitted["title"]).startswith("Chunk "):
            self.expected_root_omissions[str(omitted["knowledge_item_id"])] = (
                omission_reason
            )
        source_ids = list(
            dict.fromkeys(
                segment_id
                for item in merged
                for segment_id in item["source_segment_ids"]
            )
        )
        return {
            "schema_version": 1,
            "batch_ordinal": payload["batch_ordinal"],
            "items": [
                {
                    "item_ordinal": 0,
                    "kind": "claim",
                    "title": f"Consolidated {payload['batch_ordinal']}",
                    "statement": "Consolidated knowledge supported by the source.",
                    "importance": "core",
                    "source_segment_ids": source_ids,
                }
            ],
            "term_candidates": [],
            "warnings": [],
            "lineage": [
                {
                    "output_item_ordinal": 0,
                    "merged_from": [item["knowledge_item_id"] for item in merged],
                }
            ],
            "omissions": [
                {
                    "input_id": omitted["knowledge_item_id"],
                    "reason": omission_reason,
                }
            ],
        }


class _ForeignComposerCitationExecutor(_DeterministicExecutor):
    def __init__(self, foreign_segment_id: str) -> None:
        super().__init__()
        self._foreign_segment_id = foreign_segment_id

    def _composition_response(self, payload: dict[str, object]) -> dict[str, object]:
        items = payload["knowledge_items"]
        item_segment_ids = {
            segment_id
            for item in items
            for segment_id in item["source_segment_ids"]
        }
        assert self._foreign_segment_id not in item_segment_ids
        return {
            "schema_version": 1,
            "markdown": (
                "# One coherent article\n\n"
                "## Main lesson\n\n"
                f"Unsupported by the supplied items.[^{self._foreign_segment_id}]"
            ),
            "covered_input_ids": payload["coverage_input_ids"],
            "omissions": [],
            "warnings": [],
        }


class _ByteHeavyMapExecutor(_DeterministicExecutor):
    @staticmethod
    def _map_response(payload: dict[str, object]) -> dict[str, object]:
        segments = payload["segments"]
        assert isinstance(segments, list) and segments
        first = segments[0]
        return {
            "schema_version": 1,
            "chunk_ordinal": payload["chunk_ordinal"],
            "items": [
                {
                    "item_ordinal": ordinal,
                    "kind": "claim",
                    "title": f"{ordinal}-" + chr(0x5BBD) * 50,
                    "statement": chr(0x77E5) * 210,
                    "importance": "core",
                    "source_segment_ids": [first["segment_id"]],
                }
                for ordinal in range(3)
            ],
            "term_candidates": [],
            "warnings": [],
        }

    @staticmethod
    def _consolidation_response(payload: dict[str, object]) -> dict[str, object]:
        items = payload["input_items"]
        assert isinstance(items, list) and len(items) >= 2
        source_ids = list(
            dict.fromkeys(
                segment_id
                for item in items
                for segment_id in item["source_segment_ids"]
            )
        )
        return {
            "schema_version": 1,
            "batch_ordinal": payload["batch_ordinal"],
            "items": [
                {
                    "item_ordinal": 0,
                    "kind": "claim",
                    "title": f"Consolidated {payload['batch_ordinal']}",
                    "statement": "Consolidated knowledge supported by the source.",
                    "importance": "core",
                    "source_segment_ids": source_ids,
                }
            ],
            "term_candidates": [],
            "warnings": [],
            "lineage": [
                {
                    "output_item_ordinal": 0,
                    "merged_from": [item["knowledge_item_id"] for item in items],
                }
            ],
            "omissions": [],
        }

    @staticmethod
    def _composition_response(payload: dict[str, object]) -> dict[str, object]:
        if "segments" in payload:
            segments = payload["segments"]
            segment_ids = [value["segment_id"] for value in segments]
        else:
            items = payload["knowledge_items"]
            segment_ids = list(
                dict.fromkeys(
                    segment_id
                    for item in items
                    for segment_id in item["source_segment_ids"]
                )
            )
        citation = segment_ids[0]
        return {
            "schema_version": 1,
            "markdown": (
                f"# One coherent article\n\n"
                f"## Main lesson\n\nA unified explanation.[^{citation}]"
            ),
            "covered_input_ids": payload["coverage_input_ids"],
            "omissions": [],
            "warnings": [],
        }


def _transcript(count: int, *, text_size: int) -> TranscriptDocument:
    return TranscriptDocument(
        "en",
        tuple(
            TranscriptSegment(
                f"seg_{ordinal + 1:06d}",
                ordinal * 1_000,
                (ordinal + 1) * 1_000,
                f"lesson-{ordinal}-" + "x" * text_size,
            )
            for ordinal in range(count)
        ),
    )


def _binding(**changes: object) -> ModelExecutionBinding:
    value = ModelExecutionBinding(
        schema_version=1,
        provider_type="fixture",
        model_identity="fixture/model-v1",
        credential_profile_ref="fixture-profile",
        context_window_tokens=4_096,
        max_output_tokens=1_024,
        max_concurrency=4,
        supports_structured_output=True,
        supports_temperature=True,
        timeout_seconds=60,
    )
    return replace(value, **changes)


def _request(
    transcript: TranscriptDocument,
    *,
    binding: ModelExecutionBinding | None = None,
    max_request_bytes: int = 16_384,
    map_output_tokens: int = 256,
    map_output_bytes: int = 1_200,
) -> KnowledgeCompilationRequestV1:
    active_binding = binding or _binding()
    quality = assess_transcript_quality(
        TranscriptQualityInputV1(
            schema_version=1,
            transcript=transcript,
            transcript_basis=TranscriptBasis.PLATFORM_CAPTION,
            source_duration_ms=transcript.segments[-1].end_ms,
            detected_languages=("en",),
        )
    )
    planning = VideoCompilationPlanningRequestV1(
        schema_version=1,
        recipe_id="alltonote.video-course-note",
        recipe_version=2,
        quality_profile=CompilationQualityProfile.BALANCED,
        transcript=transcript,
        transcript_quality=quality,
        model_binding=active_binding,
        stage_id="knowledge-map",
        stage_version=1,
        prompt_id="knowledge-map-balanced",
        prompt_version=1,
        # Includes the frozen system instruction and JSON response schema. The
        # unknown-tokenizer path intentionally uses UTF-8 bytes as an upper bound.
        prompt_overhead_tokens=1_400,
        prompt_overhead_bytes=1_400,
        reserved_output_tokens=active_binding.max_output_tokens,
        max_request_bytes=max_request_bytes,
        max_chunk_duration_ms=10 * 60 * 1_000,
        estimated_map_output_tokens_per_chunk=map_output_tokens,
        map_output_byte_budget_per_chunk=map_output_bytes,
        max_repair_attempts=1,
    )
    return KnowledgeCompilationRequestV1(
        schema_version=1,
        planning_request=planning,
        source_title="Fixture lesson",
        output_language="en",
        style="structured",
        screenshot_policy=ScreenshotPolicy.OFF,
        map_parser_limits=KnowledgeMapParserLimitsV1(
            max_response_bytes=map_output_bytes,
            max_items=4,
            max_title_characters=100,
            max_statement_characters=2_048,
            max_segment_refs_per_item=64,
            max_term_candidates=16,
            max_term_characters=100,
            max_warnings=16,
            max_warning_characters=200,
        ),
        composer_parser_limits=ComposerParserLimitsV1(
            max_response_bytes=32_768,
            max_markdown_characters=16_384,
            max_coverage_items=128,
            max_omissions=128,
            max_omission_reason_characters=200,
            max_warnings=16,
            max_warning_characters=200,
        ),
    )


def _compiler_context(
    tmp_path: Path,
    executor: _DeterministicExecutor,
) -> tuple[VideoKnowledgeCompiler, VideoCompilationContext, SqliteJobRepository]:
    repository = SqliteJobRepository.open(tmp_path / "machine", clock=lambda: 1_000)
    job = repository.create_job(
        request_hash="sha256:" + "a" * 64,
        principal="local",
        client_request_id=None,
    )
    repository.transition_job(job.job_id, JobState.RUNNING)
    authority = repository.acquire_scheduler_lease("fixture", ttl_seconds=60)
    attempt = repository.start_attempt(
        repository.create_attempt(job.job_id, "knowledge-compile").attempt_id,
        authority,
    )
    execution = ModelCallExecution(
        job_id=job.job_id,
        step_id=attempt.step_id,
        attempt_id=attempt.attempt_id,
        authority=authority,
        heartbeat=lambda: repository.heartbeat_scheduler_lease(
            authority, ttl_seconds=60
        ),
    )
    coordinator = ModelCallCoordinator(
        operation_store=repository,
        result_store=ModelOperationResultStore(tmp_path / "results"),
        executor=executor,
    )
    return (
        VideoKnowledgeCompiler(coordinator),
        VideoCompilationContext(
            execution=execution,
            cancellation_token=CancellationToken(repository, job.job_id),
        ),
        repository,
    )


def test_repair_prompt_names_each_failed_substantive_h2_section() -> None:
    markdown = (
        "# Article\n\n"
        "## First uncited section\n\nFirst body.\n\n"
        "## Second uncited section\n\nSecond body.\n\n"
        "## Cited section\n\nCited body.[^seg_000001]"
    )
    prompt = build_knowledge_repair_prompt(
        markdown=markdown,
        allowed_segment_ids=("seg_000001",),
        covered_input_ids=("ki_1",),
        omissions=(),
        failed_checks=(
            {
                "check_id": "substantive_h2_citations",
                "line_numbers": list(range(3, 11)),
                "reason": "Each substantive H2 section must cite evidence",
                "related_ids": [],
            },
        ),
        limits=_request(_transcript(1, text_size=20)).composer_parser_limits,
    )

    payload = json.loads(prompt.user_content)

    assert payload["failed_h2_sections"] == [
        {
            "end_line": 6,
            "heading": "First uncited section",
            "start_line": 3,
        },
        {
            "end_line": 10,
            "heading": "Second uncited section",
            "start_line": 7,
        },
    ]
    assert "inside every listed failed H2 section" in prompt.system_instruction
    assert "write footnote-definition lines" in prompt.system_instruction
    assert "never put them in headings" in prompt.system_instruction


def test_direct_compiler_returns_one_article_in_one_model_wave(tmp_path: Path) -> None:
    executor = _DeterministicExecutor()
    compiler, context, _ = _compiler_context(tmp_path, executor)
    result = compiler.compile(_request(_transcript(2, text_size=20)), context)

    assert result.plan.topology is CompilationTopology.DIRECT
    assert result.model_identity == "fixture/model-v1"
    assert result.markdown.count("# One coherent article") == 1
    assert result.markdown.count("## Main lesson") == 1
    assert result.execution_summary.model_operation_count == 1
    assert result.execution_summary.sequential_model_waves == 1
    assert [request.stage_id for request in executor.requests] == ["global-compose"]
    assert "never write footnote-definition lines" in executor.requests[0].system_instruction
    assert "never headings" in executor.requests[0].system_instruction
    assert "possible ASR output" in executor.requests[0].system_instruction
    assert "even when evidence contains only the corrupted form" in (
        executor.requests[0].system_instruction
    )
    assert "do not preserve or caveat the corrupted spelling" in (
        executor.requests[0].system_instruction
    )
    assert "preserve the source wording and mark it as uncertain" in (
        executor.requests[0].system_instruction
    )


def test_compiler_behavior_identity_covers_consolidation_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = VideoKnowledgeCompiler.behavior_identity()

    monkeypatch.setattr(
        video_compiler_module,
        "_CONSOLIDATION_STAGE_VERSION",
        999,
    )

    assert VideoKnowledgeCompiler.behavior_identity() != before


def test_map_compose_uses_stable_chunk_ordinals_then_one_global_article(
    tmp_path: Path,
) -> None:
    executor = _DeterministicExecutor()
    compiler, context, _ = _compiler_context(tmp_path, executor)
    request = _request(
        _transcript(14, text_size=1_500),
        binding=_binding(
            context_window_tokens=12_288,
            max_output_tokens=1_024,
            max_concurrency=3,
        ),
        max_request_bytes=16_384,
        map_output_tokens=256,
    )
    result = compiler.compile(request, context)

    assert result.plan.topology is CompilationTopology.MAP_COMPOSE
    map_payloads = [
        json.loads(value.user_content)
        for value in executor.requests
        if value.stage_id == "knowledge-map"
    ]
    assert sorted(value["chunk_ordinal"] for value in map_payloads) == list(
        range(len(result.plan.transcript_chunks))
    )
    assert executor.requests[-1].stage_id == "global-compose"
    assert result.markdown.count("# One coherent article") == 1
    assert result.execution_summary.sequential_model_waves == 2
    assert result.execution_summary.model_operation_count == len(map_payloads) + 1
    map_requests = [
        value for value in executor.requests if value.stage_id == "knowledge-map"
    ]
    assert all(
        "Fix clear ASR name errors" in value.system_instruction
        for value in map_requests
    )
    assert all(
        "only ambiguous forms in term_candidates" in value.system_instruction
        for value in map_requests
    )


def test_map_response_schema_freezes_the_same_bounds_as_the_parser(
    tmp_path: Path,
) -> None:
    executor = _DeterministicExecutor()
    compiler, context, _ = _compiler_context(tmp_path, executor)
    request = _request(
        _transcript(14, text_size=1_500),
        binding=_binding(
            context_window_tokens=12_288,
            max_output_tokens=1_024,
        ),
        max_request_bytes=16_384,
        map_output_tokens=256,
    )

    compiler.compile(request, context)

    map_request = next(
        value for value in executor.requests if value.stage_id == "knowledge-map"
    )
    schema = json.loads(map_request.response_schema_json or "")
    properties = schema["properties"]
    item_properties = properties["items"]["items"]["properties"]
    assert properties["items"]["maxItems"] == request.map_parser_limits.max_items
    assert (
        item_properties["statement"]["maxLength"]
        == request.map_parser_limits.max_statement_characters
    )
    assert (
        item_properties["source_segment_ids"]["maxItems"]
        == request.map_parser_limits.max_segment_refs_per_item
    )
    assert (
        properties["term_candidates"]["maxItems"]
        == request.map_parser_limits.max_term_candidates
    )


def test_quality_gate_repairs_once_without_replaying_map_or_compose(
    tmp_path: Path,
) -> None:
    executor = _KnowledgeQualityRepairExecutor()
    compiler, context, _ = _compiler_context(tmp_path, executor)
    result = compiler.compile(
        _request(
            _transcript(14, text_size=1_500),
            binding=_binding(
                context_window_tokens=12_288,
                max_output_tokens=1_024,
                max_concurrency=3,
            ),
            max_request_bytes=16_384,
            map_output_tokens=256,
        ),
        context,
    )

    stage_ids = [request.stage_id for request in executor.requests]
    map_call_count = stage_ids.count("knowledge-map")
    assert result.plan.topology is CompilationTopology.MAP_COMPOSE
    assert map_call_count == len(result.plan.transcript_chunks)
    assert stage_ids.count("global-compose") == 1
    assert stage_ids.count("knowledge-text-repair") == 1
    assert stage_ids[-2:] == ["global-compose", "knowledge-text-repair"]
    assert result.markdown.count("# One repaired article") == 1
    assert "# First article" not in result.markdown
    assert result.execution_summary.model_operation_count == map_call_count + 2
    assert result.execution_summary.sequential_model_waves == 3


def test_quality_gate_fails_after_at_most_one_unsuccessful_repair(
    tmp_path: Path,
) -> None:
    executor = _KnowledgeQualityRepairExecutor(repair_mode="still-invalid")
    compiler, context, _ = _compiler_context(tmp_path, executor)

    with pytest.raises(DomainError, match="knowledge_note_quality_failed"):
        compiler.compile(
            _request(
                _transcript(14, text_size=1_500),
                binding=_binding(
                    context_window_tokens=12_288,
                    max_output_tokens=1_024,
                    max_concurrency=3,
                ),
                max_request_bytes=16_384,
                map_output_tokens=256,
            ),
            context,
        )

    stage_ids = [request.stage_id for request in executor.requests]
    assert stage_ids.count("global-compose") == 1
    assert stage_ids.count("knowledge-text-repair") == 1
    assert stage_ids[-1] == "knowledge-text-repair"


@pytest.mark.parametrize(
    ("repair_mode", "error_code"),
    (
        ("expand-coverage", "knowledge_coverage_invalid"),
        ("expand-citations", "model_citation_unknown"),
    ),
)
def test_quality_repair_cannot_expand_frozen_evidence_context(
    tmp_path: Path,
    repair_mode: str,
    error_code: str,
) -> None:
    executor = _KnowledgeQualityRepairExecutor(repair_mode=repair_mode)
    compiler, context, _ = _compiler_context(tmp_path, executor)

    with pytest.raises(DomainError, match=error_code):
        compiler.compile(
            _request(
                _transcript(14, text_size=1_500),
                binding=_binding(
                    context_window_tokens=12_288,
                    max_output_tokens=1_024,
                    max_concurrency=3,
                ),
                max_request_bytes=16_384,
                map_output_tokens=256,
            ),
            context,
        )

    stage_ids = [request.stage_id for request in executor.requests]
    assert stage_ids.count("global-compose") == 1
    assert stage_ids.count("knowledge-text-repair") == 1
    assert stage_ids[-1] == "knowledge-text-repair"


@pytest.mark.parametrize(
    ("repair_mode", "error_code"),
    (
        ("replace-citations", "knowledge_note_repair_context_changed"),
        ("remove-citations", "model_citation_missing"),
        ("add-citations", "knowledge_note_repair_context_changed"),
    ),
)
def test_quality_repair_must_preserve_exact_original_citation_set(
    tmp_path: Path,
    repair_mode: str,
    error_code: str,
) -> None:
    executor = _KnowledgeQualityRepairExecutor(repair_mode=repair_mode)
    compiler, context, _ = _compiler_context(tmp_path, executor)

    with pytest.raises(DomainError, match=error_code):
        compiler.compile(
            _request(
                _transcript(14, text_size=1_500),
                binding=_binding(
                    context_window_tokens=12_288,
                    max_output_tokens=1_024,
                    max_concurrency=3,
                ),
                max_request_bytes=16_384,
                map_output_tokens=256,
            ),
            context,
        )

    stage_ids = [request.stage_id for request in executor.requests]
    assert stage_ids.count("knowledge-text-repair") == 1


def test_hierarchical_compose_makes_progress_and_preserves_final_coverage(
    tmp_path: Path,
) -> None:
    executor = _DeterministicExecutor()
    compiler, context, _ = _compiler_context(tmp_path, executor)
    request = _request(
        _transcript(100, text_size=1_500),
        binding=_binding(
            context_window_tokens=8_192,
            max_output_tokens=1_024,
            max_concurrency=4,
        ),
        max_request_bytes=16_384,
        map_output_tokens=512,
    )
    result = compiler.compile(request, context)

    assert result.plan.topology is CompilationTopology.HIERARCHICAL_COMPOSE
    consolidation_requests = [
        value for value in executor.requests if value.stage_id == "knowledge-consolidate"
    ]
    assert consolidation_requests
    assert result.execution_summary.sequential_model_waves > 2
    assert result.execution_summary.knowledge_item_count == len(
        result.plan.transcript_chunks
    )
    assert result.coverage.covered_input_ids
    assert not result.coverage.omissions
    assert result.markdown.count("# One coherent article") == 1


def test_successful_compilation_recovers_without_replaying_provider(
    tmp_path: Path,
) -> None:
    executor = _DeterministicExecutor()
    compiler, context, repository = _compiler_context(tmp_path, executor)
    request = _request(
        _transcript(12, text_size=1_500),
        binding=_binding(context_window_tokens=12_288, max_output_tokens=1_024),
        max_request_bytes=16_384,
        map_output_tokens=256,
    )
    first = compiler.compile(request, context)
    paid_call_count = len(executor.requests)

    forbidden = _DeterministicExecutor(forbidden=True)
    recovered = VideoKnowledgeCompiler(
        ModelCallCoordinator(
            operation_store=repository,
            result_store=ModelOperationResultStore(tmp_path / "results"),
            executor=forbidden,
        )
    ).compile(request, context)

    assert recovered == first
    assert paid_call_count > 1
    assert forbidden.requests == []


def test_consolidation_rejects_evidence_outside_merged_lineage(
    tmp_path: Path,
) -> None:
    executor = _CrossLineageCitationExecutor()
    compiler, context, _ = _compiler_context(tmp_path, executor)
    request = _request(
        _transcript(100, text_size=1_500),
        binding=_binding(context_window_tokens=8_192, max_output_tokens=1_024),
        map_output_tokens=512,
    )

    with pytest.raises(
        DomainError, match="knowledge_consolidation_evidence_invalid"
    ):
        compiler.compile(request, context)


def test_hierarchy_coverage_closes_over_original_items_and_keeps_reasons(
    tmp_path: Path,
) -> None:
    executor = _OmittingHierarchyExecutor()
    compiler, context, _ = _compiler_context(tmp_path, executor)
    result = compiler.compile(
        _request(
            _transcript(100, text_size=1_500),
            binding=_binding(context_window_tokens=8_192, max_output_tokens=1_024),
            map_output_tokens=512,
        ),
        context,
    )

    omissions = {
        omission.input_id: omission.reason for omission in result.coverage.omissions
    }
    all_root_ids = set(result.coverage.covered_input_ids) | set(omissions)
    assert len(all_root_ids) == result.execution_summary.knowledge_item_count
    assert set(executor.expected_root_omissions).issubset(omissions)
    assert all(
        omissions[item_id] == reason
        for item_id, reason in executor.expected_root_omissions.items()
    )


def test_map_composer_cannot_cite_transcript_segment_absent_from_items(
    tmp_path: Path,
) -> None:
    executor = _ForeignComposerCitationExecutor("seg_000002")
    compiler, context, _ = _compiler_context(tmp_path, executor)
    request = _request(
        _transcript(14, text_size=1_500),
        binding=_binding(context_window_tokens=12_288, max_output_tokens=1_024),
        map_output_tokens=256,
    )

    with pytest.raises(DomainError, match="model_citation_unknown"):
        compiler.compile(request, context)


def test_direct_over_coverage_capacity_switches_before_first_paid_call(
    tmp_path: Path,
) -> None:
    executor = _DeterministicExecutor()
    compiler, context, _ = _compiler_context(tmp_path, executor)
    request = _request(_transcript(4, text_size=20))
    request = replace(
        request,
        composer_parser_limits=replace(
            request.composer_parser_limits,
            max_coverage_items=1,
            max_omissions=1,
        ),
    )

    with pytest.raises(DomainError, match="model_context_insufficient"):
        compiler.compile(request, context)

    assert executor.requests == []


def test_byte_budget_freezes_hierarchy_before_map_calls(
    tmp_path: Path,
) -> None:
    executor = _ByteHeavyMapExecutor()
    compiler, context, _ = _compiler_context(tmp_path, executor)
    result = compiler.compile(
        _request(
            _transcript(14, text_size=1_500),
            binding=_binding(context_window_tokens=12_288, max_output_tokens=1_024),
            map_output_tokens=256,
            map_output_bytes=3_000,
        ),
        context,
    )

    assert result.plan.topology is CompilationTopology.HIERARCHICAL_COMPOSE
    assert any(
        request.stage_id == "knowledge-consolidate"
        for request in executor.requests
    )


def test_map_output_byte_budget_is_a_hard_parser_bound() -> None:
    request = _request(_transcript(2, text_size=20), map_output_bytes=2_048)

    with pytest.raises(
        DomainError, match="knowledge_compilation_contract_invalid"
    ):
        replace(
            request,
            planning_request=replace(
                request.planning_request,
                map_output_byte_budget_per_chunk=1_024,
            ),
        )


def test_all_map_prompts_are_preflighted_before_any_paid_call(
    tmp_path: Path,
) -> None:
    executor = _DeterministicExecutor()
    compiler, context, _ = _compiler_context(tmp_path, executor)
    request = replace(
        _request(
            _transcript(14, text_size=1_500),
            binding=_binding(context_window_tokens=12_288, max_output_tokens=1_024),
        ),
        source_title="x" * 20_000,
    )

    with pytest.raises(DomainError, match="model_request_budget_exceeded"):
        compiler.compile(request, context)

    assert executor.requests == []

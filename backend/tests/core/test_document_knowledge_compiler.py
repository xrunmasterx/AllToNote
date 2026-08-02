from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.adapters.models.legacy_gpt import LegacyModelResponse
from app.adapters.models.legacy_model_executor import LegacyModelExecutor
from app.adapters.models.model_result_store import ModelOperationResultStore
from app.core.application.document_knowledge_compiler import (
    CompiledDocumentKnowledgeNoteV1,
    DocumentCompilationContext,
    DocumentKnowledgeCompilationRequestV1,
    DocumentKnowledgeCompiler,
)
from app.core.application.document_knowledge_verifier import (
    DocumentKnowledgeVerificationRequestV1,
    DocumentKnowledgeVerifier,
    compiled_document_knowledge_sha256,
    document_knowledge_evidence_sha256,
)
from app.core.application.model_call_coordinator import (
    ModelCallCoordinator,
    ModelCallExecution,
)
from app.core.domain.document import (
    DocumentBlock,
    DocumentBoundingBox,
    DocumentPage,
    ParsedDocument,
)
from app.core.domain.video import JobState
from app.core.errors import DomainError
from app.core.jobs.cancellation import CancellationToken
from app.core.ports.model_executor import (
    ModelExecutionBinding,
    ModelExecutionRequest,
    ModelExecutionResult,
    ModelFinishReason,
)


def _document(*, body_size: int = 32) -> ParsedDocument:
    values = (
        ("blk_title", "title", "A useful paper"),
        ("blk_problem", "paragraph", "P" * body_size),
        ("blk_method", "paragraph", "M" * body_size),
    )
    blocks = tuple(
        DocumentBlock(
            block_id,
            1,
            index,
            kind,
            text,
            "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
            DocumentBoundingBox(10, 10 + index * 20, 200, 25 + index * 20),
        )
        for index, (block_id, kind, text) in enumerate(values)
    )
    return ParsedDocument(
        source_sha256="sha256:" + "a" * 64,
        source_name="paper.pdf",
        parser_id="docling",
        parser_version="2.117.0",
        model_revision="fixture",
        pages=(DocumentPage(1, 612, 792, blocks),),
        metadata={"status": "success"},
        warnings=(),
    )


def _binding() -> ModelExecutionBinding:
    return ModelExecutionBinding(
        schema_version=1,
        provider_type="fixture",
        model_identity="fixture/model-v1",
        credential_profile_ref="fixture/credential",
        context_window_tokens=128_000,
        max_output_tokens=4_096,
        max_concurrency=1,
        supports_structured_output=True,
        supports_temperature=True,
        timeout_seconds=60,
    )


class _Executor:
    def __init__(
        self,
        response: str,
        *,
        finish_reason: ModelFinishReason = ModelFinishReason.STOP,
        warnings: tuple[str, ...] = (),
    ) -> None:
        self.response = response
        self.finish_reason = finish_reason
        self.warnings = warnings
        self.requests: list[ModelExecutionRequest] = []

    def complete(self, request: ModelExecutionRequest, token) -> ModelExecutionResult:
        token.raise_if_cancelled()
        self.requests.append(request)
        return ModelExecutionResult(
            text=self.response,
            actual_model_identity="fixture/model-v1",
            input_tokens=120,
            output_tokens=80,
            finish_reason=self.finish_reason,
            provider_request_id="fixture-request",
            warnings=self.warnings,
        )


class _SequentialExecutor(_Executor):
    def __init__(self, responses: tuple[str, ...]) -> None:
        super().__init__(responses[0])
        self._responses = list(responses)

    def complete(self, request: ModelExecutionRequest, token) -> ModelExecutionResult:
        self.response = self._responses.pop(0)
        return super().complete(request, token)


class _LegacyBridge:
    def __init__(self, response: str) -> None:
        self.response = response

    def complete_request(
        self,
        prompt: str,
        request: ModelExecutionRequest,
        *,
        check_cancelled=None,
    ) -> LegacyModelResponse:
        if check_cancelled is not None:
            check_cancelled()
        return LegacyModelResponse(
            markdown=self.response,
            provider_request_id="fixture-request",
            input_tokens=120,
            output_tokens=80,
            actual_model="fixture/model-v1",
        )


def _model_harness(
    tmp_path: Path,
    executor,
) -> tuple[ModelCallCoordinator, DocumentCompilationContext]:
    repository = SqliteJobRepository.open(tmp_path / "machine", clock=lambda: 1_000)
    job = repository.create_job(
        request_hash="sha256:" + "b" * 64,
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
            "compile_document_note",
            authority=authority,
        ).attempt_id,
        authority,
    )
    coordinator = ModelCallCoordinator(
        operation_store=repository,
        result_store=ModelOperationResultStore(tmp_path / "results"),
        executor=executor,
    )
    return (
        coordinator,
        DocumentCompilationContext(
            execution=ModelCallExecution(
                job_id=job.job_id,
                step_id=attempt.step_id,
                attempt_id=attempt.attempt_id,
                authority=authority,
                heartbeat=lambda: repository.heartbeat_job_claim(
                    authority, ttl_seconds=60
                ),
            ),
            cancellation_token=CancellationToken(repository, job.job_id),
        ),
    )


def _compilation_harness(
    tmp_path: Path,
    executor,
) -> tuple[DocumentKnowledgeCompiler, DocumentCompilationContext]:
    coordinator, context = _model_harness(tmp_path, executor)
    return DocumentKnowledgeCompiler(coordinator), context


def _compiler(
    tmp_path: Path,
    response: object,
) -> tuple[DocumentKnowledgeCompiler, DocumentCompilationContext, _Executor]:
    text = response if isinstance(response, str) else json.dumps(response)
    executor = _Executor(text)
    compiler, context = _compilation_harness(tmp_path, executor)
    return (
        compiler,
        context,
        executor,
    )


def _response() -> dict[str, object]:
    return {
        "title": {
            "text": "A useful paper",
            "source_block_ids": ["blk_title"],
        },
        "overview": [
            {
                "text": "The paper frames a concrete problem.",
                "source_block_ids": ["blk_problem"],
            }
        ],
        "sections": [
            {
                "heading": {
                    "text": "Method",
                    "source_block_ids": ["blk_method"],
                },
                "paragraphs": [
                    {
                        "text": "The method follows from the stated problem.",
                        "source_block_ids": ["blk_problem", "blk_method"],
                    }
                ],
                "key_points": [
                    {
                        "text": "The method is the main contribution.",
                        "source_block_ids": ["blk_method"],
                    }
                ],
            }
        ],
    }


def _compiled_note(tmp_path: Path) -> CompiledDocumentKnowledgeNoteV1:
    compiler, context, _executor = _compiler(tmp_path, _response())
    return compiler.compile(
        DocumentKnowledgeCompilationRequestV1(
            schema_version=1,
            parsed=_document(),
            output_language="en",
            model_binding=_binding(),
        ),
        context,
    )


def test_document_verification_evidence_digest_binds_actual_block_text(
    tmp_path: Path,
) -> None:
    parsed = _document()
    compiled = _compiled_note(tmp_path)
    changed_block = replace(parsed.pages[0].blocks[1], text="changed evidence")
    changed_page = replace(
        parsed.pages[0],
        blocks=(
            parsed.pages[0].blocks[0],
            changed_block,
            parsed.pages[0].blocks[2],
        ),
    )
    changed = replace(parsed, pages=(changed_page,))

    assert changed_block.content_sha256 == parsed.pages[0].blocks[1].content_sha256
    assert document_knowledge_evidence_sha256(
        parsed, compiled
    ) != document_knowledge_evidence_sha256(changed, compiled)


def test_document_compiler_returns_semantic_note_with_separate_evidence(
    tmp_path: Path,
) -> None:
    compiler, context, executor = _compiler(tmp_path, _response())

    result = compiler.compile(
        DocumentKnowledgeCompilationRequestV1(
            schema_version=1,
            parsed=_document(),
            output_language="en",
            model_binding=_binding(),
        ),
        context,
    )

    assert result.title.text == "A useful paper"
    assert result.sections[0].heading.text == "Method"
    assert result.referenced_block_ids == (
        "blk_title",
        "blk_problem",
        "blk_method",
    )
    assert result.model_identity == "fixture/model-v1"
    assert result.input_tokens == 120
    assert result.output_tokens == 80
    assert len(executor.requests) == 1
    assert executor.requests[0].stage_id == "document-knowledge-compose"
    assert executor.requests[0].prompt_version == 2
    payload = json.loads(executor.requests[0].user_content)
    assert [block["block_id"] for block in payload["source_blocks"]] == [
        "blk_title",
        "blk_problem",
        "blk_method",
    ]
    assert "untrusted source data" in executor.requests[0].system_instruction
    instruction = executor.requests[0].system_instruction.lower()
    assert "every material detail" in instruction and "cited blocks" in instruction
    assert "[^blk_" not in json.dumps(result.to_dict())
    assert "uniqueItems" not in executor.requests[0].response_schema_json
    schema = json.loads(executor.requests[0].response_schema_json)
    evidence_items = schema["properties"]["title"]["properties"][
        "source_block_ids"
    ]["items"]
    assert evidence_items["enum"] == [
        "blk_title",
        "blk_problem",
        "blk_method",
    ]


def test_document_compiler_accepts_success_from_production_legacy_executor(
    tmp_path: Path,
) -> None:
    binding = _binding()
    executor = LegacyModelExecutor(
        binding=binding,
        bridge=_LegacyBridge(json.dumps(_response())),
    )
    compiler, context = _compilation_harness(tmp_path, executor)

    result = compiler.compile(
        DocumentKnowledgeCompilationRequestV1(
            schema_version=1,
            parsed=_document(),
            output_language="en",
            model_binding=binding,
        ),
        context,
    )

    assert result.title.text == "A useful paper"
    assert result.model_identity == "fixture/model-v1"


def test_document_compiler_requires_evidence_for_title_and_heading(
    tmp_path: Path,
) -> None:
    response = {
        **_response(),
        "title": {
            "text": "A useful paper",
            "source_block_ids": ["blk_title"],
        },
        "sections": [
            {
                **_response()["sections"][0],
                "heading": {
                    "text": "Method",
                    "source_block_ids": ["blk_method"],
                },
            }
        ],
    }
    compiler, context, _ = _compiler(tmp_path, response)

    result = compiler.compile(
        DocumentKnowledgeCompilationRequestV1(
            schema_version=1,
            parsed=_document(),
            output_language="en",
            model_binding=_binding(),
        ),
        context,
    )

    assert result.title.text == "A useful paper"
    assert result.title.source_block_ids == ("blk_title",)
    assert result.sections[0].heading.source_block_ids == ("blk_method",)
    assert result.referenced_block_ids == (
        "blk_title",
        "blk_problem",
        "blk_method",
    )


def test_document_compiler_rejects_unknown_finish_reason_without_legacy_signal(
    tmp_path: Path,
) -> None:
    executor = _Executor(
        json.dumps(_response()),
        finish_reason=ModelFinishReason.UNKNOWN,
    )
    compiler, context = _compilation_harness(tmp_path, executor)

    with pytest.raises(DomainError) as caught:
        compiler.compile(
            DocumentKnowledgeCompilationRequestV1(
                schema_version=1,
                parsed=_document(),
                output_language="en",
                model_binding=_binding(),
            ),
            context,
        )

    assert caught.value.code == "document_knowledge_response_invalid"


@pytest.mark.parametrize(
    "response",
    (
        {
            **_response(),
            "overview": [{"text": "Unsupported.", "source_block_ids": ["blk_missing"]}],
        },
        {
            **_response(),
            "sections": [
                {
                    "heading": "Method",
                    "paragraphs": [
                        {"text": "No evidence.", "source_block_ids": []}
                    ],
                    "key_points": [],
                }
            ],
        },
        {
            **_response(),
            "title": {
                "text": "Repeated evidence.",
                "source_block_ids": ["blk_title", "blk_title"],
            },
        },
        '{"title":"First","title":"Second","overview":[],"sections":[]}',
    ),
)
def test_document_compiler_rejects_unverifiable_model_output(
    tmp_path: Path,
    response: object,
) -> None:
    compiler, context, _ = _compiler(tmp_path, response)

    with pytest.raises(DomainError) as caught:
        compiler.compile(
            DocumentKnowledgeCompilationRequestV1(
                schema_version=1,
                parsed=_document(),
                output_language="en",
                model_binding=_binding(),
            ),
            context,
        )

    assert caught.value.code == "document_knowledge_response_invalid"


def test_document_compiler_fails_before_model_call_when_source_is_too_large(
    tmp_path: Path,
) -> None:
    compiler, context, executor = _compiler(tmp_path, _response())

    with pytest.raises(DomainError) as caught:
        compiler.compile(
            DocumentKnowledgeCompilationRequestV1(
                schema_version=1,
                parsed=_document(body_size=256),
                output_language="en",
                model_binding=_binding(),
                max_source_bytes=128,
            ),
            context,
        )

    assert caught.value.code == "document_long_compilation_required"
    assert executor.requests == []


def _verification_response(status: str = "supported") -> dict[str, object]:
    return {
        "claims": [
            {"claim_id": claim_id, "status": status}
            for claim_id in (
                "title-0001",
                "overview-0001",
                "section-0001-heading-0001",
                "section-0001-paragraph-0001",
                "section-0001-key-point-0001",
            )
        ]
    }


def _verifier(
    tmp_path: Path,
    response: object,
) -> tuple[DocumentKnowledgeVerifier, DocumentCompilationContext, _Executor]:
    text = response if isinstance(response, str) else json.dumps(response)
    executor = _Executor(text)
    coordinator, context = _model_harness(tmp_path, executor)
    return DocumentKnowledgeVerifier(coordinator), context, executor


def test_document_verifier_checks_every_claim_against_only_its_cited_blocks(
    tmp_path: Path,
) -> None:
    compiled = _compiled_note(tmp_path / "compile")
    verifier, context, executor = _verifier(
        tmp_path / "verify",
        _verification_response(),
    )

    result = verifier.verify(
        DocumentKnowledgeVerificationRequestV1(
            schema_version=1,
            parsed=_document(),
            compiled=compiled,
            model_binding=_binding(),
        ),
        context,
    )

    assert result.passed is True
    assert result.compiled_sha256 == compiled_document_knowledge_sha256(compiled)
    assert len(result.claims) == 5
    assert len(executor.requests) == 1
    assert executor.requests[0].stage_id == "document-knowledge-verify"
    assert executor.requests[0].prompt_version == 2
    payload = json.loads(executor.requests[0].user_content)
    overview = next(
        claim for claim in payload["claims"] if claim["claim_id"] == "overview-0001"
    )
    title = next(
        claim for claim in payload["claims"] if claim["claim_id"] == "title-0001"
    )
    heading = next(
        claim
        for claim in payload["claims"]
        if claim["claim_id"] == "section-0001-heading-0001"
    )
    paragraph = next(
        claim
        for claim in payload["claims"]
        if claim["claim_id"] == "section-0001-paragraph-0001"
    )
    key_point = next(
        claim
        for claim in payload["claims"]
        if claim["claim_id"] == "section-0001-key-point-0001"
    )
    assert overview["cited_sources"] == [
        {"block_id": "blk_problem", "text": "P" * 32}
    ]
    assert title["claim_role"] == "structural-label"
    assert heading["claim_role"] == "structural-label"
    assert overview["claim_role"] == "factual"
    assert paragraph["claim_role"] == "factual"
    assert key_point["claim_role"] == "factual"
    assert "only against its cited source" in executor.requests[0].system_instruction
    assert (
        "structural-label" in executor.requests[0].system_instruction
        and "no new factual detail" in executor.requests[0].system_instruction
    )


def test_document_verifier_fails_closed_when_any_claim_is_not_supported(
    tmp_path: Path,
) -> None:
    compiled = _compiled_note(tmp_path / "compile")
    response = _verification_response()
    response["claims"][3]["status"] = "insufficient-evidence"  # type: ignore[index]
    verifier, context, _executor = _verifier(tmp_path / "verify", response)

    result = verifier.verify(
        DocumentKnowledgeVerificationRequestV1(
            schema_version=1,
            parsed=_document(),
            compiled=compiled,
            model_binding=_binding(),
        ),
        context,
    )

    assert result.passed is False
    assert result.claims[3].status == "insufficient-evidence"


@pytest.mark.parametrize(
    "claims",
    (
        _verification_response()["claims"][:-1],
        [
            *_verification_response()["claims"][:-1],
            {"claim_id": "title-0001", "status": "supported"},
        ],
        [
            *_verification_response()["claims"][:-1],
            {"claim_id": "unknown", "status": "supported"},
        ],
    ),
)
def test_document_verifier_rejects_missing_duplicate_or_unknown_claims(
    tmp_path: Path,
    claims: object,
) -> None:
    compiled = _compiled_note(tmp_path / "compile")
    verifier, context, _executor = _verifier(
        tmp_path / "verify",
        {"claims": claims},
    )

    with pytest.raises(DomainError) as caught:
        verifier.verify(
            DocumentKnowledgeVerificationRequestV1(
                schema_version=1,
                parsed=_document(),
                compiled=compiled,
                model_binding=_binding(),
            ),
            context,
        )

    assert caught.value.code == "document_knowledge_verification_response_invalid"


def test_document_compose_and_verify_recover_without_repeating_paid_calls(
    tmp_path: Path,
) -> None:
    executor = _SequentialExecutor(
        (
            json.dumps(_response()),
            json.dumps(_verification_response()),
        )
    )
    coordinator, context = _model_harness(tmp_path, executor)
    compiler = DocumentKnowledgeCompiler(coordinator)
    verifier = DocumentKnowledgeVerifier(coordinator)
    compilation_request = DocumentKnowledgeCompilationRequestV1(
        schema_version=1,
        parsed=_document(),
        output_language="en",
        model_binding=_binding(),
    )

    compiled = compiler.compile(compilation_request, context)
    verification_request = DocumentKnowledgeVerificationRequestV1(
        schema_version=1,
        parsed=_document(),
        compiled=compiled,
        model_binding=_binding(),
    )
    verified = verifier.verify(verification_request, context)
    recovered_compiled = compiler.compile(compilation_request, context)
    recovered_verified = verifier.verify(verification_request, context)

    assert verified.passed is True
    assert recovered_compiled == compiled
    assert recovered_verified == verified
    assert [request.stage_id for request in executor.requests] == [
        "document-knowledge-compose",
        "document-knowledge-verify",
    ]

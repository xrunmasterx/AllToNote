from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import shutil
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

import app.runtime as runtime_module
import app.core.application.document_service as document_service_module
from app.job_runtime import JobRuntime
from app.adapters.documents.document_basic_pack import (
    PACK_ID,
    PACK_VERSION,
    current_document_pack_platform,
)
from app.adapters.models.legacy_gpt import (
    LegacyModelBinding,
    LegacyModelCapabilities,
    LegacyModelResponse,
)
from app.adapters.iwiki.portable_gateway import IWikiPortableGateway
from app.adapters.jobs.file_attempt_storage import FileAttemptStorage
from app.adapters.jobs.machine_resource_lease import MachineResourceLeaseStore
from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.adapters.sources.legacy_video import VerifiedSourceIdentityRegistry
from app.core.application.document_checkpoints import DocumentCandidateCheckpoint
from app.core.application.document_knowledge_compiler import (
    CompiledDocumentKnowledgeNoteV1,
    DocumentKnowledgeClaimV1,
    DocumentKnowledgeSectionV1,
)
from app.core.application.document_service import (
    CHECKPOINT_SCHEMA,
    DocumentKnowledgeCompilationInput,
    DocumentKnowledgeVerificationInput,
    DocumentService,
)
from app.core.application.document_knowledge_verifier import (
    DocumentKnowledgeClaimVerificationV1,
    DocumentKnowledgeVerificationV1,
    compiled_document_knowledge_sha256,
    document_knowledge_evidence_sha256,
    document_knowledge_claims,
)
from app.core.application.job_execution_router import JobExecutionRouter
from app.core.application.job_service import JobService
from app.core.application.produce_service import ProduceService
from app.core.domain.document import (
    DocumentBlock,
    DocumentBoundingBox,
    DocumentPage,
    DocumentKnowledgeProduceRequest,
    DocumentProduceRequest,
    ParsedDocument,
)
from app.core.domain.ids import new_typed_id, sha256_digest
from app.core.domain.production import RecipeProduceResult
from app.core.domain.video import RetryJobRequest, VideoProduceRequest
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.external_operation import ExternalOperationGuard
from app.core.jobs.model import AttemptState, JobExecutionOwner, JobState
from app.core.jobs.resource_lease import (
    JobExecutionAuthority,
    ResourceLease,
    ResourceOwner,
)
from app.core.packs.events import ExecutionPackIdentity, JobPackEnvironmentSnapshot
from app.core.ports.jobs import SourceIdentityBinding
from app.core.ports.model_executor import ModelExecutionBinding
from app.core.recipes.contracts import InputDescriptor, ProduceRequest, RecipeKey
from app.core.recipes.document.adapter import DocumentRecipeAdapter
from app.core.recipes.document.descriptor import DOCUMENT_NOTE_V1
from app.core.recipes.registry import RecipeRegistry


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "workspace-v2"
_DOCUMENT_PACK_ENVIRONMENT = JobPackEnvironmentSnapshot(
    schema_version=1,
    packs=(
        ExecutionPackIdentity(
            pack_id=PACK_ID,
            pack_version=PACK_VERSION,
            platform=current_document_pack_platform(),
            manifest_sha256="sha256:" + "a" * 64,
        ),
    ),
)


class _Parser:
    def __init__(self) -> None:
        self.calls = 0

    def parse(
        self,
        source: Path,
        *,
        work_root: Path,
        cancellation_token,
    ) -> ParsedDocument:
        cancellation_token.raise_if_cancelled()
        del work_root
        self.calls += 1
        raw = source.read_bytes()
        text = "Real-Time Rendering of Glossy Reflections"
        return ParsedDocument(
            source_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
            source_name=source.name,
            parser_id="docling",
            parser_version="2.117.0",
            model_revision="8f39ad3c0b4c58e9c2d2c84a38465abf757272d8",
            pages=(
                DocumentPage(
                    1,
                    612.0,
                    792.0,
                    (
                        DocumentBlock(
                            "blk_title",
                            1,
                            0,
                            "section_header",
                            text,
                            "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
                            DocumentBoundingBox(50, 50, 500, 80),
                        ),
                    ),
                ),
            ),
            metadata={"status": "success"},
            warnings=(),
        )


class _RuntimeParser(_Parser):
    def doctor(self) -> None:
        return None


class _StageBridge:
    def __init__(self, model_identity: str, stage_id: str, response: object) -> None:
        self.model_identity = model_identity
        self.stage_id = stage_id
        self.response = response
        self.calls: list[str] = []

    def complete_request(
        self,
        prompt: str,
        request,
        *,
        check_cancelled=None,
    ) -> LegacyModelResponse:
        del prompt
        if check_cancelled is not None:
            check_cancelled()
        assert request.stage_id == self.stage_id
        self.calls.append(request.stage_id)
        return LegacyModelResponse(
            markdown=json.dumps(self.response),
            provider_request_id=f"{self.stage_id}-request",
            input_tokens=10,
            output_tokens=10,
            actual_model=self.model_identity,
        )


class _RoutingStageBridge:
    def __init__(self, model_identity: str, responses: dict[str, object]) -> None:
        self.model_identity = model_identity
        self.responses = responses
        self.calls: list[str] = []

    def complete_request(
        self,
        prompt: str,
        request,
        *,
        check_cancelled=None,
    ) -> LegacyModelResponse:
        del prompt
        if check_cancelled is not None:
            check_cancelled()
        response = self.responses[request.stage_id]
        self.calls.append(request.stage_id)
        return LegacyModelResponse(
            markdown=json.dumps(response),
            provider_request_id=f"{request.stage_id}-request",
            input_tokens=10,
            output_tokens=10,
            actual_model=self.model_identity,
        )


class _ForbiddenStageBridge:
    def __init__(self, model_identity: str) -> None:
        self.model_identity = model_identity
        self.calls = 0

    def complete_request(
        self,
        prompt: str,
        request,
        *,
        check_cancelled=None,
    ) -> LegacyModelResponse:
        del prompt, request, check_cancelled
        self.calls += 1
        raise AssertionError("successful model operations must not be replayed")


class _TerminateAfterVerificationAssembler:
    def assemble(self, *args, **kwargs) -> None:
        del args, kwargs
        os._exit(23)


def _compiled_stage_response() -> dict[str, object]:
    return {
        "title": {
            "text": "Compiled paper note",
            "source_block_ids": ["blk_title"],
        },
        "overview": [
            {
                "text": "The paper studies glossy reflections.",
                "source_block_ids": ["blk_title"],
            }
        ],
        "sections": [
            {
                "heading": {
                    "text": "Main idea",
                    "source_block_ids": ["blk_title"],
                },
                "paragraphs": [
                    {
                        "text": "The source establishes the topic.",
                        "source_block_ids": ["blk_title"],
                    }
                ],
                "key_points": [],
            }
        ],
    }


def _verification_stage_response() -> dict[str, object]:
    return {
        "claims": [
            {"claim_id": claim_id, "status": "supported"}
            for claim_id in (
                "title-0001",
                "overview-0001",
                "section-0001-heading-0001",
                "section-0001-paragraph-0001",
            )
        ]
    }


def _legacy_model_binding(identity: str, bridge: object) -> LegacyModelBinding:
    return LegacyModelBinding(
        provider_kind="fixture",
        model_identity=identity,
        bridge=bridge,
        capabilities=LegacyModelCapabilities(),
    )


def _core_model_binding(identity: str) -> ModelExecutionBinding:
    return ModelExecutionBinding(
        schema_version=1,
        provider_type="fixture",
        model_identity=identity,
        credential_profile_ref="fixture/credential",
        context_window_tokens=128_000,
        max_output_tokens=4_096,
        max_concurrency=1,
        supports_structured_output=True,
        supports_temperature=True,
        timeout_seconds=60,
    )


def _dual_model_runtime(
    machine_root: Path,
    *,
    owner_id: str,
    clock: Callable[[], int],
    composer_bridge: object,
    verifier_bridge: object,
    workspace_instance_id: str | None = None,
) -> runtime_module.AllToNoteRuntime:
    composer_identity = "fixture/composer-v1"
    verifier_identity = "fixture/reviewer-v1"
    return runtime_module.create_document_runtime(
        machine_root,
        worker_config=object(),  # type: ignore[arg-type]
        model=_legacy_model_binding(composer_identity, composer_bridge),
        model_execution_binding=_core_model_binding(composer_identity),
        model_execution_profile="composer",
        verifier_model=_legacy_model_binding(verifier_identity, verifier_bridge),
        verifier_model_execution_binding=_core_model_binding(verifier_identity),
        verifier_model_execution_profile="reviewer",
        pack_environment=_DOCUMENT_PACK_ENVIRONMENT,
        owner_id=owner_id,
        local_instance_id="document-test",
        workspace_instance_id=workspace_instance_id,
        clock=clock,
    )


def _terminate_after_document_model_success(
    machine_root: str,
    job_id: str,
) -> None:
    runtime_module.DoclingWorkerParser = lambda _config: _RuntimeParser()
    document_service_module.DocumentBundleAssembler = (
        _TerminateAfterVerificationAssembler
    )
    runtime = _dual_model_runtime(
        Path(machine_root),
        owner_id="lost-document-process",
        clock=lambda: 1_000,
        composer_bridge=_RoutingStageBridge(
            "fixture/composer-v1",
            {"document-knowledge-compose": _compiled_stage_response()},
        ),
        verifier_bridge=_RoutingStageBridge(
            "fixture/reviewer-v1",
            {"document-knowledge-verify": _verification_stage_response()},
        ),
    )
    runtime.wait_job(job_id)
    os._exit(24)


def test_document_runtime_exposes_registered_workspace_instance_for_engine_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance_id = "d" * 32
    monkeypatch.setattr(
        runtime_module,
        "DoclingWorkerParser",
        lambda _config: _RuntimeParser(),
    )
    runtime = _dual_model_runtime(
        tmp_path / "machine-instance",
        owner_id="document-process",
        clock=lambda: 1_000,
        composer_bridge=_RoutingStageBridge(
            "fixture/composer-v1",
            {"document-knowledge-compose": _compiled_stage_response()},
        ),
        verifier_bridge=_RoutingStageBridge(
            "fixture/reviewer-v1",
            {"document-knowledge-verify": _verification_stage_response()},
        ),
        workspace_instance_id=instance_id,
    )

    assert runtime.workspace_instance_id == instance_id


def _document_operation_rows(
    machine_root: Path,
    job_id: str,
) -> tuple[tuple[object, ...], ...]:
    with sqlite3.connect(machine_root / "job-store" / "jobs.sqlite") as connection:
        rows = connection.execute(
            """
            SELECT operation_id, job_id, step_id, attempt_id, provider,
                   request_hash, operation_idempotency_key, provider_request_id,
                   outcome, summary_json, created_at, updated_at
            FROM external_operations
            WHERE job_id = ?
            ORDER BY rowid
            """,
            (job_id,),
        ).fetchall()
    return tuple(tuple(row) for row in rows)


def _document_checkpoint_count(
    machine_root: Path,
    job_id: str,
    step_id: str,
) -> int:
    with sqlite3.connect(machine_root / "job-store" / "jobs.sqlite") as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) FROM checkpoints
            WHERE job_id = ? AND step_id = ?
            """,
            (job_id, step_id),
        ).fetchone()
    assert row is not None
    return int(row[0])


class _KnowledgeCompiler:
    def __init__(
        self,
        *,
        verifier_provider_profile: str = "default",
        verifier_model_identity: str = "fixture/model-v1",
    ) -> None:
        self.calls = 0
        self.verifier_provider_profile = verifier_provider_profile
        self.verifier_model_identity = verifier_model_identity

    def compilation_identity(self) -> str:
        return "sha256:" + "c" * 64

    def model_identity(self) -> str:
        return "fixture/model-v1"

    def compile(
        self,
        request: DocumentKnowledgeCompilationInput,
        *,
        execution,
    ) -> CompiledDocumentKnowledgeNoteV1:
        execution.heartbeat()
        self.calls += 1
        assert request.provider_profile == "default"
        assert request.model_override == "fixture/model-v1"
        assert request.output_language == "en"
        return CompiledDocumentKnowledgeNoteV1(
            title=DocumentKnowledgeClaimV1(
                "Compiled paper note",
                ("blk_title",),
            ),
            overview=(
                DocumentKnowledgeClaimV1(
                    "The paper studies glossy reflections.",
                    ("blk_title",),
                ),
            ),
            sections=(
                DocumentKnowledgeSectionV1(
                    DocumentKnowledgeClaimV1(
                        "Main idea",
                        ("blk_title",),
                    ),
                    (
                        DocumentKnowledgeClaimV1(
                            "The source establishes the topic.",
                            ("blk_title",),
                        ),
                    ),
                    (),
                ),
            ),
            referenced_block_ids=("blk_title",),
            model_identity="fixture/model-v1",
            input_tokens=10,
            output_tokens=20,
            token_counts_complete=True,
        )

    def verify(
        self,
        request: DocumentKnowledgeVerificationInput,
        *,
        execution,
    ) -> DocumentKnowledgeVerificationV1:
        execution.heartbeat()
        assert (
            request.verifier_provider_profile
            == self.verifier_provider_profile
        )
        assert request.verifier_model_override == self.verifier_model_identity
        return DocumentKnowledgeVerificationV1(
            compiled_sha256=compiled_document_knowledge_sha256(request.compiled),
            evidence_input_sha256=document_knowledge_evidence_sha256(
                request.parsed, request.compiled
            ),
            claims=tuple(
                DocumentKnowledgeClaimVerificationV1(claim_id, "supported")
                for claim_id, _claim in document_knowledge_claims(request.compiled)
            ),
            model_identity=self.verifier_model_identity,
            input_tokens=5,
            output_tokens=5,
            token_counts_complete=True,
        )


class _CrashParser(_Parser):
    def parse(
        self,
        source: Path,
        *,
        work_root: Path,
        cancellation_token,
    ) -> ParsedDocument:
        cancellation_token.raise_if_cancelled()
        self.calls += 1
        raise RuntimeError("injected parser crash")


class _CrashAfterCommitGateway:
    def __init__(self) -> None:
        self._delegate = IWikiPortableGateway()
        self._crash = True

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    def commit_prepared(self, prepared: object) -> object:
        result = self._delegate.commit_prepared(prepared)
        if self._crash:
            self._crash = False
            raise RuntimeError("injected crash after Portable commit")
        return result


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    shutil.copytree(FIXTURE_ROOT, workspace)
    shutil.rmtree(workspace / "raw" / "personal" / ".staging")
    for relative in (
        "raw/common",
        "raw/personal/.staging",
        "wiki/common",
        "wiki/personal",
        ".cache",
    ):
        (workspace / relative).mkdir(parents=True, exist_ok=True)
    return workspace


def _service(
    machine_root: Path,
    parser: _Parser,
    gateway: object,
    *,
    owner_id: str,
    resource_lease_store: MachineResourceLeaseStore | None = None,
    resource_owner: ResourceOwner | None = None,
    adopted_resource_lease: ResourceLease | None = None,
    expected_job_authority: JobExecutionAuthority | None = None,
    knowledge_compiler: _KnowledgeCompiler | None = None,
    pack_environment: JobPackEnvironmentSnapshot | None = (
        _DOCUMENT_PACK_ENVIRONMENT
    ),
) -> tuple[DocumentService, SqliteJobRepository]:
    repository = SqliteJobRepository.open(machine_root / "job-store")
    storage = FileAttemptStorage(
        machine_root / "attempts",
        repository,
        validators={
            CHECKPOINT_SCHEMA: lambda payload: isinstance(json.loads(payload), dict)
        },
    )

    def resolve_source_identity(
        workspace_root: Path,
        connector_id: str,
        canonical_identity: str,
    ) -> SourceIdentityBinding | None:
        return VerifiedSourceIdentityRegistry(
            workspace_root,
            cache=repository,
            truth=gateway,
        ).resolve_verified(connector_id, canonical_identity)

    service = DocumentService(
        repository,
        storage,
        parser,
        gateway,
        work_root=storage.root,
        checkpoint_reader=lambda metadata: (
            storage.root / Path(metadata.relative_path)
        ).read_bytes(),
        owner_id=owner_id,
        local_instance_id="document-test",
        source_identity_resolver=resolve_source_identity,
        knowledge_compiler=knowledge_compiler,
        pack_environment=pack_environment,
        resource_lease_store=resource_lease_store,
        resource_owner=resource_owner,
        adopted_resource_lease=adopted_resource_lease,
        expected_job_authority=expected_job_authority,
    )
    return service, repository


def _submit(
    service: DocumentService,
    repository: SqliteJobRepository,
    workspace: Path,
    source: Path,
    *,
    execution_owner: JobExecutionOwner = JobExecutionOwner.FOREGROUND,
) -> tuple[str, JobExecutionRouter]:
    registry = RecipeRegistry(
        ((DOCUMENT_NOTE_V1, DocumentRecipeAdapter(service)),)
    )
    submission = ProduceService(registry).submit(
        ProduceRequest(
            1,
            RecipeKey("alltonote.document-note", 1),
            InputDescriptor("file", str(source)),
            str(workspace),
            ("knowledge-note",),
        ),
        execution_owner=execution_owner,
    )
    router = JobExecutionRouter(
        repository,
        ((service.execution_binding, service),),
    )
    return submission.job_id, router


def _submit_detached_document_in_process(
    machine_root: str,
    workspace: str,
    source: str,
    owner_id: str,
    ready_queue,
    start_event,
    result_queue,
) -> None:
    service, repository = _service(
        Path(machine_root),
        _Parser(),
        IWikiPortableGateway(),
        owner_id=owner_id,
    )
    ready_queue.put(owner_id)
    start_event.wait()
    try:
        job_id, _router = _submit(
            service,
            repository,
            Path(workspace),
            Path(source),
            execution_owner=JobExecutionOwner.ENGINE,
        )
    except BaseException as error:
        result_queue.put(("error", type(error).__name__, str(error)))
    else:
        result_queue.put(("ok", job_id))


def test_detached_document_uses_managed_snapshot_after_original_is_deleted(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "原始文档 有空格.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    parser = _Parser()
    machine_root = tmp_path / "machine"
    service, repository = _service(
        machine_root,
        parser,
        IWikiPortableGateway(),
        owner_id="runtime-one",
    )
    job_id, router = _submit(
        service,
        repository,
        workspace,
        source,
        execution_owner=JobExecutionOwner.ENGINE,
    )

    stored_request = json.loads(repository.get_job_request(job_id))
    assert stored_request["input_path"] == str(source.resolve())
    snapshot_events = tuple(
        event
        for event in repository.list_events(job_id)
        if event.event_type == "document.input-snapshot.v1"
    )
    assert len(snapshot_events) == 1
    snapshot_binding = json.loads(snapshot_events[0].payload_json)
    expected_digest = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    assert snapshot_binding == {
        "byte_length": source.stat().st_size,
        "schema_version": 1,
        "sha256": expected_digest,
    }
    assert "path" not in snapshot_events[0].payload_json
    pack_events = tuple(
        event
        for event in repository.list_events(job_id)
        if event.event_type == "execution.pack-environment.v1"
    )
    assert len(pack_events) == 1
    assert json.loads(pack_events[0].payload_json) == {
        "packs": [
            {
                "manifest_sha256": "sha256:" + "a" * 64,
                "pack_id": PACK_ID,
                "pack_version": PACK_VERSION,
                "platform": current_document_pack_platform(),
            }
        ],
        "schema_version": 1,
    }
    source.unlink()

    completed = router.wait_job(job_id)

    assert completed.state is JobState.SUCCEEDED
    assert parser.calls == 1


def test_detached_document_rejects_unfrozen_pack_before_snapshot(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    machine_root = tmp_path / "machine"
    service, repository = _service(
        machine_root,
        _Parser(),
        IWikiPortableGateway(),
        owner_id="runtime-one",
        pack_environment=None,
    )

    with pytest.raises(DomainError) as raised:
        _submit(
            service,
            repository,
            workspace,
            source,
            execution_owner=JobExecutionOwner.ENGINE,
        )

    assert raised.value.code == "pack_generation_unavailable"
    assert not tuple(
        (machine_root / "attempts" / "document-inputs").rglob("source.pdf")
    )


def test_tampered_detached_document_snapshot_fails_before_parser(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    parser = _Parser()
    machine_root = tmp_path / "machine"
    service, repository = _service(
        machine_root,
        parser,
        IWikiPortableGateway(),
        owner_id="runtime-one",
    )
    job_id, router = _submit(
        service,
        repository,
        workspace,
        source,
        execution_owner=JobExecutionOwner.ENGINE,
    )
    snapshots = tuple(
        (machine_root / "attempts" / "document-inputs").rglob("source.pdf")
    )
    assert len(snapshots) == 1
    snapshots[0].write_bytes(b"%PDF-1.7\ntampered\n")

    completed = router.wait_job(job_id)

    assert completed.state is JobState.FAILED
    assert completed.error is not None
    assert completed.error.code == "document_input_changed"
    assert parser.calls == 0


def test_concurrent_detached_document_submissions_reuse_one_snapshot(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "shared paper.pdf"
    source.write_bytes(b"%PDF-1.7\nshared fixture\n")
    machine_root = tmp_path / "machine"
    service, repository = _service(
        machine_root,
        _Parser(),
        IWikiPortableGateway(),
        owner_id="runtime-one",
    )

    def submit() -> str:
        job_id, _router = _submit(
            service,
            repository,
            workspace,
            source,
            execution_owner=JobExecutionOwner.ENGINE,
        )
        return job_id

    with ThreadPoolExecutor(max_workers=8) as executor:
        job_ids = tuple(executor.map(lambda _index: submit(), range(8)))

    assert len(set(job_ids)) == 8
    assert len(tuple((machine_root / "attempts" / "document-inputs").rglob("source.pdf"))) == 1
    assert all(
        sum(
            event.event_type == "document.input-snapshot.v1"
            for event in repository.list_events(job_id)
        )
        == 1
        for job_id in job_ids
    )


def test_cross_process_detached_document_submissions_reuse_one_snapshot(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "shared process paper.pdf"
    source.write_bytes(b"%PDF-1.7\nshared process fixture\n")
    machine_root = tmp_path / "machine"
    _service(
        machine_root,
        _Parser(),
        IWikiPortableGateway(),
        owner_id="initializer",
    )
    context = multiprocessing.get_context("spawn")
    ready_queue = context.Queue()
    result_queue = context.Queue()
    start_event = context.Event()
    processes = tuple(
        context.Process(
            target=_submit_detached_document_in_process,
            args=(
                str(machine_root),
                str(workspace),
                str(source),
                f"submitter-{index}",
                ready_queue,
                start_event,
                result_queue,
            ),
        )
        for index in range(4)
    )
    for process in processes:
        process.start()
    for _process in processes:
        ready_queue.get(timeout=20)
    start_event.set()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    results = tuple(result_queue.get(timeout=5) for _process in processes)

    assert all(result[0] == "ok" for result in results), results
    assert len({result[1] for result in results}) == 4
    assert len(
        tuple((machine_root / "attempts" / "document-inputs").rglob("source.pdf"))
    ) == 1
    reopened = SqliteJobRepository.open(machine_root / "job-store")
    assert all(
        sum(
            event.event_type == "document.input-snapshot.v1"
            for event in reopened.list_events(result[1])
        )
        == 1
        for result in results
    )


def test_detached_document_rejects_linked_snapshot_ancestor_before_write(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    machine_root = tmp_path / "machine"
    service, repository = _service(
        machine_root,
        _Parser(),
        IWikiPortableGateway(),
        owner_id="runtime-one",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_root = machine_root / "attempts" / "document-inputs"
    try:
        os.symlink(outside, linked_root, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink unavailable: {error}")

    with pytest.raises(DomainError) as caught:
        _submit(
            service,
            repository,
            workspace,
            source,
            execution_owner=JobExecutionOwner.ENGINE,
        )

    assert caught.value.code == "document_input_snapshot_unavailable"
    assert tuple(outside.iterdir()) == ()


def test_detached_document_rejects_snapshot_ancestor_replaced_before_execution(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    parser = _Parser()
    machine_root = tmp_path / "machine"
    service, repository = _service(
        machine_root,
        parser,
        IWikiPortableGateway(),
        owner_id="runtime-one",
    )
    job_id, router = _submit(
        service,
        repository,
        workspace,
        source,
        execution_owner=JobExecutionOwner.ENGINE,
    )
    snapshot_root = machine_root / "attempts" / "document-inputs"
    outside = tmp_path / "outside"
    snapshot_root.rename(outside)
    try:
        os.symlink(outside, snapshot_root, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink unavailable: {error}")

    completed = router.wait_job(job_id)

    assert completed.state is JobState.FAILED
    assert completed.error is not None
    assert completed.error.code == "document_input_changed"
    assert parser.calls == 0


def test_detached_document_rejects_hardlinked_snapshot_lock_without_mutation(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "paper.pdf"
    raw = b"%PDF-1.7\nfixture\n"
    source.write_bytes(raw)
    machine_root = tmp_path / "machine"
    service, repository = _service(
        machine_root,
        _Parser(),
        IWikiPortableGateway(),
        owner_id="runtime-one",
    )
    digest = hashlib.sha256(raw).hexdigest()
    snapshot_parent = (
        machine_root
        / "attempts"
        / "document-inputs"
        / digest[:2]
        / digest
    )
    snapshot_parent.mkdir(parents=True)
    canary = tmp_path / "canary.lock"
    canary.write_bytes(b"do not truncate")
    os.link(canary, snapshot_parent / ".snapshot.lock")

    with pytest.raises(DomainError) as caught:
        _submit(
            service,
            repository,
            workspace,
            source,
            execution_owner=JobExecutionOwner.ENGINE,
        )

    assert caught.value.code == "document_input_snapshot_unavailable"
    assert canary.read_bytes() == b"do not truncate"


def test_legacy_engine_document_without_pack_snapshot_fails_closed(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "legacy paper.pdf"
    source.write_bytes(b"%PDF-1.7\nlegacy fixture\n")
    source_stat = source.stat()
    parser = _Parser()
    machine_root = tmp_path / "machine"
    service, repository = _service(
        machine_root,
        parser,
        IWikiPortableGateway(),
        owner_id="runtime-after-upgrade",
    )
    request = DocumentProduceRequest(
        request_schema_version=1,
        workspace_root=workspace,
        input_path=source.resolve(),
        expected_source_sha256=(
            "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
        ),
        expected_source_size=source_stat.st_size,
        expected_source_mtime_ns=source_stat.st_mtime_ns,
    )
    submitted = JobService(repository).submit(
        request,
        execution_binding=service.execution_binding,
        execution_owner=JobExecutionOwner.ENGINE,
    )

    with pytest.raises(DomainError) as raised:
        service.wait_job(submitted.job_id)

    assert raised.value.code == "execution_pack_snapshot_missing"
    assert parser.calls == 0
    assert not (machine_root / "attempts" / "document-inputs").exists()


def test_document_retry_inherits_detached_input_snapshot_binding(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nretry fixture\n")
    machine_root = tmp_path / "machine"
    first_service, first_repository = _service(
        machine_root,
        _Parser(),
        IWikiPortableGateway(),
        owner_id="runtime-one",
    )
    job_id, _first_router = _submit(
        first_service,
        first_repository,
        workspace,
        source,
        execution_owner=JobExecutionOwner.ENGINE,
    )
    source.unlink()
    cancelled = first_service.cancel_job(job_id)
    assert cancelled.state is JobState.CANCELLED

    parser = _Parser()
    second_service, second_repository = _service(
        machine_root,
        parser,
        IWikiPortableGateway(),
        owner_id="runtime-two",
    )
    retried = JobRuntime(
        second_repository,
        wait_job=None,
        current_config_snapshot=None,
        execution_owner=JobExecutionOwner.ENGINE,
    ).retry_job(
        job_id,
        RetryJobRequest(1, "retry-document-snapshot", JobState.CANCELLED),
    )
    retry_events = tuple(
        event
        for event in second_repository.list_events(retried.snapshot.job_id)
        if event.event_type == "document.input-snapshot.v1"
    )

    completed = second_service.wait_job(retried.snapshot.job_id)

    assert len(retry_events) == 1
    assert completed.state is JobState.SUCCEEDED
    assert parser.calls == 1


def test_legacy_document_does_not_adopt_unbound_content_snapshot(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    raw = b"%PDF-1.7\nshared legacy fixture\n"
    current_source = tmp_path / "current.pdf"
    legacy_source = tmp_path / "legacy.pdf"
    current_source.write_bytes(raw)
    legacy_source.write_bytes(raw)
    parser = _Parser()
    machine_root = tmp_path / "machine"
    service, repository = _service(
        machine_root,
        parser,
        IWikiPortableGateway(),
        owner_id="runtime-one",
    )
    _submit(
        service,
        repository,
        workspace,
        current_source,
        execution_owner=JobExecutionOwner.ENGINE,
    )
    assert len(tuple((machine_root / "attempts" / "document-inputs").rglob("source.pdf"))) == 1
    legacy_stat = legacy_source.stat()
    legacy_request = DocumentProduceRequest(
        request_schema_version=1,
        workspace_root=workspace,
        input_path=legacy_source.resolve(),
        expected_source_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
        expected_source_size=legacy_stat.st_size,
        expected_source_mtime_ns=legacy_stat.st_mtime_ns,
    )
    legacy = JobService(repository).submit(
        legacy_request,
        execution_binding=service.execution_binding,
        execution_owner=JobExecutionOwner.ENGINE,
    )
    legacy_source.unlink()

    with pytest.raises(DomainError) as raised:
        service.wait_job(legacy.job_id)

    assert raised.value.code == "execution_pack_snapshot_missing"
    assert parser.calls == 0


def test_document_recipe_uses_generic_submit_and_survives_reopen(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    parser = _Parser()
    machine_root = tmp_path / "machine"
    service, repository = _service(
        machine_root,
        parser,
        IWikiPortableGateway(),
        owner_id="runtime-one",
    )
    assert service.execution_binding.pack_version == PACK_VERSION
    job_id, router = _submit(service, repository, workspace, source)

    completed = router.wait_job(job_id)

    assert completed.state is JobState.SUCCEEDED
    assert isinstance(completed.result, RecipeProduceResult)
    assert completed.result.result_kind == "document-note"
    assert completed.result.usage == {"pages": 1, "blocks": 1}
    assert completed.result.quality_overall == "pass"
    assert completed.result.publish_eligible is False
    assert set(completed.result.artifacts) == {
        "evidence_set",
        "normalized_content",
        "primary_draft",
        "quality_report",
        "source_metadata",
    }
    assert parser.calls == 1
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before

    reopened_service, reopened_repository = _service(
        machine_root,
        parser,
        IWikiPortableGateway(),
        owner_id="runtime-two",
    )
    reopened_router = JobExecutionRouter(
        reopened_repository,
        ((reopened_service.execution_binding, reopened_service),),
    )
    reopened = reopened_router.get_job(job_id)
    assert reopened == completed
    assert parser.calls == 1

    second_job_id, second_router = _submit(
        reopened_service,
        reopened_repository,
        workspace,
        source,
    )
    second = second_router.wait_job(second_job_id)
    assert second.state is JobState.SUCCEEDED
    assert isinstance(second.result, RecipeProduceResult)
    assert second.result.source_id == completed.result.source_id
    assert second.result.source_revision_id != completed.result.source_revision_id
    assert second.result.bundle_id != completed.result.bundle_id
    assert parser.calls == 2


def test_document_cancellation_interrupts_parser_and_settles_job(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    started = threading.Event()
    poll = threading.Event()

    class BlockingParser(_Parser):
        def parse(
            self,
            source: Path,
            *,
            work_root: Path,
            cancellation_token,
        ) -> ParsedDocument:
            started.set()
            while True:
                cancellation_token.raise_if_cancelled()
                poll.wait(0.01)

    service, repository = _service(
        tmp_path / "machine",
        BlockingParser(),
        IWikiPortableGateway(),
        owner_id="runtime-one",
    )
    job_id, _ = _submit(service, repository, workspace, source)

    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(service.wait_job, job_id)
        assert started.wait(timeout=2)
        service.cancel_job(job_id)
        completed = result.result(timeout=2)

    assert completed.state is JobState.CANCELLED
    _, active_attempt, _ = repository.get_job_details(job_id)
    assert active_attempt is None


def test_document_wait_returns_cancelled_when_job_changes_before_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    parser = _Parser()
    service, repository = _service(
        tmp_path / "machine",
        parser,
        IWikiPortableGateway(),
        owner_id="runtime-one",
    )
    job_id, _ = _submit(service, repository, workspace, source)
    original_claim = repository.claim_job

    def cancel_then_claim(
        claimed_job_id: str,
        owner_id: str,
        *,
        ttl_seconds: int,
    ):
        repository.cancel_job(claimed_job_id)
        return original_claim(
            claimed_job_id,
            owner_id,
            ttl_seconds=ttl_seconds,
        )

    monkeypatch.setattr(repository, "claim_job", cancel_then_claim)

    completed = service.wait_job(job_id)

    assert completed.state is JobState.CANCELLED
    assert parser.calls == 0


def test_document_fenced_heartbeat_preserves_authority_loss_error(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    heartbeat_failed = threading.Event()
    repository: SqliteJobRepository

    class FencingParser(_Parser):
        def parse(
            self,
            source: Path,
            *,
            work_root: Path,
            cancellation_token,
        ) -> ParsedDocument:
            repository._clock = lambda: 302_000
            repository.claim_job(
                job_id,
                "replacement-owner",
                ttl_seconds=300,
            )
            assert heartbeat_failed.wait(timeout=2)
            return super().parse(
                source,
                work_root=work_root,
                cancellation_token=cancellation_token,
            )

    service, repository = _service(
        tmp_path / "machine",
        FencingParser(),
        IWikiPortableGateway(),
        owner_id="runtime-one",
    )
    repository._clock = lambda: 1_000
    service._checkpoint_runner.heartbeat_interval_seconds = 0.01
    original_heartbeat = repository.heartbeat_job_claim

    def observe_heartbeat(authority: object, *, ttl_seconds: int) -> object:
        try:
            return original_heartbeat(authority, ttl_seconds=ttl_seconds)
        except BaseException:
            if threading.current_thread() is not threading.main_thread():
                heartbeat_failed.set()
            raise

    repository.heartbeat_job_claim = observe_heartbeat  # type: ignore[method-assign]
    job_id, router = _submit(service, repository, workspace, source)

    with pytest.raises(DomainError, match="job_claim_fenced"):
        router.wait_job(job_id)

    assert repository.latest_checkpoint(job_id, "parse") is None


def test_document_source_identity_tracks_path_while_revision_tracks_bytes(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    first_source = tmp_path / "first paper.pdf"
    second_source = tmp_path / "second paper.pdf"
    original = b"%PDF-1.7\nidentical fixture\n"
    first_source.write_bytes(original)
    second_source.write_bytes(original)
    parser = _Parser()
    service, repository = _service(
        tmp_path / "machine",
        parser,
        IWikiPortableGateway(),
        owner_id="runtime-one",
    )
    legacy_source_id = new_typed_id("src")
    repository.cache_source_identity_candidate(
        SourceIdentityBinding(
            connector_id="local-document-sha256",
            canonical_identity="sha256:" + hashlib.sha256(original).hexdigest(),
            source_id=legacy_source_id,
            owning_bundle_id=new_typed_id("bnd"),
            manifest_sha256="sha256:" + "a" * 64,
        )
    )
    current_identity = sha256_digest(
        "document-test\0"
        + os.path.normcase(os.path.normpath(str(first_source.resolve())))
    )
    stale_source_id = new_typed_id("src")
    stale_binding = SourceIdentityBinding(
        connector_id="local-document-path-v1",
        canonical_identity=current_identity,
        source_id=stale_source_id,
        owning_bundle_id=new_typed_id("bnd"),
        manifest_sha256="sha256:" + "b" * 64,
    )
    repository.cache_source_identity_candidate(stale_binding)

    first_job_id, first_router = _submit(
        service,
        repository,
        workspace,
        first_source,
    )
    first = first_router.wait_job(first_job_id)
    second_job_id, second_router = _submit(
        service,
        repository,
        workspace,
        second_source,
    )
    second = second_router.wait_job(second_job_id)

    assert first.state is JobState.SUCCEEDED
    assert second.state is JobState.SUCCEEDED
    assert isinstance(first.result, RecipeProduceResult)
    assert isinstance(second.result, RecipeProduceResult)
    assert first.result.source_id != legacy_source_id
    assert first.result.source_id != stale_source_id
    assert first.result.source_id != second.result.source_id
    assert first.result.source_revision_id != second.result.source_revision_id

    verified_binding = repository.read_source_identity_candidate(
        "local-document-path-v1",
        current_identity,
    )
    assert verified_binding is not None
    repository.discard_source_identity_candidate(verified_binding)
    rebuilt_job_id, rebuilt_router = _submit(
        service,
        repository,
        workspace,
        first_source,
    )
    rebuilt = rebuilt_router.wait_job(rebuilt_job_id)
    assert rebuilt.state is JobState.SUCCEEDED
    assert isinstance(rebuilt.result, RecipeProduceResult)
    assert rebuilt.result.source_id == first.result.source_id
    assert rebuilt.result.source_revision_id != first.result.source_revision_id

    previous_mtime_ns = first_source.stat().st_mtime_ns
    changed = b"%PDF-1.7\nchanged fixture content\n"
    first_source.write_bytes(changed)
    os.utime(
        first_source,
        ns=(previous_mtime_ns + 1_000_000_000, previous_mtime_ns + 1_000_000_000),
    )
    changed_job_id, changed_router = _submit(
        service,
        repository,
        workspace,
        first_source,
    )
    changed_result = changed_router.wait_job(changed_job_id)

    assert changed_result.state is JobState.SUCCEEDED
    assert isinstance(changed_result.result, RecipeProduceResult)
    assert changed_result.result.source_id == first.result.source_id
    assert changed_result.result.source_revision_id != first.result.source_revision_id

    first_manifest = json.loads(
        (
            workspace
            / first.result.workspace_relative_bundle_path
            / "bundle.json"
        ).read_text(encoding="utf-8")
    )
    second_manifest = json.loads(
        (
            workspace
            / second.result.workspace_relative_bundle_path
            / "bundle.json"
        ).read_text(encoding="utf-8")
    )
    changed_manifest_path = (
        workspace
        / changed_result.result.workspace_relative_bundle_path
        / "bundle.json"
    )
    changed_manifest_text = changed_manifest_path.read_text(encoding="utf-8")
    changed_manifest = json.loads(changed_manifest_text)
    first_identity = first_manifest["sources"][0]["canonical_identity"]
    second_identity = second_manifest["sources"][0]["canonical_identity"]
    changed_identity = changed_manifest["sources"][0]["canonical_identity"]

    assert first_identity["scheme"] == "local-document-path-v1"
    assert first_identity != second_identity
    assert changed_identity == first_identity
    assert changed_manifest["source_revisions"][0]["content_digest"] == (
        "sha256:" + hashlib.sha256(changed).hexdigest()
    )
    assert changed_manifest["source_revisions"][0]["materialization"] == {
        "kind": "external_local",
        "external_ref_id": f"ext_{first.result.source_id.removeprefix('src_')}",
    }
    assert str(first_source) not in changed_manifest_text
    assert str(first_source.parent) not in changed_manifest_text


def test_document_candidate_checkpoint_identity_modes_fail_closed(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "paper.pdf"
    raw = b"%PDF-1.7\nfixture\n"
    source.write_bytes(raw)
    service, _ = _service(
        tmp_path / "machine",
        _Parser(),
        IWikiPortableGateway(),
        owner_id="runtime-one",
    )
    stat = source.stat()
    request = DocumentProduceRequest(
        1,
        workspace,
        source.resolve(),
        "sha256:" + hashlib.sha256(raw).hexdigest(),
        stat.st_size,
        stat.st_mtime_ns,
    )
    legacy = DocumentCandidateCheckpoint(
        staging_relative_path="raw/personal/.staging/candidate",
        bundle_id=new_typed_id("bnd"),
        manifest_sha256="sha256:" + "a" * 64,
        run_id=new_typed_id("run"),
        source_id=new_typed_id("src"),
        source_revision_id=new_typed_id("rev"),
        artifacts={"primary_draft": new_typed_id("art")},
        quality_overall="pass",
        publish_eligible=False,
        usage={"pages": 1, "blocks": 1},
        warnings=(),
    )

    assert service._checkpoint_source_identity(request, legacy) == (
        "local-document-sha256",
        request.expected_source_sha256,
    )
    current_connector, current_identity = service._source_identity(request)
    mismatched = replace(
        legacy,
        source_identity_connector_id=current_connector,
        source_canonical_identity="sha256:" + "b" * 64,
    )
    with pytest.raises(DomainError) as caught:
        service._checkpoint_source_identity(request, mismatched)
    assert caught.value.code == "candidate_checkpoint_invalid"
    assert current_identity != mismatched.source_canonical_identity


def _knowledge_request(
    workspace: Path,
    source: Path,
) -> DocumentKnowledgeProduceRequest:
    raw = source.read_bytes()
    stat = source.stat()
    return DocumentKnowledgeProduceRequest(
        request_schema_version=2,
        workspace_root=workspace,
        input_path=source.resolve(),
        expected_source_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
        expected_source_size=stat.st_size,
        expected_source_mtime_ns=stat.st_mtime_ns,
        provider_profile="default",
        model_override="fixture/model-v1",
        output_language="en",
    )


def test_document_v2_same_model_review_remains_not_publishable(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    compiler = _KnowledgeCompiler()
    service, repository = _service(
        tmp_path / "machine",
        _Parser(),
        IWikiPortableGateway(),
        owner_id="runtime-one",
        knowledge_compiler=compiler,
    )
    submission = ProduceService(
        RecipeRegistry(((DOCUMENT_NOTE_V1, DocumentRecipeAdapter(service)),))
    ).submit(
        ProduceRequest(
            1,
            RecipeKey("alltonote.document-note", 1),
            InputDescriptor("file", str(source)),
            str(workspace),
            ("knowledge-note",),
            {
                "provider_profile": "default",
                "model_override": None,
                "output_language": "en",
            },
        )
    )
    router = JobExecutionRouter(
        repository,
        ((service.execution_binding, service),),
    )
    stored_request = json.loads(
        repository.get_job_request(submission.job_id) or ""
    )

    completed = router.wait_job(submission.job_id)

    assert stored_request["request_schema_version"] == 2
    assert stored_request["model_override"] == "fixture/model-v1"
    assert completed.state is JobState.SUCCEEDED
    assert completed.result is not None
    assert completed.result.publish_eligible is False
    assert completed.result.quality_overall == "fail"
    assert set(completed.result.artifacts) == {
        "evidence_set",
        "knowledge_map",
        "normalized_content",
        "primary_draft",
        "quality_report",
        "source_metadata",
    }
    assert compiler.calls == 1


def test_document_v3_independent_verifier_is_frozen_and_publishable(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    compiler = _KnowledgeCompiler(
        verifier_provider_profile="reviewer",
        verifier_model_identity="fixture/reviewer-v1",
    )
    service, repository = _service(
        tmp_path / "machine",
        _Parser(),
        IWikiPortableGateway(),
        owner_id="runtime-one",
        knowledge_compiler=compiler,
    )
    submission = ProduceService(
        RecipeRegistry(((DOCUMENT_NOTE_V1, DocumentRecipeAdapter(service)),))
    ).submit(
        ProduceRequest(
            1,
            RecipeKey("alltonote.document-note", 1),
            InputDescriptor("file", str(source)),
            str(workspace),
            ("knowledge-note",),
            {
                "provider_profile": "default",
                "model_override": "fixture/model-v1",
                "output_language": "en",
                "verifier_provider_profile": "reviewer",
                "verifier_model_override": "fixture/reviewer-v1",
            },
        )
    )
    stored_request = json.loads(
        repository.get_job_request(submission.job_id) or ""
    )

    completed = service.wait_job(submission.job_id)

    assert stored_request["request_schema_version"] == 3
    assert stored_request["provider_profile"] == "default"
    assert stored_request["model_override"] == "fixture/model-v1"
    assert stored_request["verifier_provider_profile"] == "reviewer"
    assert stored_request["verifier_model_override"] == "fixture/reviewer-v1"
    assert completed.state is JobState.SUCCEEDED
    assert completed.result is not None
    assert completed.result.quality_overall == "pass"
    assert completed.result.publish_eligible is True


def test_document_runtime_routes_v2_self_review_and_v3_independent_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    composer_identity = "fixture/composer-v1"
    verifier_identity = "fixture/reviewer-v1"
    compiled_response = {
        "title": {
            "text": "Compiled paper note",
            "source_block_ids": ["blk_title"],
        },
        "overview": [
            {
                "text": "The paper studies glossy reflections.",
                "source_block_ids": ["blk_title"],
            }
        ],
        "sections": [
            {
                "heading": {
                    "text": "Main idea",
                    "source_block_ids": ["blk_title"],
                },
                "paragraphs": [
                    {
                        "text": "The source establishes the topic.",
                        "source_block_ids": ["blk_title"],
                    }
                ],
                "key_points": [],
            }
        ],
    }
    verification_response = {
        "claims": [
            {"claim_id": claim_id, "status": "supported"}
            for claim_id in (
                "title-0001",
                "overview-0001",
                "section-0001-heading-0001",
                "section-0001-paragraph-0001",
            )
        ]
    }
    composer_bridge = _RoutingStageBridge(
        composer_identity,
        {
            "document-knowledge-compose": compiled_response,
            "document-knowledge-verify": verification_response,
        },
    )
    verifier_bridge = _StageBridge(
        verifier_identity,
        "document-knowledge-verify",
        verification_response,
    )

    def legacy_binding(identity: str, bridge: object) -> LegacyModelBinding:
        return LegacyModelBinding(
            provider_kind="fixture",
            model_identity=identity,
            bridge=bridge,
            capabilities=LegacyModelCapabilities(),
        )

    def core_binding(identity: str) -> ModelExecutionBinding:
        return ModelExecutionBinding(
            schema_version=1,
            provider_type="fixture",
            model_identity=identity,
            credential_profile_ref="fixture/credential",
            context_window_tokens=128_000,
            max_output_tokens=4_096,
            max_concurrency=1,
            supports_structured_output=True,
            supports_temperature=True,
            timeout_seconds=60,
        )

    monkeypatch.setattr(
        runtime_module,
        "DoclingWorkerParser",
        lambda _config: _RuntimeParser(),
    )
    runtime = runtime_module.create_document_runtime(
        tmp_path / "machine-runtime",
        worker_config=object(),  # type: ignore[arg-type]
        model=legacy_binding(composer_identity, composer_bridge),
        model_execution_binding=core_binding(composer_identity),
        model_execution_profile="composer",
        verifier_model=legacy_binding(verifier_identity, verifier_bridge),
        verifier_model_execution_binding=core_binding(verifier_identity),
        verifier_model_execution_profile="reviewer",
    )
    legacy_submission = runtime.submit(
        ProduceRequest(
            1,
            RecipeKey("alltonote.document-note", 1),
            InputDescriptor("file", str(source)),
            str(workspace),
            ("knowledge-note",),
            {
                "provider_profile": "composer",
                "model_override": composer_identity,
                "output_language": "en",
            },
        )
    )
    legacy_request = json.loads(
        runtime.job_repository.get_job_request(legacy_submission.job_id) or ""
    )
    legacy_completed = runtime.wait_job(legacy_submission.job_id)

    assert legacy_request["request_schema_version"] == 2
    assert legacy_completed.state is JobState.SUCCEEDED
    assert legacy_completed.result is not None
    assert legacy_completed.result.quality_overall == "fail"
    assert legacy_completed.result.publish_eligible is False
    assert composer_bridge.calls == [
        "document-knowledge-compose",
        "document-knowledge-verify",
    ]
    assert verifier_bridge.calls == []

    independent_submission = runtime.submit(
        ProduceRequest(
            1,
            RecipeKey("alltonote.document-note", 1),
            InputDescriptor("file", str(source)),
            str(workspace),
            ("knowledge-note",),
            {
                "provider_profile": "composer",
                "model_override": composer_identity,
                "output_language": "en",
                "verifier_provider_profile": "reviewer",
                "verifier_model_override": verifier_identity,
            },
        )
    )

    completed = runtime.wait_job(independent_submission.job_id)

    assert completed.state is JobState.SUCCEEDED
    assert completed.result is not None
    assert completed.result.quality_overall == "pass"
    assert completed.result.publish_eligible is True
    assert composer_bridge.calls == [
        "document-knowledge-compose",
        "document-knowledge-verify",
        "document-knowledge-compose",
    ]
    assert verifier_bridge.calls == ["document-knowledge-verify"]


def test_document_process_restart_recovers_both_model_calls_without_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    machine_root = tmp_path / "machine-restart"
    submit_service, submit_repository = _service(
        machine_root,
        _Parser(),
        IWikiPortableGateway(),
        owner_id="submit-process",
        knowledge_compiler=_KnowledgeCompiler(
            verifier_provider_profile="reviewer",
            verifier_model_identity="fixture/reviewer-v1",
        ),
    )
    submission = ProduceService(
        RecipeRegistry(
            ((DOCUMENT_NOTE_V1, DocumentRecipeAdapter(submit_service)),)
        )
    ).submit(
        ProduceRequest(
            1,
            RecipeKey("alltonote.document-note", 1),
            InputDescriptor("file", str(source)),
            str(workspace),
            ("knowledge-note",),
            {
                "provider_profile": "composer",
                "model_override": "fixture/composer-v1",
                "output_language": "en",
                "verifier_provider_profile": "reviewer",
                "verifier_model_override": "fixture/reviewer-v1",
            },
        )
    )
    stored_request = json.loads(
        submit_repository.get_job_request(submission.job_id) or ""
    )
    assert stored_request["request_schema_version"] == 3

    process = multiprocessing.get_context("spawn").Process(
        target=_terminate_after_document_model_success,
        args=(str(machine_root), submission.job_id),
    )
    process.start()
    process.join(timeout=30)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)
        pytest.fail("Document crash fixture did not terminate")
    assert process.exitcode == 23

    crashed_repository = SqliteJobRepository.open(
        machine_root / "job-store",
        clock=lambda: 301_001,
    )
    crashed_job, crashed_attempt, crashed_result = (
        crashed_repository.get_job_details(submission.job_id)
    )
    assert crashed_job.state is JobState.RUNNING
    assert crashed_attempt is not None
    assert crashed_attempt.step_id == "assemble_candidate_bundle"
    assert crashed_attempt.state is AttemptState.RUNNING
    assert crashed_attempt.fencing_token == 1
    assert crashed_result is None

    parse_checkpoint = crashed_repository.latest_checkpoint(
        submission.job_id,
        "parse_document",
    )
    assert parse_checkpoint is not None
    assert (
        crashed_repository.latest_checkpoint(
            submission.job_id,
            "assemble_candidate_bundle",
        )
        is None
    )
    parse_path = machine_root / "attempts" / parse_checkpoint.relative_path
    parse_before = (
        parse_checkpoint,
        parse_path.read_bytes(),
        parse_path.stat().st_mtime_ns,
    )

    operations_before = _document_operation_rows(
        machine_root,
        submission.job_id,
    )
    assert len(operations_before) == 2
    assert {row[8] for row in operations_before} == {
        "external_outcome_succeeded"
    }
    assert len({row[5] for row in operations_before}) == 2
    summaries = tuple(json.loads(str(row[9])) for row in operations_before)
    assert {summary["shard_key"] for summary in summaries} == {
        "document-note",
        "document-note-verification",
    }
    result_root = machine_root / "attempts" / "model-operations"
    model_results_before = {
        summary["result"]["path"]: (
            (result_root / summary["result"]["path"]).read_bytes(),
            (result_root / summary["result"]["path"]).stat().st_mtime_ns,
        )
        for summary in summaries
    }
    assert len(model_results_before) == 2
    assert (
        _document_checkpoint_count(
            machine_root,
            submission.job_id,
            "parse_document",
        )
        == 1
    )
    assert not tuple(workspace.rglob("commit.json"))

    recovered_parser = _RuntimeParser()
    composer_bridge = _ForbiddenStageBridge("fixture/composer-v1")
    verifier_bridge = _ForbiddenStageBridge("fixture/reviewer-v1")
    monkeypatch.setattr(
        runtime_module,
        "DoclingWorkerParser",
        lambda _config: recovered_parser,
    )
    recovered_runtime = _dual_model_runtime(
        machine_root,
        owner_id="recovered-document-process",
        clock=lambda: 301_001,
        composer_bridge=composer_bridge,
        verifier_bridge=verifier_bridge,
    )

    completed = recovered_runtime.wait_job(submission.job_id)

    assert completed.state is JobState.SUCCEEDED
    assert completed.result is not None
    assert completed.result.quality_overall == "pass"
    assert completed.result.publish_eligible is True
    assert recovered_parser.calls == 0
    assert composer_bridge.calls == 0
    assert verifier_bridge.calls == 0
    assert _document_operation_rows(machine_root, submission.job_id) == (
        operations_before
    )
    assert {
        name: (path.read_bytes(), path.stat().st_mtime_ns)
        for name in model_results_before
        for path in (result_root / name,)
    } == model_results_before

    parse_after = recovered_runtime.job_repository.latest_checkpoint(
        submission.job_id,
        "parse_document",
    )
    assert parse_after == parse_before[0]
    assert parse_path.read_bytes() == parse_before[1]
    assert parse_path.stat().st_mtime_ns == parse_before[2]
    assert (
        _document_checkpoint_count(
            machine_root,
            submission.job_id,
            "parse_document",
        )
        == 1
    )
    assert len(
        tuple(
            (machine_root / "attempts" / "model-operations").glob("*.json")
        )
    ) == 2
    candidate_checkpoint = recovered_runtime.job_repository.latest_checkpoint(
        submission.job_id,
        "assemble_candidate_bundle",
    )
    assert candidate_checkpoint is not None
    assemble_attempts = sorted(
        (
            attempt
            for attempt in recovered_runtime.job_repository.list_attempts(
                submission.job_id
            )
            if attempt.step_id == "assemble_candidate_bundle"
        ),
        key=lambda attempt: attempt.fencing_token,
    )
    assert len(assemble_attempts) == 2
    assert assemble_attempts[0].attempt_id == crashed_attempt.attempt_id
    assert assemble_attempts[0].state is AttemptState.INTERRUPTED
    assert assemble_attempts[0].fencing_token == 1
    assert assemble_attempts[1].state is AttemptState.SUCCEEDED
    assert assemble_attempts[1].fencing_token == 2
    assert candidate_checkpoint.attempt_id == assemble_attempts[1].attempt_id
    assert len(tuple(workspace.rglob("commit.json"))) == 1


def test_document_v2_fails_before_parsing_when_compiler_is_unavailable(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    parser = _Parser()
    service, repository = _service(
        tmp_path / "machine",
        parser,
        IWikiPortableGateway(),
        owner_id="runtime-one",
    )
    snapshot = service.submit_document(_knowledge_request(workspace, source))
    router = JobExecutionRouter(
        repository,
        ((service.execution_binding, service),),
    )

    completed = router.wait_job(snapshot.job_id)

    assert completed.state is JobState.FAILED
    assert completed.error is not None
    assert completed.error.code == "document_knowledge_compiler_unavailable"
    assert parser.calls == 0


def test_document_v2_process_loss_pauses_unknown_model_operation(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    compiler = _KnowledgeCompiler()
    service, repository = _service(
        tmp_path / "machine",
        _Parser(),
        IWikiPortableGateway(),
        owner_id="new-process",
        knowledge_compiler=compiler,
    )
    snapshot = service.submit_document(_knowledge_request(workspace, source))
    old_authority = repository.claim_job(
        snapshot.job_id,
        "old-process",
        ttl_seconds=60,
    ).authority
    attempt = repository.start_attempt(
        repository.create_attempt(
            snapshot.job_id,
            "assemble_candidate_bundle",
            authority=old_authority,
        ).attempt_id,
        old_authority,
    )
    guard = ExternalOperationGuard(repository, old_authority)
    operation = guard.prepare(
        job_id=snapshot.job_id,
        step_id=attempt.step_id,
        attempt_id=attempt.attempt_id,
        provider="fixture",
        request_hash="sha256:" + "d" * 64,
        summary_json="{}",
    )
    guard.start(operation.operation_id)
    repository.release_job_claim(old_authority)

    paused = service.wait_job(snapshot.job_id)

    assert paused.state is JobState.WAITING_FOR_INPUT
    assert paused.challenge_id is not None
    assert compiler.calls == 0


def test_document_commit_crash_reuses_bundle_without_duplicate_effect(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    parser = _Parser()
    machine_root = tmp_path / "machine"
    service, repository = _service(
        machine_root,
        parser,
        _CrashAfterCommitGateway(),
        owner_id="runtime-one",
    )
    job_id, router = _submit(service, repository, workspace, source)

    with pytest.raises(RuntimeError, match="injected crash"):
        router.wait_job(job_id)

    interrupted = router.get_job(job_id)
    assert interrupted.state is JobState.RUNNING
    assert interrupted.result is None
    assert len(tuple(workspace.rglob("commit.json"))) == 1

    reopened_service, reopened_repository = _service(
        machine_root,
        parser,
        IWikiPortableGateway(),
        owner_id="runtime-two",
    )
    reopened_router = JobExecutionRouter(
        reopened_repository,
        ((reopened_service.execution_binding, reopened_service),),
    )
    completed = reopened_router.wait_job(job_id)

    assert completed.state is JobState.SUCCEEDED
    assert isinstance(completed.result, RecipeProduceResult)
    assert completed.result.idempotent
    assert parser.calls == 1
    assert len(tuple(workspace.rglob("commit.json"))) == 1


def test_document_parser_crash_leaves_no_result_and_can_restart(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    machine_root = tmp_path / "machine"
    crashing = _CrashParser()
    service, repository = _service(
        machine_root,
        crashing,
        IWikiPortableGateway(),
        owner_id="runtime-one",
    )
    job_id, router = _submit(service, repository, workspace, source)

    with pytest.raises(RuntimeError, match="parser crash"):
        router.wait_job(job_id)

    interrupted = router.get_job(job_id)
    assert interrupted.state is JobState.RUNNING
    assert interrupted.result is None
    assert not tuple(workspace.rglob("commit.json"))

    recovered_parser = _Parser()
    recovered_service, recovered_repository = _service(
        machine_root,
        recovered_parser,
        IWikiPortableGateway(),
        owner_id="runtime-two",
    )
    recovered_router = JobExecutionRouter(
        recovered_repository,
        ((recovered_service.execution_binding, recovered_service),),
    )
    completed = recovered_router.wait_job(job_id)
    assert completed.state is JobState.SUCCEEDED
    assert recovered_parser.calls == 1
    assert len(tuple(workspace.rglob("commit.json"))) == 1


def test_document_job_can_be_cancelled_before_execution(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    parser = _Parser()
    service, repository = _service(
        tmp_path / "machine",
        parser,
        IWikiPortableGateway(),
        owner_id="runtime-one",
    )
    job_id, router = _submit(service, repository, workspace, source)

    cancelled = router.cancel_job(job_id)

    assert cancelled.state is JobState.CANCELLED
    assert router.wait_job(job_id) == cancelled
    assert parser.calls == 0


def test_job_store_busy_does_not_fail_document_job(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    parser = _Parser()
    service, repository = _service(
        tmp_path / "document-busy-machine",
        parser,
        IWikiPortableGateway(),
        owner_id="document-busy-process",
    )
    job_id, router = _submit(service, repository, workspace, source)

    def busy(*_args: object, **_kwargs: object) -> object:
        raise DomainError(
            "job_store_busy",
            ErrorCategory.RETRYABLE_RUNTIME,
            "The workspace JobStore is busy; retry the operation",
        )

    service._execute = busy  # type: ignore[method-assign]

    with pytest.raises(DomainError, match="job_store_busy"):
        router.wait_job(job_id)

    assert router.get_job(job_id).state is JobState.RUNNING
    assert parser.calls == 0


def test_machine_lease_store_busy_does_not_fail_document_job(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    parser = _Parser()
    store = MachineResourceLeaseStore.open(tmp_path / "document-lease-store-busy")
    service, repository = _service(
        tmp_path / "document-lease-store-busy-machine",
        parser,
        IWikiPortableGateway(),
        owner_id="document-process",
        resource_lease_store=store,
        resource_owner=ResourceOwner(
            "document-workspace",
            "document-process",
            process_id=101,
        ),
    )
    job_id, router = _submit(service, repository, workspace, source)

    def busy_heartbeat(*_args: object, **_kwargs: object) -> object:
        raise DomainError(
            "machine_lease_store_busy",
            ErrorCategory.RETRYABLE_RUNTIME,
            "The machine resource lease store is busy; retry the operation",
        )

    store._heartbeat = busy_heartbeat  # type: ignore[method-assign]
    service._execute = (
        lambda *_args, **_kwargs: service._heartbeat_resource_lease()
    )

    with pytest.raises(DomainError, match="machine_lease_store_busy"):
        router.wait_job(job_id)

    assert router.get_job(job_id).state is JobState.RUNNING
    assert parser.calls == 0


def test_document_worker_consumes_adopted_resource_and_exact_job_authority(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    machine_root = tmp_path / "document-adopted-machine"
    first_service, repository = _service(
        machine_root,
        _Parser(),
        IWikiPortableGateway(),
        owner_id="submitter",
    )
    job_id, _ = _submit(first_service, repository, workspace, source)
    store = MachineResourceLeaseStore.open(tmp_path / "document-adopted-resource")
    supervisor = ResourceOwner("document-workspace", "engine-supervisor", 101)
    worker = ResourceOwner("document-workspace", "engine-worker", 202)
    source_lease = store.acquire("produce:heavy:v1", supervisor, ttl_seconds=300)
    handoff = store.handoff(source_lease, worker, ttl_seconds=300)
    adopted = store.adopt(handoff, ttl_seconds=300)
    authority = repository.claim_job(
        job_id, worker.process_instance_id, ttl_seconds=300
    ).authority
    parser = _Parser()
    service, reopened = _service(
        machine_root,
        parser,
        IWikiPortableGateway(),
        owner_id=worker.process_instance_id,
        adopted_resource_lease=adopted,
        expected_job_authority=authority,
    )
    router = JobExecutionRouter(
        reopened,
        ((service.execution_binding, service),),
    )

    assert router.wait_job(job_id).state is JobState.SUCCEEDED
    assert parser.calls == 1
    next_lease = store.acquire(
        "produce:heavy:v1",
        ResourceOwner("other-workspace", "other-worker", 303),
        ttl_seconds=300,
    )
    assert next_lease.release()


def test_document_worker_rejects_mismatched_expected_job_authority_before_parse(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    machine_root = tmp_path / "document-fenced-machine"
    first_service, repository = _service(
        machine_root,
        _Parser(),
        IWikiPortableGateway(),
        owner_id="submitter",
    )
    job_id, _ = _submit(first_service, repository, workspace, source)
    store = MachineResourceLeaseStore.open(tmp_path / "document-fenced-resource")
    supervisor = ResourceOwner("document-workspace", "engine-supervisor", 101)
    worker = ResourceOwner("document-workspace", "engine-worker", 202)
    source_lease = store.acquire("produce:heavy:v1", supervisor, ttl_seconds=300)
    adopted = store.adopt(
        store.handoff(source_lease, worker, ttl_seconds=300),
        ttl_seconds=300,
    )
    authority = repository.claim_job(
        job_id, worker.process_instance_id, ttl_seconds=300
    ).authority
    parser = _Parser()
    service, reopened = _service(
        machine_root,
        parser,
        IWikiPortableGateway(),
        owner_id=worker.process_instance_id,
        adopted_resource_lease=adopted,
        expected_job_authority=JobExecutionAuthority(
            authority.owner_id,
            authority.fencing_token + 1,
            authority.job_id,
        ),
    )
    router = JobExecutionRouter(
        reopened,
        ((service.execution_binding, service),),
    )

    with pytest.raises(DomainError, match="job_claim_fenced"):
        router.wait_job(job_id)
    assert parser.calls == 0


def test_video_blocks_document_in_another_workspace_without_starting_parser(
    tmp_path: Path,
) -> None:
    document_workspace = _workspace(tmp_path)
    video_workspace = tmp_path / "video-workspace"
    shutil.copytree(document_workspace, video_workspace)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    parser = _Parser()
    store = MachineResourceLeaseStore.open(tmp_path / "shared-machine")
    video_entered = threading.Event()
    release_video = threading.Event()

    def block_video_model(heartbeat: Callable[[], None]) -> None:
        heartbeat()
        video_entered.set()
        assert release_video.wait(timeout=5)

    from app import runtime as runtime_module

    video = runtime_module.create_fake_runtime(
        tmp_path / "video-machine",
        operation_hooks={"model": block_video_model},
        resource_lease_store=store,
        resource_owner=ResourceOwner(
            "video-workspace",
            "video-process",
            process_id=101,
        ),
    )
    video_job = video.submit_video(
        VideoProduceRequest(
            request_schema_version=1,
            workspace_root=video_workspace,
            input_value="fixture://video",
            client_request_id="video-blocks-document",
        )
    )
    service, repository = _service(
        tmp_path / "document-machine",
        parser,
        IWikiPortableGateway(),
        owner_id="document-process",
        resource_lease_store=store,
        resource_owner=ResourceOwner(
            "document-workspace",
            "document-process",
            process_id=202,
        ),
    )
    job_id, router = _submit(
        service,
        repository,
        document_workspace,
        source,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        video_wait = executor.submit(video.wait_job, video_job.job_id)
        assert video_entered.wait(timeout=5)
        try:
            with pytest.raises(DomainError, match="resource_busy"):
                router.wait_job(job_id)
            assert router.get_job(job_id).state is JobState.QUEUED
            assert parser.calls == 0
        finally:
            release_video.set()
        assert video_wait.result(timeout=15).state is JobState.SUCCEEDED

    assert router.wait_job(job_id).state is JobState.SUCCEEDED
    assert parser.calls == 1


def test_live_job_claim_keeps_new_document_job_queued(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    parser = _Parser()
    service, repository = _service(
        tmp_path / "document-scheduler-machine",
        parser,
        IWikiPortableGateway(),
        owner_id="waiting-document-process",
    )
    job_id, router = _submit(service, repository, workspace, source)
    authority = repository.claim_job(
        job_id,
        "blocking-document-process",
        ttl_seconds=300,
    ).authority

    try:
        with pytest.raises(DomainError, match="scheduler_busy"):
            router.wait_job(job_id)
        assert router.get_job(job_id).state is JobState.RUNNING
        assert parser.calls == 0
    finally:
        repository.release_job_claim(authority)


def test_blocking_document_parser_renews_machine_admission_beyond_ttl(
    tmp_path: Path,
) -> None:
    document_workspace = _workspace(tmp_path)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    machine_now_ms = [1_000]
    heartbeat_observed = threading.Event()
    store = MachineResourceLeaseStore.open(
        tmp_path / "document-heartbeat-shared-machine",
        clock=lambda: machine_now_ms[0],
    )
    competing_store = MachineResourceLeaseStore.open(
        tmp_path / "document-heartbeat-shared-machine",
        clock=lambda: machine_now_ms[0],
    )
    original_heartbeat = store._heartbeat

    def observe_heartbeat(*args: object, **kwargs: object) -> object:
        renewed = original_heartbeat(*args, **kwargs)
        if threading.current_thread() is not threading.main_thread():
            heartbeat_observed.set()
        return renewed

    store._heartbeat = observe_heartbeat  # type: ignore[method-assign]

    class BlockingParser(_Parser):
        def parse(
            self,
            source: Path,
            *,
            work_root: Path,
            cancellation_token,
        ) -> ParsedDocument:
            cancellation_token.raise_if_cancelled()
            machine_now_ms[0] = 200_000
            assert heartbeat_observed.wait(timeout=2)
            machine_now_ms[0] = 400_000
            with pytest.raises(DomainError, match="resource_busy"):
                competing_store.acquire(
                    "produce:heavy:v1",
                    ResourceOwner(
                        "video-workspace",
                        "video-process",
                        process_id=202,
                    ),
                    ttl_seconds=300,
                )
            return super().parse(
                source,
                work_root=work_root,
                cancellation_token=cancellation_token,
            )

    parser = BlockingParser()
    service, repository = _service(
        tmp_path / "document-heartbeat-machine",
        parser,
        IWikiPortableGateway(),
        owner_id="document-process",
        resource_lease_store=store,
        resource_owner=ResourceOwner(
            "document-workspace",
            "document-process",
            process_id=101,
        ),
    )
    service._checkpoint_runner.heartbeat_interval_seconds = 0.01
    job_id, router = _submit(
        service,
        repository,
        document_workspace,
        source,
    )

    assert router.wait_job(job_id).state is JobState.SUCCEEDED
    assert parser.calls == 1
    next_lease = competing_store.acquire(
        "produce:heavy:v1",
        ResourceOwner("video-workspace", "video-process", process_id=202),
        ttl_seconds=300,
    )
    assert next_lease.release()

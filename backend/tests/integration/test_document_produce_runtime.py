from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from app.adapters.documents.document_basic_pack import PACK_VERSION
from app.adapters.iwiki.portable_gateway import IWikiPortableGateway
from app.adapters.jobs.file_attempt_storage import FileAttemptStorage
from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.core.application.document_service import CHECKPOINT_SCHEMA, DocumentService
from app.core.application.job_execution_router import JobExecutionRouter
from app.core.application.produce_service import ProduceService
from app.core.domain.document import (
    DocumentBlock,
    DocumentBoundingBox,
    DocumentPage,
    ParsedDocument,
)
from app.core.domain.production import RecipeProduceResult
from app.core.jobs.model import JobState
from app.core.recipes.contracts import InputDescriptor, ProduceRequest, RecipeKey
from app.core.recipes.document.adapter import DocumentRecipeAdapter
from app.core.recipes.document.descriptor import DOCUMENT_NOTE_V1
from app.core.recipes.registry import RecipeRegistry


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "workspace-v2"


class _Parser:
    def __init__(self) -> None:
        self.calls = 0

    def parse(self, source: Path, *, work_root: Path) -> ParsedDocument:
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


class _CrashParser(_Parser):
    def parse(self, source: Path, *, work_root: Path) -> ParsedDocument:
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
) -> tuple[DocumentService, SqliteJobRepository]:
    repository = SqliteJobRepository.open(machine_root / "job-store")
    storage = FileAttemptStorage(
        machine_root / "attempts",
        repository,
        validators={
            CHECKPOINT_SCHEMA: lambda payload: isinstance(json.loads(payload), dict)
        },
    )
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
    )
    return service, repository


def _submit(
    service: DocumentService,
    repository: SqliteJobRepository,
    workspace: Path,
    source: Path,
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
        )
    )
    router = JobExecutionRouter(
        repository,
        ((service.execution_binding, service),),
    )
    return submission.job_id, router


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
    assert second.result.bundle_id != completed.result.bundle_id
    assert parser.calls == 2


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

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from iwiki.workspace import open_workspace

import app.runtime as runtime_module
from app.adapters.documents.document_basic_pack import (
    PACK_ID,
    PACK_VERSION,
    current_document_pack_platform,
)
from app.adapters.jobs.workspace_instance_registry import WorkspaceInstanceRegistry
from app.adapters.models.legacy_gpt import (
    LegacyModelBinding,
    LegacyModelCapabilities,
    LegacyModelResponse,
)
from app.core.domain.document import (
    DocumentBlock,
    DocumentBoundingBox,
    DocumentPage,
    ParsedDocument,
)
from app.core.ports.model_executor import ModelExecutionBinding
from app.core.packs.events import ExecutionPackIdentity, JobPackEnvironmentSnapshot
from app.runtime_paths import RuntimePaths


_MODEL_IDENTITY = "fixture/document-model-v1"
_PACK_ENVIRONMENT = JobPackEnvironmentSnapshot(
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


def _record(call_log: Path, operation: str) -> None:
    with call_log.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(
            json.dumps(
                {"operation": operation, "process_id": os.getpid()},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )


class _Parser:
    def __init__(self, call_log: Path) -> None:
        self._call_log = call_log

    def doctor(self) -> None:
        return None

    def parse(
        self,
        source: Path,
        *,
        work_root: Path,
        cancellation_token,
    ) -> ParsedDocument:
        del work_root
        cancellation_token.raise_if_cancelled()
        _record(self._call_log, "parse_document")
        raw = source.read_bytes()
        text = "Detached document fixture"
        return ParsedDocument(
            source_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
            source_name=source.name,
            parser_id="fixture-parser",
            parser_version="1",
            model_revision="fixture-layout-v1",
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


class _ModelBridge:
    model_identity = _MODEL_IDENTITY

    def __init__(self, call_log: Path) -> None:
        self._call_log = call_log

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
        _record(self._call_log, request.stage_id)
        if request.stage_id == "document-knowledge-compose":
            response = _compiled_response()
        elif request.stage_id == "document-knowledge-verify":
            response = _verification_response()
        else:
            raise AssertionError("unexpected Document model stage")
        return LegacyModelResponse(
            markdown=json.dumps(response),
            provider_request_id=f"{request.stage_id}-request",
            input_tokens=10,
            output_tokens=10,
            actual_model=self.model_identity,
        )


def _compiled_response() -> dict[str, object]:
    return {
        "title": {
            "text": "Detached document note",
            "source_block_ids": ["blk_title"],
        },
        "overview": [
            {
                "text": "The fixture proves detached Document execution.",
                "source_block_ids": ["blk_title"],
            }
        ],
        "sections": [
            {
                "heading": {
                    "text": "Engine handoff",
                    "source_block_ids": ["blk_title"],
                },
                "paragraphs": [
                    {
                        "text": "The Engine owns and completes the persisted Job.",
                        "source_block_ids": ["blk_title"],
                    }
                ],
                "key_points": [],
            }
        ],
    }


def _verification_response() -> dict[str, object]:
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


def create_fake_document_runtime_for_workspace(
    workspace_root: Path,
    *,
    paths: RuntimePaths,
    call_log: Path,
    require_existing_job_store: bool,
):
    registry = WorkspaceInstanceRegistry(
        paths.workspace_registry_parent,
        inspect_workspace=lambda root: open_workspace(
            root, writable=False
        ).manifest.workspace_id,
    )
    instance = registry.resolve(workspace_root)
    runtime_module.DoclingWorkerParser = lambda _config: _Parser(call_log)
    model = LegacyModelBinding(
        provider_kind="fixture",
        model_identity=_MODEL_IDENTITY,
        bridge=_ModelBridge(call_log),
        capabilities=LegacyModelCapabilities(),
    )
    binding = ModelExecutionBinding(
        schema_version=1,
        provider_type="fixture",
        model_identity=_MODEL_IDENTITY,
        credential_profile_ref="fixture/credential",
        context_window_tokens=128_000,
        max_output_tokens=4_096,
        max_concurrency=1,
        supports_structured_output=True,
        supports_temperature=True,
        timeout_seconds=60,
    )
    return runtime_module.create_document_runtime(
        instance.machine_root,
        worker_config=object(),  # type: ignore[arg-type]
        model=model,
        model_execution_binding=binding,
        model_execution_profile="default",
        pack_environment=_PACK_ENVIRONMENT,
        owner_id=f"document-fixture-{os.getpid()}",
        local_instance_id=instance.instance_id,
        workspace_instance_id=instance.instance_id,
        require_existing_job_store=require_existing_job_store,
    )


__all__ = ["create_fake_document_runtime_for_workspace"]

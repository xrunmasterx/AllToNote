from __future__ import annotations

import hashlib
import os
import stat as stat_module
from collections.abc import Mapping
from pathlib import Path

from app.core.application.document_service import DocumentService
from app.core.domain.document import (
    DocumentKnowledgeProduceRequest,
    DocumentProduceRequest,
    MAX_BORN_DIGITAL_PDF_BYTES,
)
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.model import JobExecutionOwner
from app.core.recipes.contracts import ProduceRequest, ProduceSubmission, RecipeKey


_KEY = RecipeKey("alltonote.document-note", 1)
_KNOWLEDGE_PARAMETERS_V2 = frozenset(
    {"model_override", "output_language", "provider_profile"}
)
_KNOWLEDGE_PARAMETERS_V3 = frozenset(
    {
        *_KNOWLEDGE_PARAMETERS_V2,
        "verifier_model_override",
        "verifier_provider_profile",
    }
)


def _invalid(code: str, message: str) -> DomainError:
    return DomainError(code, ErrorCategory.INVALID_REQUEST, message)


def _knowledge_selection(
    parameters: Mapping[str, object],
    service: DocumentService,
) -> tuple[int, str, str, str, str | None, str | None] | None:
    if not parameters:
        return None
    fields = frozenset(parameters)
    if fields not in {
        _KNOWLEDGE_PARAMETERS_V2,
        _KNOWLEDGE_PARAMETERS_V3,
    }:
        raise _invalid(
            "document_recipe_parameters_invalid",
            "Document knowledge parameters are invalid",
        )
    provider_profile = parameters["provider_profile"]
    model_override = parameters["model_override"]
    output_language = parameters["output_language"]
    verifier_provider_profile = parameters.get("verifier_provider_profile")
    verifier_model_override = parameters.get("verifier_model_override")
    if (
        type(provider_profile) is not str
        or not provider_profile.strip()
        or (
            model_override is not None
            and (type(model_override) is not str or not model_override.strip())
        )
        or type(output_language) is not str
        or not output_language.strip()
        or (
            fields == _KNOWLEDGE_PARAMETERS_V3
            and (
                type(verifier_provider_profile) is not str
                or not verifier_provider_profile.strip()
                or type(verifier_model_override) is not str
                or not verifier_model_override.strip()
            )
        )
    ):
        raise _invalid(
            "document_recipe_parameters_invalid",
            "Document knowledge parameters are invalid",
        )
    if model_override is None:
        model_override = service.knowledge_model_identity()
    if (
        fields == _KNOWLEDGE_PARAMETERS_V3
        and verifier_model_override == model_override
    ):
        raise _invalid(
            "document_recipe_parameters_invalid",
            "Document verifier must use an independent frozen model binding",
        )
    return (
        3 if fields == _KNOWLEDGE_PARAMETERS_V3 else 2,
        provider_profile,
        model_override,
        output_language,
        verifier_provider_profile,
        verifier_model_override,
    )


def _inspect_pdf_source(path: Path) -> tuple[str, os.stat_result]:
    if path.suffix.lower() != ".pdf":
        raise _invalid(
            "document_input_unsupported",
            "The Document Recipe requires a bounded born-digital PDF",
        )
    initial = path.stat()
    if not stat_module.S_ISREG(initial.st_mode):
        raise _invalid(
            "document_input_unsupported",
            "The Document Recipe requires a bounded born-digital PDF",
        )
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if not os.path.samestat(initial, opened):
            raise _invalid(
                "document_input_unavailable",
                "Document input is unavailable",
            )
        if (
            not stat_module.S_ISREG(opened.st_mode)
            or opened.st_size < 5
            or opened.st_size > MAX_BORN_DIGITAL_PDF_BYTES
        ):
            raise _invalid(
                "document_input_unsupported",
                "The Document Recipe requires a bounded born-digital PDF",
            )
        magic = stream.read(5)
        if magic != b"%PDF-":
            raise _invalid(
                "document_input_unsupported",
                "The Document Recipe requires a bounded born-digital PDF",
            )
        digest.update(magic)
        total_bytes = len(magic)
        while total_bytes <= MAX_BORN_DIGITAL_PDF_BYTES:
            chunk = stream.read(
                min(1024 * 1024, MAX_BORN_DIGITAL_PDF_BYTES - total_bytes + 1)
            )
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > MAX_BORN_DIGITAL_PDF_BYTES:
                raise _invalid(
                    "document_input_unsupported",
                    "The Document Recipe requires a bounded born-digital PDF",
                )
            digest.update(chunk)
        final = os.fstat(stream.fileno())
    current = path.stat()
    if (
        not os.path.samestat(opened, final)
        or not os.path.samestat(opened, current)
        or final.st_size != total_bytes
        or opened.st_size != final.st_size
        or opened.st_mtime_ns != final.st_mtime_ns
    ):
        raise _invalid(
            "document_input_unavailable",
            "Document input is unavailable",
        )
    return "sha256:" + digest.hexdigest(), final


class DocumentRecipeAdapter:
    def __init__(self, service: DocumentService) -> None:
        self._service = service

    def submit(
        self,
        request: ProduceRequest,
        *,
        execution_owner: JobExecutionOwner = JobExecutionOwner.FOREGROUND,
    ) -> ProduceSubmission:
        if not isinstance(request, ProduceRequest) or request.recipe_key != _KEY:
            raise _invalid("document_recipe_unsupported", "Document Recipe is not supported")
        if request.input.kind != "file" or request.input.attributes:
            raise _invalid(
                "document_input_invalid",
                "Document Recipe requires a file input without attributes",
            )
        if request.requested_outputs != ("knowledge-note",):
            raise _invalid(
                "document_recipe_parameters_invalid",
                "Document Recipe supports only the knowledge-note output",
            )
        selection = _knowledge_selection(request.parameters, self._service)
        try:
            source = Path(request.input.value).resolve(strict=True)
            source_sha256, stat = _inspect_pdf_source(source)
        except DomainError:
            raise
        except OSError as error:
            raise _invalid("document_input_unavailable", "Document input is unavailable") from error
        common = {
            "workspace_root": Path(request.workspace_ref),
            "input_path": source,
            "expected_source_sha256": source_sha256,
            "expected_source_size": stat.st_size,
            "expected_source_mtime_ns": stat.st_mtime_ns,
            "principal": request.principal,
            "client_request_id": request.client_request_id,
        }
        if selection is not None:
            (
                request_schema_version,
                provider_profile,
                model_override,
                output_language,
                verifier_provider_profile,
                verifier_model_override,
            ) = selection
            document_request = DocumentKnowledgeProduceRequest(
                request_schema_version=request_schema_version,
                provider_profile=provider_profile,
                model_override=model_override,
                output_language=output_language,
                verifier_provider_profile=verifier_provider_profile,
                verifier_model_override=verifier_model_override,
                **common,
            )
        else:
            document_request = DocumentProduceRequest(
                request_schema_version=1,
                **common,
            )
        snapshot = self._service.submit_document(
            document_request,
            execution_owner=execution_owner,
        )
        return ProduceSubmission(snapshot.job_id, request.recipe_key, snapshot.state)


__all__ = ["DocumentRecipeAdapter"]

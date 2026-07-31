from __future__ import annotations

import hashlib
from pathlib import Path

from app.core.application.document_service import DocumentService
from app.core.domain.document import (
    DocumentKnowledgeProduceRequest,
    DocumentProduceRequest,
)
from app.core.errors import DomainError, ErrorCategory
from app.core.recipes.contracts import ProduceRequest, ProduceSubmission, RecipeKey


_KEY = RecipeKey("alltonote.document-note", 1)
_KNOWLEDGE_PARAMETERS = frozenset(
    {"model_override", "output_language", "provider_profile"}
)


def _invalid(code: str, message: str) -> DomainError:
    return DomainError(code, ErrorCategory.INVALID_REQUEST, message)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


class DocumentRecipeAdapter:
    def __init__(self, service: DocumentService) -> None:
        self._service = service

    def submit(self, request: ProduceRequest) -> ProduceSubmission:
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
        if request.parameters and frozenset(request.parameters) != _KNOWLEDGE_PARAMETERS:
            raise _invalid(
                "document_recipe_parameters_invalid",
                "Document knowledge parameters are invalid",
            )
        try:
            source = Path(request.input.value).resolve(strict=True)
            stat = source.stat()
            source_sha256 = _sha256(source)
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
        if request.parameters:
            provider_profile = request.parameters["provider_profile"]
            model_override = request.parameters["model_override"]
            output_language = request.parameters["output_language"]
            if (
                type(provider_profile) is not str
                or not provider_profile.strip()
                or (
                    model_override is not None
                    and (
                        type(model_override) is not str
                        or not model_override.strip()
                    )
                )
                or type(output_language) is not str
                or not output_language.strip()
            ):
                raise _invalid(
                    "document_recipe_parameters_invalid",
                    "Document knowledge parameters are invalid",
                )
            if model_override is None:
                model_override = self._service.knowledge_model_identity()
            document_request = DocumentKnowledgeProduceRequest(
                request_schema_version=2,
                provider_profile=provider_profile,
                model_override=model_override,
                output_language=output_language,
                **common,
            )
        else:
            document_request = DocumentProduceRequest(
                request_schema_version=1,
                **common,
            )
        snapshot = self._service.submit_document(document_request)
        return ProduceSubmission(snapshot.job_id, request.recipe_key, snapshot.state)


__all__ = ["DocumentRecipeAdapter"]

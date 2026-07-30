from __future__ import annotations

import hashlib
from pathlib import Path

from app.core.application.document_service import DocumentService
from app.core.domain.document import DocumentProduceRequest
from app.core.errors import DomainError, ErrorCategory
from app.core.recipes.contracts import ProduceRequest, ProduceSubmission, RecipeKey


_KEY = RecipeKey("alltonote.document-note", 1)


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
        if request.requested_outputs != ("knowledge-note",) or request.parameters:
            raise _invalid(
                "document_recipe_parameters_invalid",
                "The first Document slice has no optional output or parameters",
            )
        try:
            source = Path(request.input.value).resolve(strict=True)
            stat = source.stat()
            source_sha256 = _sha256(source)
        except OSError as error:
            raise _invalid("document_input_unavailable", "Document input is unavailable") from error
        document_request = DocumentProduceRequest(
            request_schema_version=1,
            workspace_root=Path(request.workspace_ref),
            input_path=source,
            expected_source_sha256=source_sha256,
            expected_source_size=stat.st_size,
            expected_source_mtime_ns=stat.st_mtime_ns,
            principal=request.principal,
            client_request_id=request.client_request_id,
        )
        snapshot = self._service.submit_document(document_request)
        return ProduceSubmission(snapshot.job_id, request.recipe_key, snapshot.state)


__all__ = ["DocumentRecipeAdapter"]

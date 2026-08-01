from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from app.core.errors import DomainError, ErrorCategory


_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
MAX_BORN_DIGITAL_PDF_BYTES = 64 * 1024 * 1024
DOCUMENT_INPUT_SNAPSHOT_EVENT = "document.input-snapshot.v1"


def _invalid(message: str) -> DomainError:
    return DomainError("document_parse_result_invalid", ErrorCategory.INTERNAL, message)


@dataclass(frozen=True, slots=True)
class DocumentBoundingBox:
    left: float
    top: float
    right: float
    bottom: float
    origin: str = "top-left"

    def __post_init__(self) -> None:
        if (
            self.origin != "top-left"
            or any(type(value) not in (int, float) for value in (self.left, self.top, self.right, self.bottom))
            or any(
                not math.isfinite(value)
                for value in (self.left, self.top, self.right, self.bottom)
            )
            or self.left >= self.right
            or self.top >= self.bottom
        ):
            raise _invalid("Document bounding box is invalid")


@dataclass(frozen=True, slots=True)
class DocumentBlock:
    block_id: str
    page_number: int
    reading_order: int
    kind: str
    text: str
    content_sha256: str
    bbox: DocumentBoundingBox
    basis: str = "native"

    def __post_init__(self) -> None:
        if (
            type(self.block_id) is not str
            or not self.block_id
            or type(self.page_number) is not int
            or self.page_number < 1
            or type(self.reading_order) is not int
            or self.reading_order < 0
            or type(self.kind) is not str
            or not self.kind
            or type(self.text) is not str
            or not self.text.strip()
            or _SHA256.fullmatch(self.content_sha256) is None
            or not isinstance(self.bbox, DocumentBoundingBox)
            or self.basis != "native"
        ):
            raise _invalid("Document block is invalid")


@dataclass(frozen=True, slots=True)
class DocumentPage:
    page_number: int
    width: float
    height: float
    blocks: tuple[DocumentBlock, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "blocks", tuple(self.blocks))
        if (
            type(self.page_number) is not int
            or self.page_number < 1
            or type(self.width) not in (int, float)
            or self.width <= 0
            or type(self.height) not in (int, float)
            or self.height <= 0
            or not math.isfinite(self.width)
            or not math.isfinite(self.height)
            or any(block.page_number != self.page_number for block in self.blocks)
            or tuple(block.reading_order for block in self.blocks)
            != tuple(range(len(self.blocks)))
            or any(
                block.bbox.left < 0
                or block.bbox.top < 0
                or block.bbox.right > self.width + 0.01
                or block.bbox.bottom > self.height + 0.01
                for block in self.blocks
            )
        ):
            raise _invalid("Document page is invalid")


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    source_sha256: str
    source_name: str
    parser_id: str
    parser_version: str
    model_revision: str
    pages: tuple[DocumentPage, ...]
    metadata: Mapping[str, str]
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "pages", tuple(self.pages))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        if (
            _SHA256.fullmatch(self.source_sha256) is None
            or not self.pages
            or any(
                type(value) is not str or not value.strip()
                for value in (
                    self.source_name,
                    self.parser_id,
                    self.parser_version,
                    self.model_revision,
                )
            )
            or tuple(page.page_number for page in self.pages)
            != tuple(range(1, len(self.pages) + 1))
            or any(
                type(key) is not str
                or type(value) is not str
                for key, value in self.metadata.items()
            )
            or any(type(value) is not str or not value.strip() for value in self.warnings)
            or len(
                {
                    block.block_id
                    for page in self.pages
                    for block in page.blocks
                }
            )
            != sum(len(page.blocks) for page in self.pages)
        ):
            raise _invalid("Parsed document is invalid")


@dataclass(frozen=True, slots=True)
class DocumentProduceRequest:
    request_schema_version: int
    workspace_root: Path
    input_path: Path
    expected_source_sha256: str
    expected_source_size: int
    expected_source_mtime_ns: int
    recipe_id: str = "alltonote.document-note"
    recipe_version: int = 1
    principal: str = "local-user"
    client_request_id: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.request_schema_version) is not int
            or self.request_schema_version != 1
            or type(self.recipe_id) is not str
            or not self.recipe_id
            or type(self.recipe_version) is not int
            or self.recipe_version != 1
            or type(self.principal) is not str
            or not self.principal
            or _SHA256.fullmatch(self.expected_source_sha256) is None
            or type(self.expected_source_size) is not int
            or self.expected_source_size < 5
            or type(self.expected_source_mtime_ns) is not int
            or self.expected_source_mtime_ns < 0
            or (
                self.client_request_id is not None
                and (type(self.client_request_id) is not str or not self.client_request_id)
            )
        ):
            raise DomainError(
                "document_produce_request_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Document production request is invalid",
            )
        object.__setattr__(self, "workspace_root", Path(self.workspace_root))
        object.__setattr__(self, "input_path", Path(self.input_path))


@dataclass(frozen=True, slots=True)
class DocumentKnowledgeProduceRequest:
    request_schema_version: int
    workspace_root: Path
    input_path: Path
    expected_source_sha256: str
    expected_source_size: int
    expected_source_mtime_ns: int
    provider_profile: str
    model_override: str | None
    output_language: str
    verifier_provider_profile: str | None = None
    verifier_model_override: str | None = None
    recipe_id: str = "alltonote.document-note"
    recipe_version: int = 1
    principal: str = "local-user"
    client_request_id: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.request_schema_version) is not int
            or self.request_schema_version not in (2, 3)
            or type(self.recipe_id) is not str
            or not self.recipe_id
            or type(self.recipe_version) is not int
            or self.recipe_version != 1
            or type(self.principal) is not str
            or not self.principal
            or _SHA256.fullmatch(self.expected_source_sha256) is None
            or type(self.expected_source_size) is not int
            or self.expected_source_size < 5
            or type(self.expected_source_mtime_ns) is not int
            or self.expected_source_mtime_ns < 0
            or type(self.provider_profile) is not str
            or not self.provider_profile.strip()
            or type(self.model_override) is not str
            or not self.model_override.strip()
            or type(self.output_language) is not str
            or not self.output_language.strip()
            or (
                self.request_schema_version == 2
                and (
                    self.verifier_provider_profile is not None
                    or self.verifier_model_override is not None
                )
            )
            or (
                self.request_schema_version == 3
                and (
                    type(self.verifier_provider_profile) is not str
                    or not self.verifier_provider_profile.strip()
                    or type(self.verifier_model_override) is not str
                    or not self.verifier_model_override.strip()
                    or self.verifier_model_override == self.model_override
                )
            )
            or (
                self.client_request_id is not None
                and (type(self.client_request_id) is not str or not self.client_request_id)
            )
        ):
            raise DomainError(
                "document_produce_request_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Document production request is invalid",
            )
        object.__setattr__(self, "workspace_root", Path(self.workspace_root))
        object.__setattr__(self, "input_path", Path(self.input_path))


__all__ = [
    "DocumentBlock",
    "DocumentBoundingBox",
    "DocumentPage",
    "DocumentKnowledgeProduceRequest",
    "DocumentProduceRequest",
    "MAX_BORN_DIGITAL_PDF_BYTES",
    "ParsedDocument",
]

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from app.core.domain.document import (
    DocumentBlock,
    DocumentBoundingBox,
    DocumentPage,
    ParsedDocument,
)
from app.core.errors import DomainError, ErrorCategory


def _invalid() -> DomainError:
    return DomainError(
        "checkpoint_content_invalid",
        ErrorCategory.INTERNAL,
        "Document checkpoint content is invalid",
    )


def encode_parsed_document(parsed: ParsedDocument) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "source_sha256": parsed.source_sha256,
            "source_name": parsed.source_name,
            "parser_id": parsed.parser_id,
            "parser_version": parsed.parser_version,
            "model_revision": parsed.model_revision,
            "pages": [
                {
                    "page_number": page.page_number,
                    "width": page.width,
                    "height": page.height,
                    "blocks": [
                        {
                            "block_id": block.block_id,
                            "page_number": block.page_number,
                            "reading_order": block.reading_order,
                            "kind": block.kind,
                            "text": block.text,
                            "content_sha256": block.content_sha256,
                            "bbox": {
                                "left": block.bbox.left,
                                "top": block.bbox.top,
                                "right": block.bbox.right,
                                "bottom": block.bbox.bottom,
                                "origin": block.bbox.origin,
                            },
                            "basis": block.basis,
                        }
                        for block in page.blocks
                    ],
                }
                for page in parsed.pages
            ],
            "metadata": dict(parsed.metadata),
            "warnings": list(parsed.warnings),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def decode_parsed_document(payload: bytes) -> ParsedDocument:
    try:
        value = json.loads(payload)
        expected_fields = frozenset(
            {
                "schema_version",
                "source_sha256",
                "source_name",
                "parser_id",
                "parser_version",
                "model_revision",
                "pages",
                "metadata",
                "warnings",
            }
        )
        if (
            type(value) is not dict
            or frozenset(value) != expected_fields
            or value.get("schema_version") != 1
        ):
            raise TypeError
        return ParsedDocument(
            source_sha256=value["source_sha256"],
            source_name=value["source_name"],
            parser_id=value["parser_id"],
            parser_version=value["parser_version"],
            model_revision=value["model_revision"],
            pages=tuple(
                DocumentPage(
                    page_number=page["page_number"],
                    width=page["width"],
                    height=page["height"],
                    blocks=tuple(
                        DocumentBlock(
                            block_id=block["block_id"],
                            page_number=block["page_number"],
                            reading_order=block["reading_order"],
                            kind=block["kind"],
                            text=block["text"],
                            content_sha256=block["content_sha256"],
                            bbox=DocumentBoundingBox(**block["bbox"]),
                            basis=block["basis"],
                        )
                        for block in page["blocks"]
                    ),
                )
                for page in value["pages"]
            ),
            metadata=value["metadata"],
            warnings=tuple(value["warnings"]),
        )
    except (DomainError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise _invalid() from None


@dataclass(frozen=True, slots=True)
class DocumentCandidateCheckpoint:
    staging_relative_path: str
    bundle_id: str
    manifest_sha256: str
    run_id: str
    source_id: str
    source_revision_id: str
    artifacts: Mapping[str, str]
    quality_overall: str
    publish_eligible: bool
    usage: Mapping[str, int]
    warnings: tuple[str, ...]
    source_identity_connector_id: str | None = None
    source_canonical_identity: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifacts", MappingProxyType(dict(self.artifacts)))
        object.__setattr__(self, "usage", MappingProxyType(dict(self.usage)))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        if (self.source_identity_connector_id is None) != (
            self.source_canonical_identity is None
        ) or (
            self.source_identity_connector_id is not None
            and (
                type(self.source_identity_connector_id) is not str
                or not self.source_identity_connector_id
                or type(self.source_canonical_identity) is not str
                or not self.source_canonical_identity
            )
        ):
            raise _invalid()

    def encode(self) -> bytes:
        identity = (
            {}
            if self.source_identity_connector_id is None
            else {
                "source_identity_connector_id": self.source_identity_connector_id,
                "source_canonical_identity": self.source_canonical_identity,
            }
        )
        return json.dumps(
            {
                "schema_version": 1 if not identity else 2,
                "staging_relative_path": self.staging_relative_path,
                "bundle_id": self.bundle_id,
                "manifest_sha256": self.manifest_sha256,
                "run_id": self.run_id,
                "source_id": self.source_id,
                "source_revision_id": self.source_revision_id,
                "artifacts": dict(self.artifacts),
                "quality_overall": self.quality_overall,
                "publish_eligible": self.publish_eligible,
                "usage": dict(self.usage),
                "warnings": list(self.warnings),
                **identity,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")

    @classmethod
    def decode(cls, payload: bytes) -> "DocumentCandidateCheckpoint":
        try:
            value = json.loads(payload)
            version_one_fields = frozenset(
                {
                    "schema_version",
                    "staging_relative_path",
                    "bundle_id",
                    "manifest_sha256",
                    "run_id",
                    "source_id",
                    "source_revision_id",
                    "artifacts",
                    "quality_overall",
                    "publish_eligible",
                    "usage",
                    "warnings",
                }
            )
            version_two_fields = version_one_fields | {
                "source_identity_connector_id",
                "source_canonical_identity",
            }
            schema_version = (
                value.get("schema_version") if type(value) is dict else None
            )
            if (
                type(value) is not dict
                or (
                    schema_version == 1
                    and frozenset(value) != version_one_fields
                )
                or (
                    schema_version == 2
                    and frozenset(value) != version_two_fields
                )
                or schema_version not in {1, 2}
            ):
                raise TypeError
            return cls(
                staging_relative_path=value["staging_relative_path"],
                bundle_id=value["bundle_id"],
                manifest_sha256=value["manifest_sha256"],
                run_id=value["run_id"],
                source_id=value["source_id"],
                source_revision_id=value["source_revision_id"],
                artifacts=value["artifacts"],
                quality_overall=value["quality_overall"],
                publish_eligible=value["publish_eligible"],
                usage=value["usage"],
                warnings=tuple(value["warnings"]),
                source_identity_connector_id=value.get(
                    "source_identity_connector_id"
                ),
                source_canonical_identity=value.get("source_canonical_identity"),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise _invalid() from None


__all__ = [
    "DocumentCandidateCheckpoint",
    "decode_parsed_document",
    "encode_parsed_document",
]

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

from app.core.errors import DomainError, ErrorCategory


_ARTIFACT_ID_PATTERN = re.compile(
    r"art_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)


class QualityOverall(StrEnum):
    PASS = "pass"
    PASS_WITH_WARNINGS = "pass_with_warnings"
    FAIL = "fail"


class VideoDocumentKind(StrEnum):
    KNOWLEDGE_NOTE = "knowledge-note"
    FAITHFUL_EDITION = "faithful-edition"


@dataclass(frozen=True)
class VideoProducedDocument:
    document_kind: VideoDocumentKind
    draft_artifact_id: str
    quality_report_artifact_id: str
    quality_overall: QualityOverall
    publish_eligible: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.document_kind, VideoDocumentKind)
            or type(self.draft_artifact_id) is not str
            or _ARTIFACT_ID_PATTERN.fullmatch(self.draft_artifact_id) is None
            or type(self.quality_report_artifact_id) is not str
            or _ARTIFACT_ID_PATTERN.fullmatch(self.quality_report_artifact_id) is None
            or self.draft_artifact_id == self.quality_report_artifact_id
            or not isinstance(self.quality_overall, QualityOverall)
            or type(self.publish_eligible) is not bool
        ):
            raise DomainError(
                "video_result_document_invalid",
                ErrorCategory.INTERNAL,
                "Video result document is invalid",
            )


@dataclass(frozen=True)
class VideoProduceResult:
    job_id: str
    run_id: str
    bundle_id: str
    manifest_sha256: str
    commit_sha256: str
    workspace_relative_bundle_path: str
    source_id: str
    source_revision_id: str
    primary_draft_artifact_id: str
    transcript_artifact_id: str
    evidence_set_artifact_id: str
    quality_report_artifact_id: str
    display_asset_ids: tuple[str, ...]
    quality_overall: QualityOverall
    publish_eligible: bool
    usage: Mapping[str, int | float | str]
    warnings: tuple[str, ...]
    idempotent: bool
    documents: tuple[VideoProducedDocument, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "display_asset_ids", tuple(self.display_asset_ids))
        object.__setattr__(self, "usage", MappingProxyType(dict(self.usage)))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        documents = tuple(self.documents)
        if (
            any(not isinstance(document, VideoProducedDocument) for document in documents)
            or len({document.document_kind for document in documents}) != len(documents)
            or len({document.draft_artifact_id for document in documents}) != len(documents)
            or len({document.quality_report_artifact_id for document in documents})
            != len(documents)
        ):
            raise DomainError(
                "video_result_document_invalid",
                ErrorCategory.INTERNAL,
                "Video result documents are invalid",
            )
        if documents:
            primary = next(
                (
                    document
                    for document in documents
                    if document.draft_artifact_id == self.primary_draft_artifact_id
                ),
                None,
            )
            if (
                primary is None
                or primary.quality_report_artifact_id
                != self.quality_report_artifact_id
                or primary.quality_overall is not self.quality_overall
                or primary.publish_eligible is not self.publish_eligible
            ):
                raise DomainError(
                    "video_result_document_invalid",
                    ErrorCategory.INTERNAL,
                    "Primary result projection is inconsistent",
                )
        object.__setattr__(self, "documents", documents)


__all__ = [
    "QualityOverall",
    "VideoDocumentKind",
    "VideoProducedDocument",
    "VideoProduceResult",
]

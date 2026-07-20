from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class PortableArtifactQueryRecord:
    artifact_id: str
    kind: str
    media_type: str
    charset: str | None
    size_bytes: int
    sha256: str
    created_at: str
    source_revision_ids: tuple[str, ...]
    parent_artifact_ids: tuple[str, ...]
    quality_report_ids: tuple[str, ...]
    recipe_id: str
    recipe_version: int
    compiler_identity: str
    document_kind: str | None
    primary: bool


@dataclass(frozen=True)
class PortableSourceQueryRecord:
    source_id: str
    kind: str
    connector_id: str | None
    platform: str | None


@dataclass(frozen=True)
class PortableQualityQueryRecord:
    overall: str
    publish_eligible: bool
    repair_attempts: int


@dataclass(frozen=True)
class PortableBundleQueryRecord:
    bundle_id: str
    manifest_sha256: str
    created_at: str
    producer_product: str
    runtime_version: str
    recipe_id: str
    recipe_version: int
    capability: str
    portable_contract_id: str
    artifact_count: int
    primary_draft_artifact_id: str | None
    draft_artifact_ids: tuple[str, ...]
    transcript_artifact_id: str | None
    evidence_set_artifact_id: str | None


@dataclass(frozen=True)
class PortableInspectionRecord:
    bundle: PortableBundleQueryRecord
    artifacts: tuple[PortableArtifactQueryRecord, ...]
    artifacts_truncated: bool
    target_artifact: PortableArtifactQueryRecord | None
    sources: tuple[PortableSourceQueryRecord, ...]
    source_revision_ids: tuple[str, ...]
    quality: PortableQualityQueryRecord
    payload: bytes | None = None
    payload_truncated: bool = False


class PortableInspectionPort(Protocol):
    def inspect_committed(
        self,
        workspace_root: Path,
        target_id: str,
        *,
        payload_limit: int | None = None,
    ) -> PortableInspectionRecord: ...


__all__ = [
    "PortableArtifactQueryRecord",
    "PortableBundleQueryRecord",
    "PortableInspectionPort",
    "PortableInspectionRecord",
    "PortableQualityQueryRecord",
    "PortableSourceQueryRecord",
]

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class CandidateBundleLocation:
    workspace_root: Path
    candidate_path: Path
    staging_relative_path: str
    target_area: str


class CandidateBundleWriterPort(Protocol):
    def write_payload(self, relative_path: str, data: bytes) -> None: ...

    def complete(self, manifest: bytes) -> CandidateBundleLocation: ...

    def close(self) -> None:
        """Release resources; this may raise, but an active primary error wins."""
        ...


class CandidateLocationCapabilityPort(Protocol):
    def begin(self, job_id: str) -> CandidateBundleWriterPort: ...


class PortableValidationReportPort(Protocol):
    valid: bool
    bundle_id: str | None
    manifest_sha256: str | None


class PortableCommitResultPort(Protocol):
    bundle_id: str
    manifest_sha256: str
    commit_sha256: str
    relative_path: str
    idempotent: bool


class PortableWorkspacePort(Protocol):
    """Boundary for iwiki inspect, validation, prepare, and commit operations."""

    def inspect(self, workspace_root: Path) -> object: ...

    def candidate_location(
        self,
        workspace_root: Path,
        *,
        local_instance_id: str,
        nonce: str,
    ) -> CandidateLocationCapabilityPort: ...

    def validate_candidate(
        self,
        workspace_root: Path,
        staging_relative_path: str,
    ) -> PortableValidationReportPort: ...

    def prepare_candidate(
        self,
        workspace_root: Path,
        staging_relative_path: str,
        *,
        expected_bundle_id: str,
        expected_manifest_sha256: str,
    ) -> object: ...

    def commit_prepared(self, prepared: object) -> PortableCommitResultPort: ...

    def discard_prepared(self, prepared: object) -> None: ...


__all__ = [
    "CandidateBundleLocation",
    "CandidateBundleWriterPort",
    "CandidateLocationCapabilityPort",
    "PortableCommitResultPort",
    "PortableValidationReportPort",
    "PortableWorkspacePort",
]

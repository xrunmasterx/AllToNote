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


class PortableWorkspacePort(Protocol):
    """Boundary for iwiki inspect, validation, prepare, and commit operations."""


__all__ = [
    "CandidateBundleLocation",
    "CandidateBundleWriterPort",
    "CandidateLocationCapabilityPort",
    "PortableWorkspacePort",
]

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from app.core.errors import DomainError, ErrorCategory


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TYPED_ID = re.compile(
    r"(?P<prefix>[a-z]+)_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)


def _invalid(message: str) -> DomainError:
    return DomainError("job_result_invalid", ErrorCategory.INTERNAL, message)


def _has_prefix(value: object, prefix: str) -> bool:
    match = _TYPED_ID.fullmatch(value) if type(value) is str else None
    return match is not None and match.group("prefix") == prefix


@dataclass(frozen=True, slots=True)
class RecipeProduceResult:
    result_schema_version: int
    result_kind: str
    job_id: str
    run_id: str
    bundle_id: str
    manifest_sha256: str
    commit_sha256: str
    workspace_relative_bundle_path: str
    source_id: str
    source_revision_id: str
    artifacts: Mapping[str, str]
    quality_overall: str
    publish_eligible: bool
    usage: Mapping[str, int | float | str]
    warnings: tuple[str, ...]
    idempotent: bool

    def __post_init__(self) -> None:
        artifacts = dict(self.artifacts)
        usage = dict(self.usage)
        warnings = tuple(self.warnings)
        if (
            self.result_schema_version != 1
            or type(self.result_kind) is not str
            or not self.result_kind.strip()
            or not _has_prefix(self.job_id, "job")
            or not _has_prefix(self.run_id, "run")
            or not _has_prefix(self.bundle_id, "bnd")
            or _DIGEST.fullmatch(self.manifest_sha256) is None
            or _DIGEST.fullmatch(self.commit_sha256) is None
            or type(self.workspace_relative_bundle_path) is not str
            or not self.workspace_relative_bundle_path.strip()
            or not _has_prefix(self.source_id, "src")
            or not _has_prefix(self.source_revision_id, "rev")
            or not artifacts
            or any(
                type(role) is not str
                or not role.strip()
                or not _has_prefix(artifact_id, "art")
                for role, artifact_id in artifacts.items()
            )
            or type(self.quality_overall) is not str
            or not self.quality_overall.strip()
            or type(self.publish_eligible) is not bool
            or any(
                type(key) is not str
                or not key.strip()
                or type(value) not in (int, float, str)
                for key, value in usage.items()
            )
            or any(type(warning) is not str or not warning.strip() for warning in warnings)
            or type(self.idempotent) is not bool
        ):
            raise _invalid("Stored Recipe result is invalid")
        object.__setattr__(self, "artifacts", MappingProxyType(artifacts))
        object.__setattr__(self, "usage", MappingProxyType(usage))
        object.__setattr__(self, "warnings", warnings)


__all__ = ["RecipeProduceResult"]

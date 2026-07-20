from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping


CLI_PROTOCOL_VERSION = 1


@dataclass(frozen=True)
class CliError:
    code: str
    category: str
    message: str
    retryable: bool
    next_actions: tuple[str, ...] = ()
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "next_actions", tuple(self.next_actions))
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


@dataclass(frozen=True)
class ApplicationResult:
    command: str
    correlation_id: str
    ok: bool
    data: Mapping[str, object] = field(default_factory=dict)
    error: CliError | None = None
    warnings: tuple[str, ...] = ()
    job: Mapping[str, object] | None = None
    artifacts: tuple[Mapping[str, object], ...] = ()
    capabilities: tuple[Mapping[str, object], ...] = ()
    versions: Mapping[str, object] = field(default_factory=dict)
    human_lines: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.ok == (self.error is not None):
            raise ValueError("Application result success and error fields disagree")
        object.__setattr__(self, "data", MappingProxyType(dict(self.data)))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(
            self,
            "job",
            MappingProxyType(dict(self.job)) if self.job is not None else None,
        )
        object.__setattr__(
            self,
            "artifacts",
            tuple(MappingProxyType(dict(item)) for item in self.artifacts),
        )
        object.__setattr__(
            self,
            "capabilities",
            tuple(MappingProxyType(dict(item)) for item in self.capabilities),
        )
        object.__setattr__(self, "versions", MappingProxyType(dict(self.versions)))
        object.__setattr__(self, "human_lines", tuple(self.human_lines))


__all__ = ["ApplicationResult", "CLI_PROTOCOL_VERSION", "CliError"]

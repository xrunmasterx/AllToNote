from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class ErrorCategory(StrEnum):
    INVALID_REQUEST = "invalid_request"
    WORKSPACE_INCOMPATIBLE = "workspace_incompatible"
    CONFLICT = "conflict"
    RETRYABLE_RUNTIME = "retryable_runtime"
    POLICY_DENIED = "policy_denied"
    RECIPE_FAILED = "recipe_failed"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


def _freeze_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_value(item) for item in value)
    return value


def immutable_details(details: Mapping[str, object] | None) -> Mapping[str, object]:
    return MappingProxyType(
        {key: _freeze_value(value) for key, value in (details or {}).items()}
    )


@dataclass(frozen=True)
class ErrorDetail:
    code: str
    category: ErrorCategory
    message: str
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", immutable_details(self.details))


class DomainError(Exception):
    def __init__(
        self,
        code: str,
        category: ErrorCategory,
        message: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self.code = code
        self.category = category
        self.message = message
        self.details = immutable_details(details)
        super().__init__(f"{code}: {message}")

    def __repr__(self) -> str:
        return (
            f"DomainError(code={self.code!r}, category={self.category!r}, "
            f"message={self.message!r})"
        )

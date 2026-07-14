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


_IMMUTABLE_DETAIL_SCALAR_TYPES = frozenset({bool, int, float, str, bytes})


def _freeze_mapping(value: Mapping[object, object]) -> Mapping[str, object]:
    frozen: dict[str, object] = {}
    for key, item in value.items():
        if type(key) is not str:
            raise TypeError("Error detail mapping keys must be strings")
        frozen[key] = _freeze_value(item)
    return MappingProxyType(frozen)


def _freeze_value(value: object) -> object:
    if value is None or type(value) in _IMMUTABLE_DETAIL_SCALAR_TYPES:
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_value(item) for item in value)
    raise TypeError("Error detail value type is not supported")


def immutable_details(details: Mapping[str, object] | None) -> Mapping[str, object]:
    return _freeze_mapping(details or {})


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

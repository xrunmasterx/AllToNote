from __future__ import annotations

import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Protocol

from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.model import JobState


def _error(code: str, message: str) -> DomainError:
    return DomainError(code, ErrorCategory.INVALID_REQUEST, message)


def _require_text(value: object, code: str, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise _error(code, f"{field_name} must be a non-empty string")
    return value


def _require_version(value: object, code: str, field_name: str) -> int:
    if type(value) is not int or value < 1:
        raise _error(code, f"{field_name} must be a positive integer")
    return value


def _freeze_sequence(value: object, code: str, field_name: str) -> tuple[str, ...]:
    if type(value) not in (list, tuple):
        raise _error(code, f"{field_name} must be a string sequence")
    items = tuple(value)
    if any(type(item) is not str or not item.strip() for item in items):
        raise _error(code, f"{field_name} must contain non-empty strings")
    return items


def _freeze_json(value: object, active: set[int] | None = None) -> object:
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _error("recipe_json_invalid", "Recipe JSON contains a non-finite number")
        return value
    if type(value) not in (dict, list):
        raise _error("recipe_json_invalid", "Recipe JSON contains an unsupported value")

    active = set() if active is None else active
    identity = id(value)
    if identity in active:
        raise _error("recipe_json_invalid", "Recipe JSON contains a cycle")
    active.add(identity)
    try:
        if type(value) is list:
            return tuple(_freeze_json(item, active) for item in value)
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise _error("recipe_json_invalid", "Recipe JSON keys must be strings")
            frozen[key] = _freeze_json(item, active)
        return MappingProxyType(frozen)
    except RecursionError as exc:
        raise _error("recipe_json_invalid", "Recipe JSON is too deeply nested") from exc
    finally:
        active.remove(identity)


def _freeze_json_mapping(value: object) -> Mapping[str, object]:
    if type(value) is not dict:
        raise _error("recipe_json_invalid", "Recipe JSON mapping must be an object")
    frozen = _freeze_json(value)
    assert isinstance(frozen, Mapping)
    return frozen


@dataclass(frozen=True, slots=True)
class RecipeKey:
    recipe_id: str
    recipe_version: int

    def __post_init__(self) -> None:
        _require_text(self.recipe_id, "recipe_key_invalid", "recipe_id")
        _require_version(self.recipe_version, "recipe_key_invalid", "recipe_version")


@dataclass(frozen=True, slots=True)
class RecipeDescriptor:
    key: RecipeKey
    display_name: str
    input_kinds: tuple[str, ...]
    output_kinds: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.key, RecipeKey):
            raise _error("recipe_descriptor_invalid", "key must be a RecipeKey")
        _require_text(self.display_name, "recipe_descriptor_invalid", "display_name")
        object.__setattr__(self, "input_kinds", _freeze_sequence(self.input_kinds, "recipe_descriptor_invalid", "input_kinds"))
        object.__setattr__(self, "output_kinds", _freeze_sequence(self.output_kinds, "recipe_descriptor_invalid", "output_kinds"))


@dataclass(frozen=True, slots=True)
class InputDescriptor:
    kind: str
    value: str = field(repr=False)
    attributes: Mapping[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        _require_text(self.kind, "input_descriptor_invalid", "kind")
        _require_text(self.value, "input_descriptor_invalid", "value")
        object.__setattr__(self, "attributes", _freeze_json_mapping(dict(self.attributes)))


@dataclass(frozen=True, slots=True)
class ProduceRequest:
    contract_version: int
    recipe_key: RecipeKey
    input: InputDescriptor
    workspace_ref: str
    requested_outputs: tuple[str, ...]
    parameters: Mapping[str, object] = field(default_factory=dict, repr=False)
    principal: str = "local-user"
    client_request_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.contract_version) is not int or self.contract_version != 1:
            raise _error("recipe_contract_version_unsupported", "Only Recipe contract version 1 is supported")
        if not isinstance(self.recipe_key, RecipeKey) or not isinstance(self.input, InputDescriptor):
            raise _error("produce_request_invalid", "Recipe key and input are required")
        _require_text(self.workspace_ref, "produce_request_invalid", "workspace_ref")
        _require_text(self.principal, "produce_request_invalid", "principal")
        if self.client_request_id is not None:
            _require_text(self.client_request_id, "produce_request_invalid", "client_request_id")
        object.__setattr__(self, "requested_outputs", _freeze_sequence(self.requested_outputs, "produce_request_invalid", "requested_outputs"))
        object.__setattr__(self, "parameters", _freeze_json_mapping(dict(self.parameters)))


@dataclass(frozen=True, slots=True)
class ProduceSubmission:
    job_id: str
    recipe_key: RecipeKey
    state: JobState

    def __post_init__(self) -> None:
        _require_text(self.job_id, "produce_submission_invalid", "job_id")
        if not isinstance(self.recipe_key, RecipeKey) or not isinstance(self.state, JobState):
            raise _error("produce_submission_invalid", "Recipe key and Job state are required")


class RecipeEndpoint(Protocol):
    def submit(self, request: ProduceRequest) -> ProduceSubmission: ...


__all__ = [
    "InputDescriptor",
    "ProduceRequest",
    "ProduceSubmission",
    "RecipeDescriptor",
    "RecipeEndpoint",
    "RecipeKey",
]

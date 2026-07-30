from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Mapping

from app.core.errors import DomainError, ErrorCategory
from app.core.recipes.contracts import InputDescriptor, ProduceRequest, RecipeKey
from app.core.sensitive_identifiers import is_sensitive_identifier


_MAX_REQUEST_BYTES = 1_048_576
_SELECTOR = re.compile(r"(?P<recipe_id>[^@\s]+)@(?P<version>[1-9][0-9]*)\Z")
_TOP_LEVEL_FIELDS = frozenset(
    {
        "contract_version",
        "recipe_key",
        "input",
        "workspace_ref",
        "requested_outputs",
        "parameters",
        "principal",
        "client_request_id",
    }
)
_RECIPE_KEY_FIELDS = frozenset({"recipe_id", "recipe_version"})
_INPUT_FIELDS = frozenset({"kind", "value", "attributes"})


def _error(code: str, message: str) -> DomainError:
    return DomainError(code, ErrorCategory.INVALID_REQUEST, message)


def parse_recipe_selector(value: str) -> RecipeKey:
    if type(value) is not str:
        raise _error("recipe_selector_invalid", "Recipe selector is invalid")
    matched = _SELECTOR.fullmatch(value)
    if matched is None:
        raise _error("recipe_selector_invalid", "Recipe selector is invalid")
    return RecipeKey(matched.group("recipe_id"), int(matched.group("version")))


def _object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _error("produce_request_file_invalid", "Produce request file is invalid")
        value[key] = item
    return value


def _reject_constant(_value: str) -> object:
    raise _error("produce_request_file_invalid", "Produce request file is invalid")


def _mapping(value: object, fields: frozenset[str]) -> Mapping[str, object]:
    if type(value) is not dict:
        raise _error("produce_request_file_invalid", "Produce request file is invalid")
    unknown = set(value) - fields
    if unknown:
        raise _error("produce_request_field_unknown", "Produce request contains an unknown field")
    return value


def _contains_sensitive_key(value: object) -> bool:
    if type(value) is dict:
        return any(
            is_sensitive_identifier(key) or _contains_sensitive_key(item)
            for key, item in value.items()
        )
    if type(value) is list:
        return any(_contains_sensitive_key(item) for item in value)
    return False


def load_produce_request(path: Path) -> ProduceRequest:
    try:
        if path.stat().st_size > _MAX_REQUEST_BYTES:
            raise _error("produce_request_file_invalid", "Produce request file is invalid")
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_object_pairs,
            parse_constant=_reject_constant,
        )
    except DomainError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise _error("produce_request_file_invalid", "Produce request file is invalid") from None

    root = _mapping(value, _TOP_LEVEL_FIELDS)
    if root.get("contract_version") != 1:
        raise _error(
            "produce_request_contract_unsupported",
            "Produce request contract is unsupported",
        )
    recipe = _mapping(root.get("recipe_key"), _RECIPE_KEY_FIELDS)
    input_value = _mapping(root.get("input"), _INPUT_FIELDS)
    parameters = root.get("parameters", {})
    if type(parameters) is not dict:
        raise _error("produce_request_file_invalid", "Produce request file is invalid")
    attributes = input_value.get("attributes", {})
    if type(attributes) is not dict:
        raise _error("produce_request_file_invalid", "Produce request file is invalid")
    if any(key.startswith("_") for key in parameters):
        raise _error("produce_request_parameter_invalid", "Produce request parameter is invalid")
    if "config_snapshot" in parameters:
        raise _error("produce_request_parameter_invalid", "Produce request parameter is invalid")
    if _contains_sensitive_key(parameters) or _contains_sensitive_key(attributes):
        raise _error("plaintext_secret_forbidden", "Plaintext secret fields are forbidden")

    try:
        return ProduceRequest(
            root["contract_version"],
            RecipeKey(recipe["recipe_id"], recipe["recipe_version"]),
            InputDescriptor(
                input_value["kind"],
                input_value["value"],
                input_value.get("attributes", {}),
            ),
            root["workspace_ref"],
            root["requested_outputs"],
            parameters,
            root.get("principal", "local-user"),
            root.get("client_request_id"),
        )
    except (KeyError, TypeError):
        raise _error("produce_request_file_invalid", "Produce request file is invalid") from None


__all__ = ["load_produce_request", "parse_recipe_selector"]

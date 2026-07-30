from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.cli.produce_request import load_produce_request, parse_recipe_selector
from app.core.errors import DomainError
from app.core.recipes.contracts import RecipeKey


def test_recipe_selector_is_strict() -> None:
    assert parse_recipe_selector("alltonote.video-producer@2") == RecipeKey(
        "alltonote.video-producer", 2
    )
    for value in (
        "alltonote.video-producer",
        "@2",
        "alltonote.video-producer@0",
        "alltonote.video-producer@-1",
        "alltonote.video-producer@two",
        "alltonote.video-producer@2@extra",
    ):
        with pytest.raises(DomainError, match="recipe_selector_invalid"):
            parse_recipe_selector(value)


def test_request_file_loads_existing_contract(tmp_path: Path) -> None:
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {
                "contract_version": 1,
                "recipe_key": {
                    "recipe_id": "alltonote.video-producer",
                    "recipe_version": 2,
                },
                "input": {"kind": "source", "value": "fixture://course"},
                "workspace_ref": "C:/vault",
                "requested_outputs": ["knowledge-note"],
                "parameters": {"provider_profile": "default"},
                "principal": "agent-a",
                "client_request_id": "request-file-1",
            }
        ),
        encoding="utf-8",
    )

    request = load_produce_request(path)

    assert request.recipe_key == RecipeKey("alltonote.video-producer", 2)
    assert request.input.value == "fixture://course"
    assert request.principal == "agent-a"
    assert request.client_request_id == "request-file-1"


@pytest.mark.parametrize(
    "payload",
    [
        {"contract_version": 2},
        {"contract_version": 1, "unknown": True},
        {
            "contract_version": 1,
            "recipe_key": {"recipe_id": "x", "recipe_version": 1},
            "input": {
                "kind": "source",
                "value": "fixture://course",
                "attributes": {"api_key": "secret-canary"},
            },
            "workspace_ref": "C:/vault",
            "requested_outputs": ["knowledge-note"],
            "parameters": {},
        },
        {
            "contract_version": 1,
            "recipe_key": {"recipe_id": "x", "recipe_version": 1},
            "input": {
                "kind": "source",
                "value": "fixture://course",
                "attributes": [["api_key", "secret-canary"]],
            },
            "workspace_ref": "C:/vault",
            "requested_outputs": ["knowledge-note"],
            "parameters": {},
        },
        {
            "contract_version": 1,
            "recipe_key": {"recipe_id": "x", "recipe_version": 1},
            "input": {"kind": "source", "value": "fixture://course"},
            "workspace_ref": "C:/vault",
            "requested_outputs": ["knowledge-note"],
            "parameters": {"_legacy_recipe_id": "forged"},
        },
        {
            "contract_version": 1,
            "recipe_key": {"recipe_id": "x", "recipe_version": 1},
            "input": {"kind": "source", "value": "fixture://course"},
            "workspace_ref": "C:/vault",
            "requested_outputs": ["knowledge-note"],
            "parameters": {"api_key": "secret-canary"},
        },
        {
            "contract_version": 1,
            "recipe_key": {"recipe_id": "x", "recipe_version": 1},
            "input": {"kind": "source", "value": "fixture://course"},
            "workspace_ref": "C:/vault",
            "requested_outputs": ["knowledge-note"],
            "parameters": {
                "config_snapshot": {
                    "snapshot_version": 1,
                    "values": {},
                    "digest": "caller-controlled",
                    "semantic_digest": "caller-controlled",
                }
            },
        },
    ],
)
def test_request_file_fails_closed_without_echoing_content(
    tmp_path: Path, payload: object
) -> None:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(DomainError) as raised:
        load_produce_request(path)

    assert "secret-canary" not in str(raised.value)
    assert str(path.resolve()) not in str(raised.value)


def test_request_file_rejects_duplicate_keys_and_non_finite_numbers(
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"contract_version":1,"contract_version":1}', encoding="utf-8")
    non_finite = tmp_path / "non-finite.json"
    non_finite.write_text('{"contract_version":NaN}', encoding="utf-8")

    for path in (duplicate, non_finite):
        with pytest.raises(DomainError, match="produce_request_file_invalid"):
            load_produce_request(path)

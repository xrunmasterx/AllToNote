from __future__ import annotations

import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from app.core.errors import DomainError
from app.core.jobs.model import JobState
from app.core.recipes import contracts
from app.core.recipes.contracts import (
    InputDescriptor,
    ProduceRequest,
    ProduceSubmission,
    RecipeDescriptor,
    RecipeKey,
)


def test_recipe_contracts_accept_minimal_immutable_request() -> None:
    key = RecipeKey("example.recipe", 1)
    descriptor = RecipeDescriptor(key, "Example", ("url",), ("markdown",))
    source = InputDescriptor("url", "https://example.test", {"title": "Example"})
    request = ProduceRequest(
        1,
        key,
        source,
        "workspace-1",
        ("markdown", "note"),
        {"nested": {"enabled": True}, "items": [1, 2]},
        "local-user",
        "request-1",
    )

    assert descriptor.key is key
    assert request.requested_outputs == ("markdown", "note")
    assert request.parameters["nested"]["enabled"] is True
    assert request.input.attributes["title"] == "Example"
    assert ProduceSubmission("job_1", key, JobState.QUEUED).state is JobState.QUEUED
    assert not hasattr(request, "__dict__")
    with pytest.raises(FrozenInstanceError):
        request.workspace_ref = "changed"  # type: ignore[misc]


def test_recipe_contracts_reject_unsupported_versions_and_invalid_text() -> None:
    key = RecipeKey("example.recipe", 1)
    with pytest.raises(DomainError, match="recipe_contract_version_unsupported"):
        ProduceRequest(2, key, InputDescriptor("url", "value"), "workspace", ())
    with pytest.raises(DomainError, match="recipe_contract_version_unsupported"):
        ProduceRequest(True, key, InputDescriptor("url", "value"), "workspace", ())
    with pytest.raises(DomainError, match="recipe_key_invalid"):
        RecipeKey("", 1)
    with pytest.raises(DomainError, match="recipe_key_invalid"):
        RecipeKey("example.recipe", True)
    with pytest.raises(DomainError, match="input_descriptor_invalid"):
        InputDescriptor("url", "")


def test_recipe_contracts_freeze_nested_json_values_without_repr_leaks() -> None:
    secret_canary = "secret-canary-do-not-render"
    parameters = {"items": [{"enabled": True}], "opaque": secret_canary}
    request = ProduceRequest(
        1,
        RecipeKey("example.recipe", 1),
        InputDescriptor("url", secret_canary, {"opaque": secret_canary}),
        "workspace",
        (),
        parameters,
    )
    parameters["items"][0]["enabled"] = False

    assert request.parameters["items"][0]["enabled"] is True
    assert secret_canary not in repr(request)
    assert secret_canary not in repr(request.input)
    with pytest.raises(TypeError):
        request.parameters["items"][0]["enabled"] = False


def test_recipe_contracts_reject_non_json_and_cyclic_values() -> None:
    invalid_values: list[object] = [object(), Path("input"), float("nan")]
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    invalid_values.append(cyclic)

    for invalid in invalid_values:
        with pytest.raises(DomainError, match="recipe_json_invalid"):
            ProduceRequest(
                1,
                RecipeKey("example.recipe", 1),
                InputDescriptor("url", "value"),
                "workspace",
                (),
                {"invalid": invalid},
            )


def test_recipe_contracts_expose_only_x0a_submission_types() -> None:
    assert set(contracts.__all__) == {
        "InputDescriptor",
        "ProduceRequest",
        "ProduceSubmission",
        "RecipeDescriptor",
        "RecipeEndpoint",
        "RecipeKey",
    }
    for excluded in (
        "PreflightReport",
        "RecipePlan",
        "RecipeOutput",
        "ProduceResult",
    ):
        assert not hasattr(contracts, excluded)


def test_recipe_contracts_cold_import_without_video_runtime_or_heavy_modules() -> None:
    backend_root = Path(__file__).parents[2]
    code = """
import json
import sys
import app.core.recipes.contracts
blocked = [
    name for name in sys.modules
    if name == 'app.core.domain.video'
    or name == 'app.runtime'
    or name == 'fastapi'
    or name.startswith(('torch', 'whisper', 'yt_dlp'))
]
print(json.dumps(blocked))
"""

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=backend_root,
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.strip() == "[]"

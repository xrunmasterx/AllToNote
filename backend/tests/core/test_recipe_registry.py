from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.errors import DomainError, ErrorCategory
from app.core.recipes.contracts import RecipeDescriptor, RecipeKey
from app.core.recipes.registry import RecipeRegistry


class Endpoint:
    def submit(self, request: object) -> object:
        raise AssertionError("metadata lookup must not call endpoints")


def descriptor(recipe_id: str, version: int) -> RecipeDescriptor:
    return RecipeDescriptor(
        RecipeKey(recipe_id, version), f"{recipe_id} v{version}",
        ("source",), ("markdown",),
    )


def assert_error(error: DomainError, expected: tuple[str, ErrorCategory, str]) -> None:
    assert (error.code, error.category, error.message) == expected


def test_registry_lists_and_resolves_exact_registrations_in_stable_order() -> None:
    descriptor_b = descriptor("recipe.b", 1)
    descriptor_a2 = descriptor("recipe.a", 2)
    descriptor_a1 = descriptor("recipe.a", 1)
    endpoint_b, endpoint_a2, endpoint_a1 = Endpoint(), Endpoint(), Endpoint()
    registry = RecipeRegistry((
        (descriptor_b, endpoint_b),
        (descriptor_a2, endpoint_a2),
        (descriptor_a1, endpoint_a1),
    ))

    assert registry.list() == (descriptor_a1, descriptor_a2, descriptor_b)
    assert registry.describe(descriptor_a2.key) is descriptor_a2
    assert registry.resolve(descriptor_a2.key) is endpoint_a2


def test_registry_owns_an_immutable_construction_snapshot() -> None:
    registered_descriptor = descriptor("known.recipe", 1)
    registered_endpoint = Endpoint()
    registrations = [(registered_descriptor, registered_endpoint)]
    registry = RecipeRegistry(registrations)
    registrations.append((descriptor("later.recipe", 1), Endpoint()))
    registrations.clear()

    assert isinstance(registry.list(), tuple)
    assert registry.list() == (registered_descriptor,)
    assert registry.describe(registered_descriptor.key) is registered_descriptor
    assert registry.resolve(registered_descriptor.key) is registered_endpoint


def test_registry_rejects_duplicate_keys_before_publication() -> None:
    first = descriptor("known.recipe", 1)
    duplicate = RecipeDescriptor(first.key, "Duplicate", ("other",), ("other",))
    with pytest.raises(DomainError) as raised:
        RecipeRegistry(((first, Endpoint()), (duplicate, Endpoint())))
    assert_error(raised.value, (
        "recipe_registration_duplicate", ErrorCategory.CONFLICT,
        "Recipe key is already registered",
    ))


@pytest.mark.parametrize("registration", [(object(), Endpoint()), (), (Endpoint(),)])
def test_registry_rejects_registration_without_a_descriptor(registration: object) -> None:
    with pytest.raises(DomainError) as raised:
        RecipeRegistry((registration,))  # type: ignore[arg-type]
    assert_error(raised.value, (
        "recipe_registration_invalid", ErrorCategory.INVALID_REQUEST,
        "Recipe registration requires a RecipeDescriptor",
    ))


def test_registry_rejects_registration_without_an_endpoint() -> None:
    with pytest.raises(DomainError) as raised:
        RecipeRegistry(((descriptor("known.recipe", 1), None),))  # type: ignore[arg-type]
    assert_error(raised.value, (
        "recipe_registration_invalid", ErrorCategory.INVALID_REQUEST,
        "Recipe registration requires a RecipeEndpoint",
    ))


@pytest.mark.parametrize("selector", [None, "video@1", ("video", 1)])
@pytest.mark.parametrize("operation", ["describe", "resolve"])
def test_registry_rejects_non_recipe_key_selectors(selector: object, operation: str) -> None:
    registry = RecipeRegistry(((descriptor("known.recipe", 1), Endpoint()),))
    with pytest.raises(DomainError) as raised:
        getattr(registry, operation)(selector)
    assert_error(raised.value, (
        "recipe_selector_invalid", ErrorCategory.INVALID_REQUEST,
        "Recipe selector must be a RecipeKey",
    ))


def test_registry_distinguishes_unknown_recipe_id() -> None:
    registry = RecipeRegistry(((descriptor("known.recipe", 1), Endpoint()),))
    with pytest.raises(DomainError) as raised:
        registry.resolve(RecipeKey("missing.recipe", 1))
    assert_error(raised.value, (
        "recipe_not_found", ErrorCategory.INVALID_REQUEST, "Recipe is not registered",
    ))


def test_registry_distinguishes_unknown_recipe_version() -> None:
    registry = RecipeRegistry(((descriptor("known.recipe", 1), Endpoint()),))
    with pytest.raises(DomainError) as raised:
        registry.describe(RecipeKey("known.recipe", 2))
    assert_error(raised.value, (
        "recipe_version_not_found", ErrorCategory.INVALID_REQUEST,
        "Recipe version is not registered",
    ))


def test_registry_metadata_operations_do_not_touch_endpoints() -> None:
    class UntouchableEndpoint:
        def __getattribute__(self, name: str) -> object:
            raise AssertionError(f"endpoint attribute accessed: {name}")

        def submit(self, request: object) -> object:
            raise AssertionError("metadata lookup must not call endpoints")

    registered_descriptor = descriptor("known.recipe", 1)
    registry = RecipeRegistry(((registered_descriptor, UntouchableEndpoint()),))
    assert registry.list() == (registered_descriptor,)
    assert registry.describe(registered_descriptor.key) is registered_descriptor


def test_recipe_registry_cold_import_avoids_runtime_and_heavy_modules() -> None:
    code = """
import json, sys
import app.core.recipes.registry
exact = {'app.core.domain.video', 'app.runtime', 'app.services.note',
         'app.transcriber.whisper', 'app.downloaders.youtube_downloader',
         'app.gpt.gpt_factory'}
prefixes = ('fastapi', 'torch', 'faster_whisper', 'mlx_whisper', 'yt_dlp',
            'openai', 'anthropic', 'httpx', 'sqlalchemy')
blocked = sorted(name for name in sys.modules
                 if name in exact or name.startswith(prefixes))
print(json.dumps(blocked))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=Path(__file__).parents[2], check=True,
        capture_output=True, text=True,
    )
    assert json.loads(completed.stdout) == []

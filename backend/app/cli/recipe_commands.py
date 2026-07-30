from __future__ import annotations

from app.cli.contracts import ApplicationResult
from app.cli.produce_request import parse_recipe_selector
from app.core.errors import DomainError, ErrorCategory
from app.core.recipes.contracts import RecipeDescriptor
from app.core.recipes.video.descriptor import VIDEO_DESCRIPTORS


def _projection(descriptor: RecipeDescriptor) -> dict[str, object]:
    return {
        "recipe_id": descriptor.key.recipe_id,
        "recipe_version": descriptor.key.recipe_version,
        "display_name": descriptor.display_name,
        "input_kinds": descriptor.input_kinds,
        "output_kinds": descriptor.output_kinds,
    }


def recipe_list_result(correlation_id: str) -> ApplicationResult:
    recipes = tuple(
        _projection(descriptor)
        for descriptor in sorted(
            VIDEO_DESCRIPTORS,
            key=lambda item: (item.key.recipe_id, item.key.recipe_version),
        )
    )
    return ApplicationResult(
        command="recipe list",
        correlation_id=correlation_id,
        ok=True,
        data={"recipes": recipes},
        human_lines=tuple(
            f"{item['recipe_id']}@{item['recipe_version']} - {item['display_name']}"
            for item in recipes
        ),
    )


def recipe_describe_result(selector: str, correlation_id: str) -> ApplicationResult:
    key = parse_recipe_selector(selector)
    matching_id = tuple(
        descriptor for descriptor in VIDEO_DESCRIPTORS if descriptor.key.recipe_id == key.recipe_id
    )
    if not matching_id:
        raise DomainError(
            "recipe_not_found",
            ErrorCategory.INVALID_REQUEST,
            "Recipe is not registered",
        )
    descriptor = next(
        (item for item in matching_id if item.key.recipe_version == key.recipe_version),
        None,
    )
    if descriptor is None:
        raise DomainError(
            "recipe_version_not_found",
            ErrorCategory.INVALID_REQUEST,
            "Recipe version is not registered",
        )
    data = _projection(descriptor)
    return ApplicationResult(
        command="recipe describe",
        correlation_id=correlation_id,
        ok=True,
        data=data,
        human_lines=(
            f"Recipe: {key.recipe_id}@{key.recipe_version}",
            f"Name: {descriptor.display_name}",
            f"Inputs: {', '.join(descriptor.input_kinds)}",
            f"Outputs: {', '.join(descriptor.output_kinds)}",
        ),
    )


__all__ = ["recipe_describe_result", "recipe_list_result"]

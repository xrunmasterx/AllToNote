from __future__ import annotations

from collections.abc import Iterable
from types import MappingProxyType

from app.core.errors import DomainError, ErrorCategory
from app.core.recipes.contracts import RecipeDescriptor, RecipeEndpoint, RecipeKey


def _error(code: str, category: ErrorCategory, message: str) -> DomainError:
    return DomainError(code, category, message)


class RecipeRegistry:
    def __init__(
        self,
        registrations: Iterable[tuple[RecipeDescriptor, RecipeEndpoint]],
    ) -> None:
        descriptors_by_key: dict[RecipeKey, RecipeDescriptor] = {}
        endpoints_by_key: dict[RecipeKey, RecipeEndpoint] = {}
        versions_by_id: dict[str, set[int]] = {}
        for registration in registrations:
            try:
                descriptor, endpoint = registration
            except (TypeError, ValueError):
                raise _error(
                    "recipe_registration_invalid",
                    ErrorCategory.INVALID_REQUEST,
                    "Recipe registration requires a RecipeDescriptor",
                ) from None
            if not isinstance(descriptor, RecipeDescriptor):
                raise _error(
                    "recipe_registration_invalid",
                    ErrorCategory.INVALID_REQUEST,
                    "Recipe registration requires a RecipeDescriptor",
                )
            if not callable(getattr(type(endpoint), "submit", None)):
                raise _error(
                    "recipe_registration_invalid",
                    ErrorCategory.INVALID_REQUEST,
                    "Recipe registration requires a RecipeEndpoint",
                )
            key = descriptor.key
            if key in descriptors_by_key:
                raise _error(
                    "recipe_registration_duplicate",
                    ErrorCategory.CONFLICT,
                    "Recipe key is already registered",
                )
            descriptors_by_key[key] = descriptor
            endpoints_by_key[key] = endpoint
            versions_by_id.setdefault(key.recipe_id, set()).add(key.recipe_version)

        descriptors = tuple(
            sorted(
                descriptors_by_key.values(),
                key=lambda item: (item.key.recipe_id, item.key.recipe_version),
            )
        )
        descriptors_lookup = MappingProxyType(descriptors_by_key)
        endpoints_lookup = MappingProxyType(endpoints_by_key)
        versions_lookup = MappingProxyType(
            {recipe_id: frozenset(versions) for recipe_id, versions in versions_by_id.items()}
        )
        self._descriptors = descriptors
        self._descriptors_by_key = descriptors_lookup
        self._endpoints_by_key = endpoints_lookup
        self._versions_by_id = versions_lookup

    def list(self) -> tuple[RecipeDescriptor, ...]:
        return self._descriptors

    def describe(self, selector: RecipeKey) -> RecipeDescriptor:
        self._require_registered(selector)
        return self._descriptors_by_key[selector]

    def resolve(self, selector: RecipeKey) -> RecipeEndpoint:
        self._require_registered(selector)
        return self._endpoints_by_key[selector]

    def _require_registered(self, selector: RecipeKey) -> None:
        if not isinstance(selector, RecipeKey):
            raise _error(
                "recipe_selector_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Recipe selector must be a RecipeKey",
            )
        if selector.recipe_id not in self._versions_by_id:
            raise _error(
                "recipe_not_found",
                ErrorCategory.INVALID_REQUEST,
                "Recipe is not registered",
            )
        if selector.recipe_version not in self._versions_by_id[selector.recipe_id]:
            raise _error(
                "recipe_version_not_found",
                ErrorCategory.INVALID_REQUEST,
                "Recipe version is not registered",
            )

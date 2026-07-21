from __future__ import annotations

from dataclasses import dataclass

from app.core.errors import DomainError, ErrorCategory
from app.core.recipes.contracts import ProduceRequest, ProduceSubmission
from app.core.recipes.registry import RecipeRegistry


@dataclass(frozen=True, slots=True)
class ProduceService:
    registry: RecipeRegistry

    def submit(self, request: ProduceRequest) -> ProduceSubmission:
        if not isinstance(request, ProduceRequest):
            raise DomainError(
                "produce_request_invalid",
                ErrorCategory.INVALID_REQUEST,
                "request must be a ProduceRequest",
            )
        endpoint = self.registry.resolve(request.recipe_key)
        submission = endpoint.submit(request)
        if not isinstance(submission, ProduceSubmission):
            raise DomainError(
                "produce_submission_invalid",
                ErrorCategory.INTERNAL,
                "Recipe endpoint returned an invalid submission",
            )
        if submission.recipe_key != request.recipe_key:
            raise DomainError(
                "produce_submission_recipe_mismatch",
                ErrorCategory.INTERNAL,
                "Recipe endpoint returned a mismatched Recipe key",
            )
        return submission


__all__ = ["ProduceService"]

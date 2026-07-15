from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.core.domain.video import (
    GeneratedVideoDraft,
    ScreenshotPolicy,
    TranscriptDocument,
)
from app.core.errors import DomainError, ErrorCategory
from app.core.ports.source import CancellationTokenPort


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise DomainError(
            "knowledge_model_request_invalid",
            ErrorCategory.INVALID_REQUEST,
            f"{field_name} must not be empty",
            {"field": field_name},
        )


@dataclass(frozen=True)
class KnowledgeModelRequest:
    """Provider-independent inputs needed to propose one video draft."""

    transcript: TranscriptDocument
    recipe_id: str
    recipe_version: int
    output_language: str
    style: str
    quality_preset: str
    screenshot_policy: ScreenshotPolicy

    def __post_init__(self) -> None:
        if not isinstance(self.transcript, TranscriptDocument):
            raise DomainError(
                "knowledge_model_request_invalid",
                ErrorCategory.INVALID_REQUEST,
                "transcript must use the Core transcript contract",
                {"field": "transcript"},
            )
        if (
            isinstance(self.recipe_version, bool)
            or not isinstance(self.recipe_version, int)
            or self.recipe_version < 1
        ):
            raise DomainError(
                "recipe_version_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Recipe version must be a positive integer",
            )
        if not isinstance(self.screenshot_policy, ScreenshotPolicy):
            raise DomainError(
                "screenshot_policy_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Screenshot policy must be a supported policy",
            )
        for field_name in (
            "recipe_id",
            "output_language",
            "style",
            "quality_preset",
        ):
            _require_text(getattr(self, field_name), field_name)


class KnowledgeModelPort(Protocol):
    """Boundary for proposing a generated video draft."""

    def generate(
        self,
        request: KnowledgeModelRequest,
        token: CancellationTokenPort,
    ) -> GeneratedVideoDraft: ...


__all__ = ["KnowledgeModelPort", "KnowledgeModelRequest"]

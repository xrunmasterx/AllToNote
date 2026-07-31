from __future__ import annotations

from collections.abc import Callable
import json
from typing import Protocol

from app.adapters.models.legacy_gpt import (
    LegacyModelResponse,
    LegacyReturnedInvalidResponse,
)
from app.core.errors import DomainError, ErrorCategory
from app.core.portable.identity import is_executor_identity
from app.core.ports.model_executor import ModelExecutionRequest, ModelOutputMode
from app.gpt.codex_app_server_client import CodexAppServerClient


_SUPPORTED_OUTER_FENCES = frozenset({"```json", "```markdown"})
_STRUCTURED_EXTRACTION_STAGES = frozenset(
    {"knowledge-map", "knowledge-consolidate", "faithful-edit"}
)
_STRUCTURED_EXTRACTION_EFFORT = "medium"
_COMPOSITION_EFFORT = "high"
_EXECUTION_POLICY_IDENTITY = (
    "codex-app-server-stage-effort-v1/"
    "knowledge-map,knowledge-consolidate,faithful-edit:medium/default:high"
)


class CodexAppServerTurnClient(Protocol):
    def run_markdown_turn(
        self,
        prompt: str,
        model: str,
        cwd: str | None = None,
        *,
        timeout_seconds: int | float | None = None,
        output_schema: dict[str, object] | None = None,
        reasoning_effort: str | None = None,
        check_cancelled: Callable[[], None] | None = None,
    ) -> str: ...


class CodexAppServerCompletionBridge:
    """Maps one legacy completion call to one ephemeral Codex app-server turn."""

    def __init__(
        self,
        *,
        model_identity: str,
        client: CodexAppServerTurnClient | None = None,
    ) -> None:
        if not is_executor_identity(model_identity):
            raise DomainError(
                "model_binding_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Codex app-server requires a valid frozen model identity",
            )
        resolved_client = client if client is not None else CodexAppServerClient()
        if not callable(getattr(resolved_client, "run_markdown_turn", None)):
            raise DomainError(
                "model_bridge_required",
                ErrorCategory.POLICY_DENIED,
                "Codex app-server requires a single-turn client",
            )
        self._model_identity = model_identity
        self._client = resolved_client

    def complete_once(
        self,
        prompt: str,
        *,
        check_cancelled: Callable[[], None] | None = None,
    ) -> LegacyModelResponse:
        """Compatibility entry point for the v1 Markdown generator."""

        # CodexAppServerError intentionally crosses this boundary unchanged. The
        # application coordinator owns outcome-unknown handling and replay policy.
        returned_text = self._client.run_markdown_turn(
            prompt,
            self._model_identity,
            cwd=None,
            check_cancelled=check_cancelled,
        )
        return self._normalized_response(returned_text)

    def complete_request(
        self,
        prompt: str,
        request: ModelExecutionRequest,
        *,
        check_cancelled: Callable[[], None] | None = None,
    ) -> LegacyModelResponse:
        if not isinstance(request, ModelExecutionRequest):
            raise DomainError(
                "model_call_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Codex app-server requires the frozen Core request",
            )
        if request.temperature is not None:
            raise DomainError(
                "model_capability_missing",
                ErrorCategory.POLICY_DENIED,
                "Codex app-server does not expose per-turn temperature",
            )
        output_schema = (
            json.loads(request.response_schema_json)
            if request.output_mode is ModelOutputMode.JSON_SCHEMA
            else None
        )
        returned_text = self._client.run_markdown_turn(
            prompt,
            self._model_identity,
            cwd=None,
            timeout_seconds=request.timeout_seconds,
            output_schema=output_schema,
            reasoning_effort=self._reasoning_effort(request.stage_id),
            check_cancelled=check_cancelled,
        )
        return self._normalized_response(
            returned_text,
            warnings=("provider_output_token_limit_unenforced",),
        )

    @staticmethod
    def execution_policy_identity() -> str:
        return _EXECUTION_POLICY_IDENTITY

    @staticmethod
    def _reasoning_effort(stage_id: str) -> str:
        if stage_id in _STRUCTURED_EXTRACTION_STAGES:
            return _STRUCTURED_EXTRACTION_EFFORT
        return _COMPOSITION_EFFORT

    def _normalized_response(
        self,
        returned_text: object,
        *,
        warnings: tuple[str, ...] = (),
    ) -> LegacyModelResponse:
        if type(returned_text) is not str or not returned_text.strip():
            raise LegacyReturnedInvalidResponse(
                "Codex app-server returned an invalid text response"
            )

        cleaned_text = self._remove_supported_outer_fence(returned_text)
        if not cleaned_text.strip():
            raise LegacyReturnedInvalidResponse(
                "Codex app-server returned an empty fenced response"
            )

        return LegacyModelResponse(
            markdown=cleaned_text,
            actual_model=self._model_identity,
            warnings=warnings,
        )

    @staticmethod
    def _remove_supported_outer_fence(text: str) -> str:
        stripped = text.strip()
        lines = stripped.splitlines()
        if (
            len(lines) >= 2
            and lines[0].strip().lower() in _SUPPORTED_OUTER_FENCES
            and lines[-1].strip() == "```"
        ):
            return "\n".join(lines[1:-1]).strip()
        return text


__all__ = ["CodexAppServerCompletionBridge", "CodexAppServerTurnClient"]

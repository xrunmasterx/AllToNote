from __future__ import annotations

from typing import Protocol

from app.adapters.models.legacy_gpt import (
    LegacyKnownRetryableModelFailure,
    LegacyModelResponse,
    LegacyReturnedInvalidResponse,
)
from app.core.errors import DomainError, ErrorCategory
from app.core.ports.model_executor import (
    ModelExecutionBinding,
    ModelExecutionRequest,
    ModelExecutionResult,
    ModelFinishReason,
    ModelOutputMode,
)
from app.core.ports.source import CancellationTokenPort


class LegacyAuthenticationFailure(Exception):
    """The legacy provider rejected its configured credential."""


class LegacyPolicyDeniedFailure(Exception):
    """The legacy provider rejected the request under a non-retryable policy."""


class LegacyRequestCompletionBridge(Protocol):
    """One provider turn that receives the complete frozen Core request."""

    def complete_request(
        self,
        prompt: str,
        request: ModelExecutionRequest,
    ) -> LegacyModelResponse: ...


class LegacyModelExecutor:
    """Adapts one legacy completion primitive to one Core model request."""

    def __init__(
        self,
        *,
        binding: ModelExecutionBinding,
        bridge: LegacyRequestCompletionBridge,
    ) -> None:
        if not isinstance(binding, ModelExecutionBinding):
            raise DomainError(
                "model_execution_binding_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Legacy model execution requires a frozen Core binding",
            )
        if not callable(getattr(bridge, "complete_request", None)):
            raise DomainError(
                "model_bridge_required",
                ErrorCategory.POLICY_DENIED,
                "A single-request legacy model bridge is required",
            )
        self._binding = binding
        self._bridge = bridge

    def complete(
        self,
        request: ModelExecutionRequest,
        token: CancellationTokenPort,
    ) -> ModelExecutionResult:
        self._validate_request(request)
        token.raise_if_cancelled()
        prompt = self._build_prompt(request)

        try:
            response = self._bridge.complete_request(prompt, request)
        except LegacyAuthenticationFailure:
            raise DomainError(
                "model_authentication_failed",
                ErrorCategory.POLICY_DENIED,
                "The model provider rejected the configured credential",
            ) from None
        except LegacyPolicyDeniedFailure:
            raise DomainError(
                "model_policy_denied",
                ErrorCategory.POLICY_DENIED,
                "The model provider denied the request",
            ) from None
        except LegacyKnownRetryableModelFailure:
            raise DomainError(
                "model_generation_failed",
                ErrorCategory.RETRYABLE_RUNTIME,
                "The model provider returned a known retryable failure",
            ) from None
        except LegacyReturnedInvalidResponse:
            raise DomainError(
                "model_response_invalid",
                ErrorCategory.RECIPE_FAILED,
                "The model provider returned an invalid response",
            ) from None
        except TimeoutError:
            raise self._outcome_unknown() from None
        except DomainError as error:
            if self._is_known_domain_failure(error):
                raise
            raise self._outcome_unknown() from None
        except Exception:
            raise self._outcome_unknown() from None

        if not isinstance(response, LegacyModelResponse):
            raise DomainError(
                "model_response_invalid",
                ErrorCategory.RECIPE_FAILED,
                "The legacy model bridge returned an invalid response contract",
            )

        actual_model = response.actual_model or self._binding.model_identity
        if actual_model != self._binding.model_identity:
            raise DomainError(
                "model_identity_mismatch",
                ErrorCategory.RECIPE_FAILED,
                "Actual model identity does not match the frozen binding",
            )

        warnings = ["legacy_finish_reason_unavailable", *response.warnings]
        if response.actual_model is None:
            warnings.append("legacy_actual_model_identity_unreported")
        if response.input_tokens is None or response.output_tokens is None:
            warnings.append("legacy_model_usage_unavailable")

        return ModelExecutionResult(
            text=response.markdown,
            actual_model_identity=actual_model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            finish_reason=ModelFinishReason.UNKNOWN,
            provider_request_id=response.provider_request_id,
            warnings=tuple(warnings),
        )

    def _validate_request(self, request: ModelExecutionRequest) -> None:
        if not isinstance(request, ModelExecutionRequest):
            raise DomainError(
                "model_call_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Legacy model execution requires the Core request contract",
            )
        if request.max_output_tokens > self._binding.max_output_tokens:
            raise DomainError(
                "model_call_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Requested output exceeds the frozen model binding",
            )
        if request.timeout_seconds > self._binding.timeout_seconds:
            raise DomainError(
                "model_call_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Requested timeout exceeds the frozen model binding",
            )
        if (
            request.output_mode is ModelOutputMode.JSON_SCHEMA
            and not self._binding.supports_structured_output
        ):
            raise DomainError(
                "model_capability_missing",
                ErrorCategory.POLICY_DENIED,
                "The frozen model binding does not support structured output",
            )
        if request.temperature is not None and not self._binding.supports_temperature:
            raise DomainError(
                "model_capability_missing",
                ErrorCategory.POLICY_DENIED,
                "The frozen model binding does not support temperature",
            )

    @staticmethod
    def _build_prompt(request: ModelExecutionRequest) -> str:
        parts = (
            "<system_instruction>\n",
            request.system_instruction,
            "\n</system_instruction>\n<user_content>\n",
            request.user_content,
            "\n</user_content>",
        )
        prompt = "".join(parts)
        if request.output_mode is ModelOutputMode.JSON_SCHEMA:
            prompt += (
                "\n<response_schema>\n"
                f"{request.response_schema_json}"
                "\n</response_schema>"
            )
        return prompt

    @staticmethod
    def _is_known_domain_failure(error: DomainError) -> bool:
        if error.category in {
            ErrorCategory.CANCELLED,
            ErrorCategory.INVALID_REQUEST,
            ErrorCategory.POLICY_DENIED,
            ErrorCategory.RECIPE_FAILED,
            ErrorCategory.RETRYABLE_RUNTIME,
        }:
            return True
        return (
            error.category is ErrorCategory.CONFLICT
            and error.code == "external_outcome_unknown"
        )

    @staticmethod
    def _outcome_unknown() -> DomainError:
        return DomainError(
            "external_outcome_unknown",
            ErrorCategory.CONFLICT,
            "The model provider outcome is unknown",
        )


__all__ = [
    "LegacyAuthenticationFailure",
    "LegacyModelExecutor",
    "LegacyPolicyDeniedFailure",
]

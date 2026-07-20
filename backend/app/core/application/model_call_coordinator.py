from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from app.core.domain.ids import sha256_digest
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.external_operation import (
    ExternalOperation,
    ExternalOperationGuard,
    ExternalOperationStorePort,
    ExternalOutcome,
)
from app.core.jobs.resource_lease import ExecutionAuthority
from app.core.portable.jsonio import encode_json
from app.core.ports.model_executor import (
    ModelExecutionBinding,
    ModelExecutionRequest,
    ModelExecutionResult,
    ModelExecutorPort,
    ModelOutputMode,
)
from app.core.ports.source import CancellationTokenPort


_SAFE_SHARD_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_OPERATION_NAME = "model-completion"


@dataclass(frozen=True)
class StoredModelOperationResult:
    relative_path: str
    sha256: str


class ModelOperationResultStorePort(Protocol):
    def save(
        self,
        operation_id: str,
        request_hash: str,
        result: ModelExecutionResult,
    ) -> StoredModelOperationResult: ...

    def load(
        self,
        operation_id: str,
        request_hash: str,
        summary_json: str,
    ) -> ModelExecutionResult: ...


@dataclass(frozen=True)
class ModelCallExecution:
    job_id: str
    step_id: str
    attempt_id: str
    authority: ExecutionAuthority
    heartbeat: Callable[[], None] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        for value in (self.job_id, self.step_id, self.attempt_id):
            if type(value) is not str or not value.strip():
                raise DomainError(
                    "model_call_execution_invalid",
                    ErrorCategory.INVALID_REQUEST,
                    "Model call execution requires Job, Step, and Attempt identities",
                )
        if not callable(self.heartbeat):
            raise DomainError(
                "model_call_execution_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Model call execution requires a heartbeat callback",
            )


class ModelCallCoordinator:
    """Runs recoverable model operations without owning prompt or recipe semantics."""

    def __init__(
        self,
        *,
        operation_store: ExternalOperationStorePort,
        result_store: ModelOperationResultStorePort,
        executor: ModelExecutorPort,
        max_attempts: int = 2,
    ) -> None:
        if type(max_attempts) is not int or max_attempts < 1:
            raise DomainError(
                "model_retry_budget_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Model retry budget must be a positive integer",
            )
        self._operation_store = operation_store
        self._result_store = result_store
        self._executor = executor
        self._max_attempts = max_attempts

    def execute(
        self,
        binding: ModelExecutionBinding,
        request: ModelExecutionRequest,
        execution: ModelCallExecution,
        shard_key: str,
        token: CancellationTokenPort,
    ) -> ModelExecutionResult:
        self._validate_call(binding, request, shard_key)
        request_hash = self.request_hash(binding, request, shard_key)
        prepared_summary = self._prepared_summary(binding, request, shard_key)
        guard = ExternalOperationGuard(self._operation_store, execution.authority)
        failures_in_call = 0

        while True:
            token.raise_if_cancelled()
            execution.heartbeat()
            try:
                operation = guard.prepare(
                    job_id=execution.job_id,
                    step_id=execution.step_id,
                    attempt_id=execution.attempt_id,
                    provider=binding.provider_type,
                    request_hash=request_hash,
                    operation_idempotency_key=None,
                    summary_json=prepared_summary,
                    max_attempts=self._max_attempts,
                )
            except DomainError as error:
                if error.code == "external_attempt_budget_exhausted" and failures_in_call:
                    raise DomainError(
                        "model_generation_failed",
                        ErrorCategory.RETRYABLE_RUNTIME,
                        "The model provider exhausted the retry budget",
                    ) from None
                raise

            if operation.outcome is ExternalOutcome.SUCCEEDED:
                return self._recover_terminal_operation(
                    operation,
                    binding,
                    request_hash,
                    token,
                )

            token.raise_if_cancelled()
            execution.heartbeat()
            guard.start(operation.operation_id)
            try:
                result = self._executor.complete(request, token)
            except DomainError as error:
                if error.code == "external_outcome_unknown":
                    guard.unknown(
                        operation.operation_id,
                        summary_json=self._unknown_summary(shard_key),
                    )
                    raise
                if error.category is ErrorCategory.RETRYABLE_RUNTIME:
                    guard.fail(
                        operation.operation_id,
                        summary_json=self._failure_summary(shard_key, error),
                    )
                    failures_in_call += 1
                    continue
                if error.category is ErrorCategory.CANCELLED:
                    guard.unknown(
                        operation.operation_id,
                        summary_json=self._unknown_summary(shard_key),
                    )
                    raise
                self._finish_terminal_error(guard, operation, shard_key, error)
                raise
            except Exception:
                guard.unknown(
                    operation.operation_id,
                    summary_json=self._unknown_summary(shard_key),
                )
                raise DomainError(
                    "external_outcome_unknown",
                    ErrorCategory.CONFLICT,
                    "The model provider outcome is unknown",
                ) from None

            execution.heartbeat()
            try:
                token.raise_if_cancelled()
            except DomainError as error:
                if error.category is not ErrorCategory.CANCELLED:
                    raise
                guard.unknown(
                    operation.operation_id,
                    summary_json=self._unknown_summary(shard_key),
                )
                raise

            if not isinstance(result, ModelExecutionResult):
                error = DomainError(
                    "model_response_invalid",
                    ErrorCategory.RECIPE_FAILED,
                    "Model executor returned an invalid response contract",
                )
                self._finish_terminal_error(guard, operation, shard_key, error)
                raise error

            if result.actual_model_identity != binding.model_identity:
                error = DomainError(
                    "model_identity_mismatch",
                    ErrorCategory.RECIPE_FAILED,
                    "Actual model identity does not match the frozen binding",
                )
                self._finish_terminal_error(guard, operation, shard_key, error)
                raise error

            try:
                stored = self._result_store.save(
                    operation.operation_id,
                    request_hash,
                    result,
                )
            except DomainError:
                guard.unknown(
                    operation.operation_id,
                    summary_json=self._unknown_summary(shard_key),
                )
                raise

            execution.heartbeat()
            guard.succeed(
                operation.operation_id,
                provider_request_id=result.provider_request_id,
                summary_json=self._success_summary(shard_key, request_hash, stored),
            )
            token.raise_if_cancelled()
            return result

    @staticmethod
    def request_hash(
        binding: ModelExecutionBinding,
        request: ModelExecutionRequest,
        shard_key: str,
    ) -> str:
        ModelCallCoordinator._validate_call(binding, request, shard_key)
        return sha256_digest(
            encode_json(
                {
                    "binding": {
                        "context_window_tokens": binding.context_window_tokens,
                        "credential_profile_ref": binding.credential_profile_ref,
                        "max_concurrency": binding.max_concurrency,
                        "max_output_tokens": binding.max_output_tokens,
                        "model_identity": binding.model_identity,
                        "provider_type": binding.provider_type,
                        "schema_version": binding.schema_version,
                        "supports_structured_output": binding.supports_structured_output,
                        "supports_temperature": binding.supports_temperature,
                    },
                    "operation": _OPERATION_NAME,
                    "request": {
                        "max_output_tokens": request.max_output_tokens,
                        "output_mode": request.output_mode.value,
                        "prompt_id": request.prompt_id,
                        "prompt_version": request.prompt_version,
                        "response_schema_json": request.response_schema_json,
                        "schema_version": request.schema_version,
                        "stage_id": request.stage_id,
                        "stage_version": request.stage_version,
                        "system_instruction": request.system_instruction,
                        "temperature": request.temperature,
                        "user_content": request.user_content,
                    },
                    "shard_key": shard_key,
                }
            )
        )

    @staticmethod
    def _validate_call(
        binding: ModelExecutionBinding,
        request: ModelExecutionRequest,
        shard_key: str,
    ) -> None:
        if not isinstance(binding, ModelExecutionBinding) or not isinstance(
            request, ModelExecutionRequest
        ):
            raise DomainError(
                "model_call_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Model call must use the frozen Core contracts",
            )
        if type(shard_key) is not str or _SAFE_SHARD_KEY.fullmatch(shard_key) is None:
            raise DomainError(
                "model_shard_key_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Model shard key is invalid",
            )
        if request.max_output_tokens > binding.max_output_tokens:
            raise DomainError(
                "model_call_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Requested output exceeds the frozen model binding",
            )
        if request.timeout_seconds > binding.timeout_seconds:
            raise DomainError(
                "model_call_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Requested timeout exceeds the frozen model binding",
            )
        if (
            request.output_mode is ModelOutputMode.JSON_SCHEMA
            and not binding.supports_structured_output
        ):
            raise DomainError(
                "model_capability_missing",
                ErrorCategory.POLICY_DENIED,
                "The frozen model binding does not support structured output",
            )
        if request.temperature is not None and not binding.supports_temperature:
            raise DomainError(
                "model_capability_missing",
                ErrorCategory.POLICY_DENIED,
                "The frozen model binding does not support temperature",
            )

    def _recover_terminal_operation(
        self,
        operation: ExternalOperation,
        binding: ModelExecutionBinding,
        request_hash: str,
        token: CancellationTokenPort,
    ) -> ModelExecutionResult:
        terminal_error = self._decode_terminal_error(operation.summary_json)
        if terminal_error is not None:
            raise terminal_error
        result = self._result_store.load(
            operation.operation_id,
            request_hash,
            operation.summary_json,
        )
        if result.actual_model_identity != binding.model_identity:
            raise DomainError(
                "model_identity_mismatch",
                ErrorCategory.RECIPE_FAILED,
                "Stored model identity does not match the frozen binding",
            )
        token.raise_if_cancelled()
        return result

    @staticmethod
    def _finish_terminal_error(
        guard: ExternalOperationGuard,
        operation: ExternalOperation,
        shard_key: str,
        error: DomainError,
    ) -> None:
        # A succeeded operation outcome closes this paid-call key permanently;
        # the summary preserves the normalized terminal error for recovery.
        guard.succeed(
            operation.operation_id,
            provider_request_id=None,
            summary_json=ModelCallCoordinator._terminal_error_summary(
                shard_key, error
            ),
        )

    @staticmethod
    def _prepared_summary(
        binding: ModelExecutionBinding,
        request: ModelExecutionRequest,
        shard_key: str,
    ) -> str:
        return ModelCallCoordinator._json_summary(
            {
                "model_identity": binding.model_identity,
                "operation": _OPERATION_NAME,
                "prompt_id": request.prompt_id,
                "prompt_version": request.prompt_version,
                "shard_key": shard_key,
                "stage_id": request.stage_id,
                "stage_version": request.stage_version,
            }
        )

    @staticmethod
    def _success_summary(
        shard_key: str,
        request_hash: str,
        stored: StoredModelOperationResult,
    ) -> str:
        return ModelCallCoordinator._json_summary(
            {
                "operation": _OPERATION_NAME,
                "result": {
                    "path": stored.relative_path,
                    "request_hash": request_hash,
                    "sha256": stored.sha256,
                },
                "shard_key": shard_key,
            }
        )

    @staticmethod
    def _failure_summary(shard_key: str, error: DomainError) -> str:
        return ModelCallCoordinator._json_summary(
            {
                "error": {"category": error.category.value, "code": error.code},
                "operation": _OPERATION_NAME,
                "shard_key": shard_key,
            }
        )

    @staticmethod
    def _unknown_summary(shard_key: str) -> str:
        return ModelCallCoordinator._json_summary(
            {"operation": _OPERATION_NAME, "shard_key": shard_key}
        )

    @staticmethod
    def _terminal_error_summary(shard_key: str, error: DomainError) -> str:
        return ModelCallCoordinator._json_summary(
            {
                "operation": _OPERATION_NAME,
                "shard_key": shard_key,
                "terminal_error": {
                    "category": error.category.value,
                    "code": error.code,
                },
            }
        )

    @staticmethod
    def _decode_terminal_error(summary_json: str) -> DomainError | None:
        try:
            summary = json.loads(summary_json)
            terminal = summary.get("terminal_error")
            if terminal is None:
                return None
            if (
                type(summary) is not dict
                or summary.get("operation") != _OPERATION_NAME
                or type(terminal) is not dict
                or set(terminal) != {"category", "code"}
            ):
                raise TypeError
            return DomainError(
                terminal["code"],
                ErrorCategory(terminal["category"]),
                "The model operation ended with a non-retryable error",
            )
        except MemoryError:
            raise
        except (AttributeError, TypeError, ValueError):
            raise DomainError(
                "external_result_unavailable",
                ErrorCategory.CONFLICT,
                "The anchored model result is unavailable or invalid",
            ) from None

    @staticmethod
    def _json_summary(value: dict[str, object]) -> str:
        return encode_json(value).decode("utf-8").rstrip("\n")


__all__ = [
    "ModelCallCoordinator",
    "ModelCallExecution",
    "ModelOperationResultStorePort",
    "StoredModelOperationResult",
]

from __future__ import annotations

from types import MappingProxyType

from app.core.errors import DomainError, ErrorCategory


ENGINE_REMOTE_ERROR_CATEGORIES = MappingProxyType(
    {
        "engine_job_reference_invalid": ErrorCategory.INVALID_REQUEST,
        "workspace_instance_not_found": ErrorCategory.INVALID_REQUEST,
        "job_not_found": ErrorCategory.INVALID_REQUEST,
        "workspace_instance_registry_invalid": (
            ErrorCategory.WORKSPACE_INCOMPATIBLE
        ),
        "workspace_instance_root_unsafe": (
            ErrorCategory.WORKSPACE_INCOMPATIBLE
        ),
        "engine_job_store_unavailable": (
            ErrorCategory.WORKSPACE_INCOMPATIBLE
        ),
        "job_store_schema_invalid": ErrorCategory.WORKSPACE_INCOMPATIBLE,
        "job_store_wal_unavailable": ErrorCategory.WORKSPACE_INCOMPATIBLE,
        "engine_job_authority_denied": ErrorCategory.POLICY_DENIED,
        "job_store_busy": ErrorCategory.RETRYABLE_RUNTIME,
        "engine_notification_backpressure": ErrorCategory.RETRYABLE_RUNTIME,
        "engine_draining": ErrorCategory.RETRYABLE_RUNTIME,
        "engine_internal_error": ErrorCategory.INTERNAL,
    }
)


def engine_remote_error_code(error: DomainError) -> str:
    expected = ENGINE_REMOTE_ERROR_CATEGORIES.get(error.code)
    return error.code if expected is error.category else "engine_internal_error"


__all__ = ["ENGINE_REMOTE_ERROR_CATEGORIES", "engine_remote_error_code"]

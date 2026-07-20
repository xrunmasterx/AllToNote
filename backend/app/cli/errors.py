from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Mapping

from app.cli.contracts import CliError
from app.core.errors import DomainError, ErrorCategory, ErrorDetail


class ExitCode(IntEnum):
    SUCCESS = 0
    INVALID_REQUEST = 2
    WORKSPACE_INCOMPATIBLE = 10
    CONFLICT = 20
    RETRYABLE_RUNTIME = 30
    POLICY_DENIED = 40
    RECIPE_FAILED = 50
    CANCELLED = 60
    INTERNAL = 70
    INTERRUPTED = 130


@dataclass(frozen=True)
class MappedCliError:
    error: CliError
    exit_code: ExitCode


@dataclass(frozen=True)
class _ErrorPolicy:
    exit_code: ExitCode
    retryable: bool
    next_actions: tuple[str, ...]


_ERROR_POLICIES = {
    ErrorCategory.INVALID_REQUEST: _ErrorPolicy(
        ExitCode.INVALID_REQUEST,
        False,
        ("Correct the request and run the command again",),
    ),
    ErrorCategory.WORKSPACE_INCOMPATIBLE: _ErrorPolicy(
        ExitCode.WORKSPACE_INCOMPATIBLE,
        False,
        ("Verify the Workspace and required Runtime contracts",),
    ),
    ErrorCategory.CONFLICT: _ErrorPolicy(
        ExitCode.CONFLICT,
        False,
        ("Refresh the current state before retrying the operation",),
    ),
    ErrorCategory.RETRYABLE_RUNTIME: _ErrorPolicy(
        ExitCode.RETRYABLE_RUNTIME,
        True,
        ("Retry after the temporary Runtime or dependency failure clears",),
    ),
    ErrorCategory.POLICY_DENIED: _ErrorPolicy(
        ExitCode.POLICY_DENIED,
        False,
        ("Satisfy the required policy, capability, grant, or credential",),
    ),
    ErrorCategory.RECIPE_FAILED: _ErrorPolicy(
        ExitCode.RECIPE_FAILED,
        False,
        ("Inspect the Job error and its safe recovery action",),
    ),
    ErrorCategory.CANCELLED: _ErrorPolicy(
        ExitCode.CANCELLED,
        False,
        ("Submit a new Job or explicitly retry the terminal Job",),
    ),
    ErrorCategory.INTERNAL: _ErrorPolicy(
        ExitCode.INTERNAL,
        False,
        ("Use the correlation ID to inspect local diagnostic logs",),
    ),
}


def map_error(
    *,
    code: str,
    category: ErrorCategory,
    message: str,
    details: Mapping[str, object],
) -> MappedCliError:
    policy = _ERROR_POLICIES[category]
    next_actions = policy.next_actions
    if code == "credential_input_required":
        next_actions = (
            "Pass the credential through --stdin or use an interactive terminal",
        )
    elif code == "credential_backend_locked":
        next_actions = ("Unlock the operating-system credential backend",)
    elif code == "credential_backend_unavailable":
        next_actions = (
            "Enable a secure operating-system credential backend and retry",
        )
    elif code.startswith("credential_"):
        next_actions = ("Configure or refresh the referenced credential profile",)
    elif "capability" in code or "feature_pack" in code:
        next_actions = ("Install or enable the required compatible capability",)
    elif code == "external_outcome_unknown":
        next_actions = (
            "Inspect the unknown external operation before confirming a new retry",
        )
    return MappedCliError(
        error=CliError(
            code=code,
            category=category.value,
            message=message,
            retryable=policy.retryable,
            next_actions=next_actions,
            details=details,
        ),
        exit_code=policy.exit_code,
    )


def map_domain_error(error: DomainError) -> MappedCliError:
    return map_error(
        code=error.code,
        category=error.category,
        message=error.message,
        details=error.details,
    )


def map_error_detail(error: ErrorDetail) -> MappedCliError:
    return map_error(
        code=error.code,
        category=error.category,
        message=error.message,
        details=error.details,
    )


def internal_error() -> MappedCliError:
    return map_domain_error(
        DomainError(
            "internal_error",
            ErrorCategory.INTERNAL,
            "AllToNote could not complete the command",
        )
    )


__all__ = [
    "ExitCode",
    "MappedCliError",
    "internal_error",
    "map_domain_error",
    "map_error_detail",
]

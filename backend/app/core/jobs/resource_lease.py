from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.model import Attempt, Job, JobExecutionBinding


MIN_LEASE_TTL_SECONDS = 1
MAX_LEASE_TTL_SECONDS = 300
HEAVY_PRODUCTION_RESOURCE_NAME = "produce:heavy:v1"


def validate_lease_ttl(ttl_seconds: int) -> None:
    if (
        type(ttl_seconds) is not int
        or not MIN_LEASE_TTL_SECONDS <= ttl_seconds <= MAX_LEASE_TTL_SECONDS
    ):
        raise DomainError(
            "resource_lease_ttl_invalid",
            ErrorCategory.INVALID_REQUEST,
            "Resource lease TTL is outside the supported bounds",
        )


@dataclass(frozen=True)
class ExecutionAuthority:
    owner_id: str
    fencing_token: int

    def __post_init__(self) -> None:
        if (
            type(self.owner_id) is not str
            or not self.owner_id.strip()
            or self.owner_id.isdecimal()
            or type(self.fencing_token) is not int
            or self.fencing_token < 1
        ):
            raise DomainError(
                "execution_authority_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Execution authority requires a process-instance owner and token",
            )


@dataclass(frozen=True)
class JobExecutionAuthority(ExecutionAuthority):
    job_id: str

    def __post_init__(self) -> None:
        super().__post_init__()
        if type(self.job_id) is not str or not self.job_id.strip():
            raise DomainError(
                "execution_authority_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Job execution authority requires a durable Job identity",
            )


@dataclass(frozen=True)
class PersistedJobClaim:
    job: Job
    binding: JobExecutionBinding
    authority: JobExecutionAuthority
    active_attempt: Attempt | None


@dataclass(frozen=True)
class ResourceOwner:
    workspace_identity: str
    process_instance_id: str
    process_id: int | None = None

    def __post_init__(self) -> None:
        if (
            type(self.workspace_identity) is not str
            or not self.workspace_identity.strip()
            or type(self.process_instance_id) is not str
            or not self.process_instance_id.strip()
            or (
                self.process_id is not None
                and (type(self.process_id) is not int or self.process_id < 1)
            )
        ):
            raise DomainError(
                "resource_owner_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Resource owner requires workspace and process-instance identity",
            )


@dataclass(frozen=True)
class ResourceLease:
    resource_name: str
    owner: ResourceOwner
    fencing_token: int
    expires_at_ms: int
    _heartbeat_callback: Callable[[int], ResourceLease] = field(
        repr=False, compare=False
    )
    _release_callback: Callable[[], bool] = field(repr=False, compare=False)

    def heartbeat(self, *, ttl_seconds: int) -> ResourceLease:
        return self._heartbeat_callback(ttl_seconds)

    def release(self) -> bool:
        return self._release_callback()


class ResourceLeaseStorePort(Protocol):
    def acquire(
        self,
        resource_name: str,
        owner: ResourceOwner,
        *,
        ttl_seconds: int,
    ) -> ResourceLease: ...


__all__ = [
    "ExecutionAuthority",
    "HEAVY_PRODUCTION_RESOURCE_NAME",
    "JobExecutionAuthority",
    "MAX_LEASE_TTL_SECONDS",
    "MIN_LEASE_TTL_SECONDS",
    "PersistedJobClaim",
    "ResourceLease",
    "ResourceLeaseStorePort",
    "ResourceOwner",
    "validate_lease_ttl",
]

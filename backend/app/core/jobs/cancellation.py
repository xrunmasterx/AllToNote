from __future__ import annotations

from typing import Protocol

from app.core.errors import DomainError, ErrorCategory


class _CancellationStore(Protocol):
    def is_cancellation_requested(self, job_id: str) -> bool: ...


class CancellationToken:
    def __init__(self, store: _CancellationStore, job_id: str) -> None:
        self._store = store
        self._job_id = job_id

    def is_cancelled(self) -> bool:
        return self._store.is_cancellation_requested(self._job_id)

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise DomainError(
                "job_cancelled",
                ErrorCategory.CANCELLED,
                "Job cancellation was requested",
            )


__all__ = ["CancellationToken"]

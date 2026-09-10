"""Cache only call groups explicitly approved by the recipe's quality gate."""
from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

from app.core.ports.model_executor import ModelExecutionResult


class ReviewedCallCachePort(Protocol):
    def load(self, key: str) -> dict[str, ModelExecutionResult]: ...

    def save(self, key: str, results: dict[str, ModelExecutionResult]) -> None: ...


@dataclass
class ReviewedCalls:
    cached: dict[str, ModelExecutionResult]
    completed: dict[str, ModelExecutionResult] = field(default_factory=dict)


active_reviewed_calls: ContextVar[ReviewedCalls | None] = ContextVar(
    "active_reviewed_calls", default=None,
)
T = TypeVar("T")


def run_reviewed_calls(
    cache: ReviewedCallCachePort | None,
    key: str,
    action: Callable[[], T],
    approved: Callable[[T], bool],
) -> T:
    if cache is None:
        return action()
    calls = ReviewedCalls(cache.load(key))
    reset = active_reviewed_calls.set(calls)
    try:
        value = action()
        # A successful transport is not approval. Rejected/partial groups are
        # never published, even when other groups complete concurrently.
        if approved(value) and calls.completed:
            cache.save(key, calls.completed)
        return value
    finally:
        active_reviewed_calls.reset(reset)

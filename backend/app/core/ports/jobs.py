from typing import Protocol


class JobRepositoryPort(Protocol):
    """Boundary for durable job state and execution records."""


class AttemptStoragePort(Protocol):
    """Boundary for private attempt staging and checkpoints."""

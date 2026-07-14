from __future__ import annotations

from typing import TYPE_CHECKING, Callable, TypeAlias

if TYPE_CHECKING:
    from app.core.jobs.model import JobEvent


EventSink: TypeAlias = Callable[["JobEvent"], None]

from dataclasses import dataclass, field


@dataclass(frozen=True)
class VisualFrame:
    """A captured frame, ordered alongside the image inputs to a model call."""

    segment_id: str
    timestamp_ms: int
    payload: bytes = field(repr=False)

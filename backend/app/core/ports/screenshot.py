from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.core.application.video_acquisition import VideoAcquisition
    from app.core.application.video_service import VideoStepExecutionContext
    from app.core.domain.video import ScreenshotPlanItem, TranscriptDocument
    from app.core.jobs.model import CheckpointMetadata
    from app.core.portable.bundle_assembler import DisplayAssetInput


class ScreenshotPort(Protocol):
    """Boundary for extracting validated screenshot requests."""

    def extract(
        self,
        plan: tuple[ScreenshotPlanItem, ...],
        transcript: TranscriptDocument,
        acquired: VideoAcquisition,
        *,
        acquisition_checkpoint: CheckpointMetadata,
        execution: VideoStepExecutionContext,
    ) -> tuple[DisplayAssetInput, ...]: ...

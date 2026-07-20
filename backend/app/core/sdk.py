from __future__ import annotations

from app.core.application.video_service import VideoService
from app.core.domain.video import JobSnapshot, VideoProduceRequest


class AllToNoteSDK:
    """Stable in-process facade shared by the CLI and future UI adapters."""

    def __init__(self, video_service: VideoService) -> None:
        self._video_service = video_service

    def submit_video(self, request: VideoProduceRequest) -> JobSnapshot:
        return self._video_service.submit_video(request)

    def wait_job(self, job_id: str, event_sink: object | None = None) -> JobSnapshot:
        del event_sink
        return self._video_service.wait_job(job_id)

    def get_job(self, job_id: str) -> JobSnapshot:
        return self._video_service.get_job(job_id)

    def cancel_job(self, job_id: str) -> JobSnapshot:
        return self._video_service.cancel_job(job_id)


__all__ = ["AllToNoteSDK"]

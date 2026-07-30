from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from app.core.application.produce_service import ProduceService
from app.core.domain.video import JobSnapshot, VideoProduceRequest
from app.core.recipes.contracts import ProduceRequest, ProduceSubmission


class JobControl(Protocol):
    def wait_job(self, job_id: str) -> JobSnapshot: ...

    def get_job(self, job_id: str) -> JobSnapshot: ...

    def cancel_job(self, job_id: str) -> JobSnapshot: ...


class AllToNoteSDK:
    """Stable in-process facade shared by the CLI and future UI adapters."""

    __slots__ = ("_job_control", "_legacy_video_adapter", "_produce_service")

    def __init__(
        self,
        produce_service: ProduceService,
        job_control: JobControl,
        legacy_video_adapter: Callable[[VideoProduceRequest], ProduceRequest],
    ) -> None:
        self._produce_service = produce_service
        self._job_control = job_control
        self._legacy_video_adapter = legacy_video_adapter

    def submit(self, request: ProduceRequest) -> ProduceSubmission:
        return self._produce_service.submit(request)

    def submit_video(self, request: VideoProduceRequest) -> JobSnapshot:
        submission = self.submit(self._legacy_video_adapter(request))
        return self._job_control.get_job(submission.job_id)

    def wait_job(self, job_id: str, event_sink: object | None = None) -> JobSnapshot:
        del event_sink
        return self._job_control.wait_job(job_id)

    def get_job(self, job_id: str) -> JobSnapshot:
        return self._job_control.get_job(job_id)

    def cancel_job(self, job_id: str) -> JobSnapshot:
        return self._job_control.cancel_job(job_id)


__all__ = ["AllToNoteSDK", "JobControl"]

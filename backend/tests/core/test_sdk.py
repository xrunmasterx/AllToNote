from __future__ import annotations

from types import SimpleNamespace

from app.core.jobs.model import JobExecutionOwner, JobState
from app.core.recipes.contracts import (
    InputDescriptor,
    ProduceRequest,
    ProduceSubmission,
    RecipeKey,
)
from app.core.sdk import AllToNoteSDK


class _ProduceServiceSpy:
    def __init__(self, submission: ProduceSubmission) -> None:
        self.submission = submission
        self.requests: list[ProduceRequest] = []
        self.execution_owners: list[JobExecutionOwner] = []

    def submit(
        self,
        request: ProduceRequest,
        *,
        execution_owner: JobExecutionOwner,
    ) -> ProduceSubmission:
        self.requests.append(request)
        self.execution_owners.append(execution_owner)
        return self.submission


class _JobControlSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.snapshot = SimpleNamespace(job_id="job_1")

    def wait_job(self, job_id: str) -> object:
        self.calls.append(("wait", job_id))
        return self.snapshot

    def get_job(self, job_id: str) -> object:
        self.calls.append(("get", job_id))
        return self.snapshot

    def cancel_job(self, job_id: str) -> object:
        self.calls.append(("cancel", job_id))
        return self.snapshot


def _request() -> ProduceRequest:
    return ProduceRequest(
        1,
        RecipeKey("alltonote.video-course-note", 1),
        InputDescriptor("source", "fixture://course"),
        "C:/vault",
        ("knowledge-note",),
        client_request_id="sdk-generic",
    )


def test_sdk_generic_submit_delegates_once() -> None:
    request = _request()
    submission = ProduceSubmission(request.recipe_key.recipe_id, request.recipe_key, JobState.QUEUED)
    produce_service = _ProduceServiceSpy(submission)
    job_control = _JobControlSpy()
    sdk = AllToNoteSDK(produce_service, job_control, lambda value: value)

    result = sdk.submit(request)

    assert result is submission
    assert produce_service.requests == [request]
    assert produce_service.execution_owners == [JobExecutionOwner.FOREGROUND]
    assert not hasattr(sdk, "_video_service")


def test_sdk_legacy_submit_uses_generic_path_and_restores_snapshot() -> None:
    generic = _request()
    submission = ProduceSubmission("job_1", generic.recipe_key, JobState.QUEUED)
    produce_service = _ProduceServiceSpy(submission)
    job_control = _JobControlSpy()
    legacy = object()
    adapted: list[object] = []

    def adapt(value: object) -> ProduceRequest:
        adapted.append(value)
        return generic

    sdk = AllToNoteSDK(produce_service, job_control, adapt)

    result = sdk.submit_video(legacy)  # type: ignore[arg-type]

    assert result is job_control.snapshot
    assert adapted == [legacy]
    assert produce_service.requests == [generic]
    assert produce_service.execution_owners == [JobExecutionOwner.FOREGROUND]
    assert job_control.calls == [("get", "job_1")]


def test_sdk_job_control_methods_keep_existing_delegation() -> None:
    request = _request()
    submission = ProduceSubmission("job_1", request.recipe_key, JobState.QUEUED)
    job_control = _JobControlSpy()
    sdk = AllToNoteSDK(_ProduceServiceSpy(submission), job_control, lambda value: value)

    assert sdk.wait_job("job_1") is job_control.snapshot
    assert sdk.get_job("job_1") is job_control.snapshot
    assert sdk.cancel_job("job_1") is job_control.snapshot
    assert job_control.calls == [
        ("wait", "job_1"),
        ("get", "job_1"),
        ("cancel", "job_1"),
    ]

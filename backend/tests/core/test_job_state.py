from __future__ import annotations

from app.core.domain.video import JobState as VideoJobState
from app.core.jobs.model import JobState
from app.core.jobs.state_machine import LEGAL_JOB_TRANSITIONS, transition_job


def test_job_state_has_one_generic_owner_and_video_reexports_same_type() -> None:
    assert JobState.__module__ == "app.core.jobs.model"
    assert VideoJobState is JobState
    assert tuple(VideoJobState) == tuple(JobState)
    assert all(video is generic for video, generic in zip(VideoJobState, JobState))


def test_job_state_serialized_values_remain_frozen() -> None:
    assert tuple((state.name, state.value) for state in JobState) == (
        ("QUEUED", "queued"),
        ("RUNNING", "running"),
        ("WAITING_FOR_INPUT", "waiting_for_input"),
        ("SUCCEEDED", "succeeded"),
        ("FAILED", "failed"),
        ("CANCELLED", "cancelled"),
    )
    assert JobState("queued") is JobState.QUEUED
    assert JobState("waiting_for_input") is JobState.WAITING_FOR_INPUT


def test_job_state_transition_set_remains_frozen() -> None:
    expected = (
        (JobState.QUEUED, JobState.RUNNING),
        (JobState.QUEUED, JobState.CANCELLED),
        (JobState.RUNNING, JobState.WAITING_FOR_INPUT),
        (JobState.RUNNING, JobState.SUCCEEDED),
        (JobState.RUNNING, JobState.FAILED),
        (JobState.RUNNING, JobState.CANCELLED),
        (JobState.WAITING_FOR_INPUT, JobState.QUEUED),
        (JobState.WAITING_FOR_INPUT, JobState.FAILED),
        (JobState.WAITING_FOR_INPUT, JobState.CANCELLED),
    )

    assert LEGAL_JOB_TRANSITIONS == expected
    assert all(transition_job(start, end) is end for start, end in expected)

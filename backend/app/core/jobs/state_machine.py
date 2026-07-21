from __future__ import annotations

from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.model import AttemptState, JobState


TERMINAL_JOB_STATES = frozenset(
    {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}
)
TERMINAL_ATTEMPT_STATES = frozenset(
    {
        AttemptState.SUCCEEDED,
        AttemptState.FAILED,
        AttemptState.CANCELLED,
        AttemptState.INTERRUPTED,
        AttemptState.NEEDS_INPUT,
        AttemptState.SKIPPED,
    }
)

LEGAL_JOB_TRANSITIONS = (
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
LEGAL_ATTEMPT_TRANSITIONS = (
    (AttemptState.PENDING, AttemptState.RUNNING),
    (AttemptState.PENDING, AttemptState.SKIPPED),
    (AttemptState.PENDING, AttemptState.CANCELLED),
    (AttemptState.RUNNING, AttemptState.SUCCEEDED),
    (AttemptState.RUNNING, AttemptState.FAILED),
    (AttemptState.RUNNING, AttemptState.CANCELLED),
    (AttemptState.RUNNING, AttemptState.INTERRUPTED),
    (AttemptState.RUNNING, AttemptState.NEEDS_INPUT),
)

_JOB_TRANSITIONS = frozenset(LEGAL_JOB_TRANSITIONS)
_ATTEMPT_TRANSITIONS = frozenset(LEGAL_ATTEMPT_TRANSITIONS)


def transition_job(start: JobState, end: JobState) -> JobState:
    if start in TERMINAL_JOB_STATES:
        raise DomainError(
            "job_terminal",
            ErrorCategory.CONFLICT,
            "Terminal Job state cannot transition",
        )
    if (start, end) not in _JOB_TRANSITIONS:
        raise DomainError(
            "job_transition_invalid",
            ErrorCategory.CONFLICT,
            "Job state transition is not allowed",
        )
    return end


def transition_attempt(start: AttemptState, end: AttemptState) -> AttemptState:
    if start in TERMINAL_ATTEMPT_STATES:
        raise DomainError(
            "attempt_terminal",
            ErrorCategory.CONFLICT,
            "Terminal Attempt state cannot transition",
        )
    if (start, end) not in _ATTEMPT_TRANSITIONS:
        raise DomainError(
            "attempt_transition_invalid",
            ErrorCategory.CONFLICT,
            "Attempt state transition is not allowed",
        )
    return end

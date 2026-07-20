from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass

from app.core.domain.video import JobSnapshot, JobState
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.model import JobEvent
from app.core.ports.job_queries import (
    JobListRecord,
    JobQueryRecord,
    JobQueryRepositoryPort,
)


DEFAULT_JOB_PAGE_SIZE = 50
MAX_JOB_PAGE_SIZE = 200
DEFAULT_EVENT_PAGE_SIZE = 100
MAX_EVENT_PAGE_SIZE = 1_000
_CURSOR_VERSION = 1
_MAX_CURSOR_LENGTH = 2_048


@dataclass(frozen=True)
class PendingChallengeView:
    challenge_id: str
    kind: str | None
    code: str | None


@dataclass(frozen=True)
class JobView:
    snapshot: JobSnapshot
    created_at: str
    updated_at: str
    pending_challenge: PendingChallengeView | None = None
    unknown_operation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "unknown_operation_ids",
            tuple(self.unknown_operation_ids),
        )


@dataclass(frozen=True)
class JobPage:
    jobs: tuple[JobView, ...]
    next_cursor: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "jobs", tuple(self.jobs))


@dataclass(frozen=True)
class JobEventPage:
    events: tuple[JobEvent, ...]
    next_after_sequence: int | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(self.events))


class JobQueryService:
    def __init__(self, repository: JobQueryRepositoryPort) -> None:
        self._repository = repository

    def get(self, job_id: str, *, principal: str) -> JobView:
        _validate_identity(job_id, "job_not_found", "Job does not exist")
        _validate_identity(
            principal,
            "job_principal_invalid",
            "Job principal must be non-empty text",
        )
        return _job_view(self._repository.query_job(job_id, principal=principal))

    def list(
        self,
        *,
        principal: str,
        states: tuple[JobState, ...] = (),
        cursor: str | None = None,
        limit: int = DEFAULT_JOB_PAGE_SIZE,
    ) -> JobPage:
        _validate_identity(
            principal,
            "job_principal_invalid",
            "Job principal must be non-empty text",
        )
        normalized_states = _normalize_states(states)
        _validate_limit(limit, MAX_JOB_PAGE_SIZE, "job_list_limit_invalid")
        before_created_at: str | None = None
        before_job_id: str | None = None
        if cursor is not None:
            before_created_at, before_job_id = _decode_cursor(
                cursor,
                states=normalized_states,
            )
        records = self._repository.query_jobs(
            principal=principal,
            states=normalized_states,
            before_created_at=before_created_at,
            before_job_id=before_job_id,
            limit=limit + 1,
        )
        visible = records[:limit]
        jobs = tuple(_listed_job_view(record) for record in visible)
        next_cursor = (
            _encode_cursor(visible[-1], states=normalized_states)
            if len(records) > limit and visible
            else None
        )
        return JobPage(jobs=jobs, next_cursor=next_cursor)

    def events(
        self,
        job_id: str,
        *,
        principal: str,
        after_sequence: int = 0,
        limit: int = DEFAULT_EVENT_PAGE_SIZE,
    ) -> JobEventPage:
        _validate_identity(job_id, "job_not_found", "Job does not exist")
        _validate_identity(
            principal,
            "job_principal_invalid",
            "Job principal must be non-empty text",
        )
        if (
            type(after_sequence) is not int
            or after_sequence < 0
        ):
            raise DomainError(
                "job_event_sequence_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Job event sequence must be a non-negative integer",
            )
        _validate_limit(limit, MAX_EVENT_PAGE_SIZE, "job_event_limit_invalid")
        records = self._repository.query_job_events(
            job_id,
            principal=principal,
            after_sequence=after_sequence,
            limit=limit + 1,
        )
        events = records[:limit]
        return JobEventPage(
            events=events,
            next_after_sequence=(
                events[-1].sequence if len(records) > limit and events else None
            ),
        )


def _validate_identity(value: object, code: str, message: str) -> None:
    if type(value) is not str or not value.strip() or len(value) > 512:
        raise DomainError(code, ErrorCategory.INVALID_REQUEST, message)


def _validate_limit(value: object, maximum: int, code: str) -> None:
    if type(value) is not int or value < 1 or value > maximum:
        raise DomainError(
            code,
            ErrorCategory.INVALID_REQUEST,
            f"Page size must be between 1 and {maximum}",
        )


def _normalize_states(states: tuple[JobState, ...]) -> tuple[JobState, ...]:
    if type(states) is not tuple or any(type(state) is not JobState for state in states):
        raise DomainError(
            "job_state_filter_invalid",
            ErrorCategory.INVALID_REQUEST,
            "Job state filter is invalid",
        )
    return tuple(sorted(set(states), key=lambda state: state.value))


def _snapshot_from_record(record: JobQueryRecord | JobListRecord) -> JobSnapshot:
    job = record.job
    result = record.result
    error = record.error
    if job.state is JobState.SUCCEEDED:
        if result is None or error is not None:
            _raise_projection_invalid()
    elif job.state is JobState.FAILED:
        if error is None or result is not None:
            _raise_projection_invalid()
    elif result is not None or error is not None:
        _raise_projection_invalid()
    if isinstance(record, JobQueryRecord):
        active_attempt_id = (
            record.active_attempt.attempt_id
            if record.active_attempt is not None
            else None
        )
        challenge_id = (
            record.pending_challenge.challenge_id
            if record.pending_challenge is not None
            else None
        )
    else:
        active_attempt_id = None
        challenge_id = None
    return JobSnapshot(
        job_id=job.job_id,
        state=job.state,
        cancellation_requested=job.cancellation_requested,
        active_attempt_id=active_attempt_id,
        challenge_id=challenge_id,
        retry_of_job_id=job.retry_of_job_id,
        result=result,
        error=error,
    )


def _raise_projection_invalid() -> None:
    raise DomainError(
        "job_projection_invalid",
        ErrorCategory.INTERNAL,
        "Stored Job state and result projection are inconsistent",
    )


def _challenge_view(record: JobQueryRecord) -> PendingChallengeView | None:
    challenge = record.pending_challenge
    if challenge is None:
        if record.job.state is JobState.WAITING_FOR_INPUT:
            _raise_projection_invalid()
        return None
    if record.job.state is not JobState.WAITING_FOR_INPUT:
        _raise_projection_invalid()
    try:
        payload = json.loads(challenge.prompt_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        _raise_projection_invalid()
    if type(payload) is not dict:
        _raise_projection_invalid()
    kind = payload.get("kind")
    code = payload.get("code")
    if kind is not None and (type(kind) is not str or not kind.strip()):
        _raise_projection_invalid()
    if code is not None and (type(code) is not str or not code.strip()):
        _raise_projection_invalid()
    return PendingChallengeView(
        challenge_id=challenge.challenge_id,
        kind=kind,
        code=code,
    )


def _job_view(record: JobQueryRecord) -> JobView:
    return JobView(
        snapshot=_snapshot_from_record(record),
        created_at=record.job.created_at,
        updated_at=record.job.updated_at,
        pending_challenge=_challenge_view(record),
        unknown_operation_ids=record.unknown_operation_ids,
    )


def _listed_job_view(record: JobListRecord) -> JobView:
    return JobView(
        snapshot=_snapshot_from_record(record),
        created_at=record.job.created_at,
        updated_at=record.job.updated_at,
    )


def _encode_cursor(
    record: JobListRecord,
    *,
    states: tuple[JobState, ...],
) -> str:
    payload = json.dumps(
        {
            "created_at": record.job.created_at,
            "job_id": record.job.job_id,
            "states": [state.value for state in states],
            "version": _CURSOR_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(
    cursor: str,
    *,
    states: tuple[JobState, ...],
) -> tuple[str, str]:
    try:
        if (
            type(cursor) is not str
            or not cursor
            or len(cursor) > _MAX_CURSOR_LENGTH
        ):
            raise ValueError
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.b64decode(
            padded,
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(raw.decode("ascii"))
        expected_states = [state.value for state in states]
        if (
            type(payload) is not dict
            or frozenset(payload)
            != frozenset({"created_at", "job_id", "states", "version"})
            or type(payload["version"]) is not int
            or payload["version"] != _CURSOR_VERSION
            or type(payload["created_at"]) is not str
            or not payload["created_at"]
            or type(payload["job_id"]) is not str
            or not payload["job_id"]
            or type(payload["states"]) is not list
            or payload["states"] != expected_states
        ):
            raise ValueError
        return payload["created_at"], payload["job_id"]
    except (
        binascii.Error,
        UnicodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ):
        raise DomainError(
            "job_cursor_invalid",
            ErrorCategory.INVALID_REQUEST,
            "Job cursor is invalid for the current query",
        ) from None


__all__ = [
    "DEFAULT_EVENT_PAGE_SIZE",
    "DEFAULT_JOB_PAGE_SIZE",
    "JobEventPage",
    "JobPage",
    "JobQueryService",
    "JobView",
    "MAX_EVENT_PAGE_SIZE",
    "MAX_JOB_PAGE_SIZE",
    "PendingChallengeView",
]

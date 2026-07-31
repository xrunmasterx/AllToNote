from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import pytest

from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.core.domain.video import JobState, RetryJobRequest
from app.core.errors import DomainError, ErrorCategory, ErrorDetail
from app.core.jobs.model import AttemptState, JobExecutionOwner


@dataclass(frozen=True)
class _CanonicalRequest:
    path: Path
    mode: JobState
    payload: dict[str, object]
    principal: str
    client_request_id: str | None


class _SecretLike:
    def __init__(self, value: str) -> None:
        self.value = value

    def __repr__(self) -> str:
        return f"_SecretLike({self.value})"


class _SecretToken(StrEnum):
    VALUE = "synthetic-secret-token"


class _ApiKey(StrEnum):
    VALUE = "synthetic-api-key"


class _CredentialKind(StrEnum):
    API_KEY = "synthetic-member-secret"


class _SafeMode(StrEnum):
    STANDARD = "standard"


@pytest.fixture
def repo(tmp_path: Path) -> SqliteJobRepository:
    return SqliteJobRepository.open(tmp_path / "machine-root")


def _new_service(repo: SqliteJobRepository):
    module = importlib.import_module("app.core.application.job_service")
    return module.JobService(repo)


def _authority(repo: SqliteJobRepository):
    return repo.acquire_scheduler_lease(
        "test-workspace:test-process", ttl_seconds=300
    )


def _request(
    tmp_path: Path,
    *,
    client_request_id: str | None = "submit-1",
    principal: str = "agent",
    payload: dict[str, object] | None = None,
) -> _CanonicalRequest:
    return _CanonicalRequest(
        path=tmp_path / "工作区",
        mode=JobState.QUEUED,
        payload=payload or {"z": (2, "雪"), "a": {"enabled": True}},
        principal=principal,
        client_request_id=client_request_id,
    )


def _failed_job(
    repo: SqliteJobRepository,
    *,
    operation_ids: tuple[str, ...] = (),
):
    job = repo.create_job(
        request_hash="sha256:" + "a" * 64,
        principal="agent",
        client_request_id="original",
    )
    repo.transition_job(job.job_id, JobState.RUNNING)
    authority = _authority(repo)
    attempt = repo.create_attempt(job.job_id, "model")
    attempt = repo.start_attempt(attempt.attempt_id, authority)
    with repo._transaction(immediate=True) as connection:
        for operation_id in operation_ids:
            connection.execute(
                """
                INSERT INTO external_operations (
                    operation_id, job_id, step_id, attempt_id, provider,
                    request_hash, operation_idempotency_key,
                    provider_request_id, outcome, summary_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'provider', ?, NULL, NULL,
                          'external_outcome_unknown', '{}', 'created', 'updated')
                """,
                (
                    operation_id,
                    job.job_id,
                    attempt.step_id,
                    attempt.attempt_id,
                    job.request_hash,
                ),
            )
    repo.transition_attempt(
        attempt.attempt_id, AttemptState.FAILED, authority=authority
    )
    return repo.transition_job(job.job_id, JobState.FAILED)


def _waiting_job(repo: SqliteJobRepository):
    job = repo.create_job(
        request_hash="sha256:" + "b" * 64,
        principal="agent",
        client_request_id="waiting",
    )
    repo.transition_job(job.job_id, JobState.RUNNING)
    authority = _authority(repo)
    attempt = repo.create_attempt(job.job_id, "acquire")
    attempt = repo.start_attempt(attempt.attempt_id, authority)
    attempt = repo.transition_attempt(
        attempt.attempt_id, AttemptState.NEEDS_INPUT, authority=authority
    )
    challenge = repo.create_challenge(job.job_id, attempt.attempt_id, "{}")
    return job, attempt, challenge


def test_submit_hashes_sorted_compact_utf8_canonical_request(
    repo: SqliteJobRepository,
    tmp_path: Path,
) -> None:
    service = _new_service(repo)
    request = _request(tmp_path)
    expected_json = json.dumps(
        {
            "mode": "queued",
            "path": str(tmp_path / "工作区"),
            "payload": {"a": {"enabled": True}, "z": [2, "雪"]},
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    expected_hash = "sha256:" + hashlib.sha256(
        expected_json.encode("utf-8")
    ).hexdigest()

    snapshot = service.submit(request)

    assert repo.get_job(snapshot.job_id).request_hash == expected_hash
    assert repo.get_job_request(snapshot.job_id) == expected_json


def test_submit_persists_explicit_execution_owner(
    repo: SqliteJobRepository,
    tmp_path: Path,
) -> None:
    service = _new_service(repo)

    snapshot = service.submit(
        _request(tmp_path),
        execution_owner=JobExecutionOwner.ENGINE,
    )

    assert repo.get_job(snapshot.job_id).execution_owner is JobExecutionOwner.ENGINE


def test_get_restores_persisted_failure_detail(
    repo: SqliteJobRepository,
    tmp_path: Path,
) -> None:
    service = _new_service(repo)
    submitted = service.submit(_request(tmp_path))
    repo.transition_job(submitted.job_id, JobState.RUNNING)
    error = ErrorDetail(
        "preflight_failed",
        ErrorCategory.WORKSPACE_INCOMPATIBLE,
        "Preflight failed",
        {"check": "workspace"},
    )
    repo.fail_job_atomic(submitted.job_id, error)

    snapshot = _new_service(repo).get(submitted.job_id)

    assert snapshot.state is JobState.FAILED
    assert snapshot.error == error


def test_submit_hash_excludes_principal_and_client_request_id(
    repo: SqliteJobRepository,
    tmp_path: Path,
) -> None:
    service = _new_service(repo)

    first = service.submit(_request(tmp_path, principal="agent-a", client_request_id="a"))
    second = service.submit(_request(tmp_path, principal="agent-b", client_request_id="b"))

    assert repo.get_job(first.job_id).request_hash == repo.get_job(second.job_id).request_hash


def test_submit_delegates_durable_idempotency_to_repository(
    repo: SqliteJobRepository,
    tmp_path: Path,
) -> None:
    service = _new_service(repo)

    first = service.submit(_request(tmp_path, payload={"b": 2, "a": 1}))
    replay = service.submit(_request(tmp_path, payload={"a": 1, "b": 2}))

    assert replay.job_id == first.job_id
    with pytest.raises(DomainError, match="idempotency_conflict"):
        service.submit(_request(tmp_path, payload={"a": 1, "b": 3}))


@pytest.mark.parametrize(
    "invalid_payload",
    (
        {1: "non-string key"},
        {"value": float("nan")},
        {"value": object()},
    ),
)
def test_submit_rejects_noncanonical_values_with_stable_safe_error(
    repo: SqliteJobRepository,
    tmp_path: Path,
    invalid_payload: dict[object, object],
) -> None:
    service = _new_service(repo)

    with pytest.raises(DomainError) as caught:
        service.submit(_request(tmp_path, payload=invalid_payload))

    assert caught.value.code == "request_canonicalization_invalid"
    assert caught.value.category is ErrorCategory.INVALID_REQUEST
    assert caught.value.details == {}


def test_secret_like_value_never_enters_hash_database_or_error(
    repo: SqliteJobRepository,
    tmp_path: Path,
) -> None:
    service = _new_service(repo)
    secret = "sk-never-persist-this"

    with pytest.raises(DomainError) as caught:
        service.submit(_request(tmp_path, payload={"credential": _SecretLike(secret)}))

    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
    assert caught.value.details == {}
    assert secret.encode("utf-8") not in repo.database_path.read_bytes()


@pytest.mark.parametrize(
    "secret_field",
    ("api_key", "Authorization", "cookie", "access-token"),
)
def test_secret_named_field_never_enters_hash_database_or_error(
    repo: SqliteJobRepository,
    tmp_path: Path,
    secret_field: str,
) -> None:
    service = _new_service(repo)
    secret = "secret-value-never-hash"

    with pytest.raises(DomainError) as caught:
        service.submit(_request(tmp_path, payload={secret_field: secret}))

    assert caught.value.code == "request_canonicalization_invalid"
    assert caught.value.category is ErrorCategory.INVALID_REQUEST
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
    assert secret.encode("utf-8") not in repo.database_path.read_bytes()


@pytest.mark.parametrize(
    "secret_field",
    (
        "X-API-Key",
        "auth_token",
        "set-cookie",
        "xapikey",
        "authtoken",
        "setcookie",
        "X-Auth-Token",
        "clientSecret",
        "aws_secret_access_key",
        "apiToken",
        "privateKey",
        "bearerToken",
    ),
)
def test_secret_field_alias_is_rejected_by_submit_and_respond(
    repo: SqliteJobRepository,
    tmp_path: Path,
    secret_field: str,
) -> None:
    service = _new_service(repo)
    secret = "secret-alias-value-never-store"

    with pytest.raises(DomainError) as submit_error:
        service.submit(_request(tmp_path, payload={secret_field: secret}))

    job, _, challenge = _waiting_job(repo)
    with pytest.raises(DomainError) as respond_error:
        service.respond(job.job_id, challenge.challenge_id, {secret_field: secret})

    for caught in (submit_error, respond_error):
        assert caught.value.code == "request_canonicalization_invalid"
        assert caught.value.category is ErrorCategory.INVALID_REQUEST
        assert caught.value.details == {}
        assert secret_field not in str(caught.value)
        assert secret_field not in repr(caught.value)
        assert secret not in str(caught.value)
        assert secret not in repr(caught.value)
    with repo._connect() as connection:
        challenge_row = connection.execute(
            "SELECT state, response_json FROM challenges WHERE challenge_id = ?",
            (challenge.challenge_id,),
        ).fetchone()

    assert tuple(challenge_row) == ("pending", None)
    assert secret.encode("utf-8") not in repo.database_path.read_bytes()
    assert service.get(job.job_id).challenge_id == challenge.challenge_id


def test_non_secret_identifier_containing_key_text_is_not_rejected(
    repo: SqliteJobRepository,
    tmp_path: Path,
) -> None:
    service = _new_service(repo)

    snapshot = service.submit(_request(tmp_path, payload={"monkey": "banana"}))

    assert snapshot.state is JobState.QUEUED


@pytest.mark.parametrize(
    "secret_member",
    (_SecretToken.VALUE, _ApiKey.VALUE, _CredentialKind.API_KEY),
)
def test_secret_like_strenum_is_rejected_by_submit_and_respond(
    repo: SqliteJobRepository,
    tmp_path: Path,
    secret_member: StrEnum,
) -> None:
    service = _new_service(repo)
    secret = str(secret_member)

    with pytest.raises(DomainError) as submit_error:
        service.submit(_request(tmp_path, payload={"credential": secret_member}))

    job, _, challenge = _waiting_job(repo)
    with pytest.raises(DomainError) as respond_error:
        service.respond(
            job.job_id,
            challenge.challenge_id,
            {"credential": secret_member},
        )

    for caught in (submit_error, respond_error):
        assert caught.value.code == "request_canonicalization_invalid"
        assert caught.value.category is ErrorCategory.INVALID_REQUEST
        assert caught.value.details == {}
        assert type(secret_member).__name__ not in str(caught.value)
        assert type(secret_member).__qualname__ not in repr(caught.value)
        assert secret_member.name not in str(caught.value)
        assert secret not in repr(caught.value)
    assert secret.encode("utf-8") not in repo.database_path.read_bytes()
    assert service.get(job.job_id).challenge_id == challenge.challenge_id


def test_non_secret_custom_strenum_remains_canonicalizable(
    repo: SqliteJobRepository,
    tmp_path: Path,
) -> None:
    service = _new_service(repo)

    submitted = service.submit(
        _request(tmp_path, payload={"processing_mode": _SafeMode.STANDARD})
    )
    job, _, challenge = _waiting_job(repo)
    resumed = service.respond(
        job.job_id,
        challenge.challenge_id,
        {"processing_mode": _SafeMode.STANDARD},
    )

    assert submitted.state is JobState.QUEUED
    assert resumed.state is JobState.QUEUED


def test_get_projects_pending_challenge_and_response_attempt(
    repo: SqliteJobRepository,
) -> None:
    service = _new_service(repo)
    job, _, challenge = _waiting_job(repo)

    waiting = service.get(job.job_id)
    resumed = service.respond(
        job.job_id,
        challenge.challenge_id,
        {"credential_profile": "配置"},
    )

    assert waiting.state is JobState.WAITING_FOR_INPUT
    assert waiting.challenge_id == challenge.challenge_id
    assert waiting.active_attempt_id is None
    assert resumed.state is JobState.QUEUED
    assert resumed.challenge_id is None
    assert resumed.active_attempt_id is not None


def test_challenge_response_is_canonical_hash_idempotent(
    repo: SqliteJobRepository,
) -> None:
    service = _new_service(repo)
    job, _, challenge = _waiting_job(repo)

    first = service.respond(
        job.job_id,
        challenge.challenge_id,
        {"z": "雪", "credential_profile": "p"},
    )
    replay = service.respond(
        job.job_id,
        challenge.challenge_id,
        {"credential_profile": "p", "z": "雪"},
    )

    assert replay.active_attempt_id == first.active_attempt_id
    with pytest.raises(DomainError, match="challenge_response_conflict"):
        service.respond(
            job.job_id,
            challenge.challenge_id,
            {"credential_profile": "different", "z": "雪"},
        )


def test_secret_like_challenge_response_is_rejected_before_consumption(
    repo: SqliteJobRepository,
) -> None:
    service = _new_service(repo)
    job, _, challenge = _waiting_job(repo)
    secret = "cookie=never-store"

    with pytest.raises(DomainError) as caught:
        service.respond(
            job.job_id,
            challenge.challenge_id,
            {"credential": _SecretLike(secret)},
        )

    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
    assert secret.encode("utf-8") not in repo.database_path.read_bytes()
    assert service.get(job.job_id).challenge_id == challenge.challenge_id


def test_cancel_is_stable_and_terminal_state_is_immutable(
    repo: SqliteJobRepository,
    tmp_path: Path,
) -> None:
    service = _new_service(repo)
    queued = service.submit(_request(tmp_path, client_request_id="cancel-me"))

    cancelled = service.cancel(queued.job_id)
    replay = service.cancel(queued.job_id)

    assert cancelled.state is JobState.CANCELLED
    assert cancelled.cancellation_requested is True
    assert replay == cancelled


def test_retry_creates_new_job_preserves_lineage_and_replays(
    repo: SqliteJobRepository,
) -> None:
    service = _new_service(repo)
    original = _failed_job(repo, operation_ids=("op_a", "op_b"))
    request = RetryJobRequest(
        1,
        "retry-1",
        JobState.FAILED,
        ("op_b", "op_a"),
    )

    retried = service.retry(original.job_id, request)
    replay = service.retry(original.job_id, request)

    assert retried.job_id != original.job_id
    assert retried.retry_of_job_id == original.job_id
    assert replay.job_id == retried.job_id
    assert service.get(original.job_id).state is JobState.FAILED


def test_retry_persists_config_snapshot_atomically_and_replay_checks_it(
    repo: SqliteJobRepository,
) -> None:
    service = _new_service(repo)
    original = _failed_job(repo)
    request = RetryJobRequest(1, "retry-config", JobState.FAILED)
    first_snapshot = {
        "snapshot_version": 1,
        "values": {"quality": "balanced"},
        "digest": "sha256:" + "1" * 64,
        "semantic_digest": "sha256:" + "2" * 64,
    }

    retried = service.retry(
        original.job_id,
        request,
        initial_events=(("configuration.snapshot.v1", first_snapshot),),
    )
    replay = service.retry(
        original.job_id,
        request,
        initial_events=(("configuration.snapshot.v1", first_snapshot),),
    )

    assert replay.job_id == retried.job_id
    events = repo.list_events(retried.job_id)
    assert [event.event_type for event in events] == [
        "configuration.snapshot.v1"
    ]
    with pytest.raises(DomainError, match="idempotency_conflict"):
        service.retry(
            original.job_id,
            request,
            initial_events=(
                (
                    "configuration.snapshot.v1",
                    {
                        **first_snapshot,
                        "semantic_digest": "sha256:" + "3" * 64,
                    },
                ),
            ),
        )
    assert len(repo.list_events(retried.job_id)) == 1


@pytest.mark.parametrize(
    ("retry_request", "error_code"),
    (
        (RetryJobRequest(2, "retry-version", JobState.FAILED), "retry_request_schema_unsupported"),
        (RetryJobRequest(True, "retry-bool", JobState.FAILED), "retry_request_schema_unsupported"),
        (RetryJobRequest(1, "", JobState.FAILED), "retry_client_request_id_invalid"),
        (RetryJobRequest(1, "retry-state", "failed"), "retry_expected_state_invalid"),
        (
            RetryJobRequest(
                1, "retry-duplicate", JobState.FAILED, ("op_a", "op_a")
            ),
            "retry_unknown_operations_invalid",
        ),
        ({"retry_request_schema_version": 1}, "retry_request_invalid"),
    ),
)
def test_retry_validation_rejects_before_creating_job(
    repo: SqliteJobRepository,
    retry_request: object,
    error_code: str,
) -> None:
    service = _new_service(repo)
    original = _failed_job(repo, operation_ids=("op_a",))

    with pytest.raises(DomainError, match=error_code):
        service.retry(original.job_id, retry_request)

    with repo._connect() as connection:
        retry_count = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE retry_of_job_id = ?",
            (original.job_id,),
        ).fetchone()[0]
    assert retry_count == 0

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import StrEnum
from pathlib import Path

from app.core.domain.video import JobSnapshot, JobState, RetryJobRequest
from app.core.errors import DomainError, ErrorCategory
from app.core.ports.jobs import JobRepositoryPort


_IDEMPOTENCY_FIELDS = frozenset({"principal", "client_request_id"})
_SECRET_FIELD_NAMES = frozenset(
    {
        "accesstoken",
        "apikey",
        "authorization",
        "cookie",
        "password",
        "refreshtoken",
        "secret",
        "token",
    }
)
_SECRET_IDENTIFIER_TOKENS = frozenset(
    {
        "authorization",
        "bearer",
        "cookie",
        "passwd",
        "password",
        "secret",
        "token",
    }
)
_SECRET_KEY_QUALIFIERS = frozenset(
    {"access", "api", "client", "encryption", "private", "signing"}
)
_IDENTIFIER_SEGMENT_PATTERN = re.compile(r"[A-Za-z0-9]+")
_IDENTIFIER_TOKEN_PATTERN = re.compile(
    r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+"
)


def _identifier_tokens(identifier: str) -> tuple[str, ...]:
    return tuple(
        token.group(0).casefold()
        for segment in _IDENTIFIER_SEGMENT_PATTERN.finditer(identifier)
        for token in _IDENTIFIER_TOKEN_PATTERN.finditer(segment.group(0))
    )


def _is_secret_identifier(identifier: str) -> bool:
    normalized = "".join(
        character for character in identifier.casefold() if character.isalnum()
    )
    if normalized in _SECRET_FIELD_NAMES:
        return True
    tokens = frozenset(_identifier_tokens(identifier))
    return bool(tokens & _SECRET_IDENTIFIER_TOKENS) or (
        "key" in tokens and bool(tokens & _SECRET_KEY_QUALIFIERS)
    )


def _canonical_value(value: object, *, excluded_fields: frozenset[str]) -> object:
    if value is None or type(value) in (bool, int, float, str):
        return value
    if isinstance(value, StrEnum):
        enum_type = type(value)
        if any(
            _is_secret_identifier(identifier)
            for identifier in (enum_type.__name__, enum_type.__qualname__, value.name)
        ):
            raise TypeError
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        parameters = getattr(type(value), "__dataclass_params__", None)
        if parameters is None or not parameters.frozen:
            raise TypeError
        normalized: dict[str, object] = {}
        for field in fields(value):
            if field.name in excluded_fields:
                continue
            if _is_secret_identifier(field.name):
                raise TypeError
            normalized[field.name] = _canonical_value(
                getattr(value, field.name), excluded_fields=frozenset()
            )
        return normalized
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError
            if key not in excluded_fields:
                if _is_secret_identifier(key):
                    raise TypeError
                normalized[key] = _canonical_value(
                    item, excluded_fields=frozenset()
                )
        return normalized
    if isinstance(value, tuple):
        return [
            _canonical_value(item, excluded_fields=frozenset()) for item in value
        ]
    raise TypeError


def _canonical_json(
    value: object, *, excluded_fields: frozenset[str] = frozenset()
) -> str:
    try:
        normalized = _canonical_value(value, excluded_fields=excluded_fields)
        return json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError):
        raise DomainError(
            "request_canonicalization_invalid",
            ErrorCategory.INVALID_REQUEST,
            "Request contains a value that cannot be canonicalized",
        ) from None


def _sha256_json(canonical_json: str) -> str:
    return "sha256:" + hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _request_field(request: object, field_name: str) -> object:
    if is_dataclass(request) and not isinstance(request, type):
        if any(field.name == field_name for field in fields(request)):
            return getattr(request, field_name)
    elif isinstance(request, Mapping):
        if field_name in request:
            return request[field_name]
    raise DomainError(
        "request_canonicalization_invalid",
        ErrorCategory.INVALID_REQUEST,
        "Request is missing required idempotency metadata",
    )


class JobService:
    def __init__(self, repository: JobRepositoryPort) -> None:
        self._repository = repository

    def submit(self, request: object) -> JobSnapshot:
        principal = _request_field(request, "principal")
        client_request_id = _request_field(request, "client_request_id")
        if type(principal) is not str or not principal.strip():
            raise DomainError(
                "request_principal_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Request principal must be non-empty text",
            )
        if client_request_id is not None and (
            type(client_request_id) is not str or not client_request_id.strip()
        ):
            raise DomainError(
                "client_request_id_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Client request ID must be non-empty text",
            )
        request_json = _canonical_json(
            request, excluded_fields=_IDEMPOTENCY_FIELDS
        )
        job = self._repository.create_job(
            request_hash=_sha256_json(request_json),
            principal=principal,
            client_request_id=client_request_id,
        )
        return self.get(job.job_id)

    def get(self, job_id: str) -> JobSnapshot:
        job, active_attempt, pending_challenge = (
            self._repository.get_job_details(job_id)
        )
        return JobSnapshot(
            job_id=job.job_id,
            state=job.state,
            active_attempt_id=(
                active_attempt.attempt_id if active_attempt is not None else None
            ),
            challenge_id=(
                pending_challenge.challenge_id
                if pending_challenge is not None
                else None
            ),
            retry_of_job_id=job.retry_of_job_id,
            result=None,
            error=None,
        )

    def cancel(self, job_id: str) -> JobSnapshot:
        job = self._repository.cancel_job(job_id)
        return self.get(job.job_id)

    def respond(
        self,
        job_id: str,
        challenge_id: str,
        response: object,
    ) -> JobSnapshot:
        response_json = _canonical_json(response)
        self._repository.respond_challenge_atomic(
            job_id,
            challenge_id,
            response_hash=_sha256_json(response_json),
            response_json=response_json,
        )
        return self.get(job_id)

    def retry(
        self,
        original_job_id: str,
        retry_request: object,
    ) -> JobSnapshot:
        if type(retry_request) is not RetryJobRequest:
            raise DomainError(
                "retry_request_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Retry request must use the versioned retry schema",
            )
        if (
            type(retry_request.retry_request_schema_version) is not int
            or retry_request.retry_request_schema_version != 1
        ):
            raise DomainError(
                "retry_request_schema_unsupported",
                ErrorCategory.INVALID_REQUEST,
                "Retry request schema version is not supported",
            )
        if (
            type(retry_request.client_request_id) is not str
            or not retry_request.client_request_id.strip()
        ):
            raise DomainError(
                "retry_client_request_id_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Retry client request ID must be non-empty text",
            )
        if type(retry_request.expected_original_job_state) is not JobState:
            raise DomainError(
                "retry_expected_state_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Retry expected state must be a Job state",
            )
        confirmed = retry_request.confirmed_unknown_operation_ids
        if (
            any(type(operation_id) is not str or not operation_id for operation_id in confirmed)
            or len(confirmed) != len(set(confirmed))
        ):
            raise DomainError(
                "retry_unknown_operations_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Unknown operation confirmations must be unique IDs",
            )
        job = self._repository.create_retry_job_atomic(
            original_job_id,
            expected_original_state=retry_request.expected_original_job_state,
            confirmed_unknown_operation_ids=confirmed,
            client_request_id=retry_request.client_request_id,
        )
        return self.get(job.job_id)


__all__ = ["JobService"]

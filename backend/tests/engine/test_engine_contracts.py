from __future__ import annotations

import json

import pytest

from app.core.errors import DomainError, ErrorCategory
from app.engine.contracts import (
    ENGINE_PROTOCOL_VERSION,
    EngineJobReference,
    EngineDescriptor,
    EngineProtocolError,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
)
from app.engine.errors import (
    ENGINE_REMOTE_ERROR_CATEGORIES,
    engine_remote_error_code,
)


def _descriptor() -> EngineDescriptor:
    return EngineDescriptor(
        descriptor_version=1,
        engine_protocol_version=ENGINE_PROTOCOL_VERSION,
        runtime_major=0,
        scope_id="a" * 64,
        engine_id="018f0000-0000-7000-8000-000000000001",
        pid=1234,
        process_start_identity="windows-filetime:133700000000000000",
        endpoint_kind="windows-named-pipe",
        endpoint_name=r"\\.\pipe\alltonote-engine-" + "a" * 32,
        nonce="b" * 43,
        started_at="2026-08-01T00:00:00Z",
    )


def test_engine_remote_errors_have_one_shared_category_authority() -> None:
    for code, category in ENGINE_REMOTE_ERROR_CATEGORIES.items():
        assert engine_remote_error_code(
            DomainError(code, category, "private detail")
        ) == code

    assert engine_remote_error_code(
        DomainError(
            "workspace_instance_not_found",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
            "drifted category",
        )
    ) == "engine_internal_error"
    assert engine_remote_error_code(
        DomainError(
            "unknown_remote_error",
            ErrorCategory.RETRYABLE_RUNTIME,
            "unknown code",
        )
    ) == "engine_internal_error"


def test_engine_descriptor_round_trips_exact_canonical_fields() -> None:
    descriptor = _descriptor()

    encoded = descriptor.to_bytes()

    assert encoded.endswith(b"\n")
    assert b"\r" not in encoded
    assert EngineDescriptor.from_bytes(encoded) == descriptor
    assert set(json.loads(encoded)) == {
        "descriptor_version",
        "engine_protocol_version",
        "runtime_major",
        "scope_id",
        "engine_id",
        "pid",
        "process_start_identity",
        "endpoint_kind",
        "endpoint_name",
        "nonce",
        "started_at",
    }
    assert descriptor.nonce not in repr(descriptor)


@pytest.mark.parametrize(
    "mutation",
    (
        lambda payload: payload.pop("engine_id"),
        lambda payload: payload.update({"unknown": True}),
        lambda payload: payload.update({"pid": True}),
        lambda payload: payload.update(
            {"engine_protocol_version": ENGINE_PROTOCOL_VERSION + 1}
        ),
        lambda payload: payload.update({"nonce": "short"}),
    ),
)
def test_engine_descriptor_rejects_invalid_schema(mutation) -> None:
    payload = json.loads(_descriptor().to_bytes())
    mutation(payload)

    with pytest.raises(EngineProtocolError):
        EngineDescriptor.from_bytes(json.dumps(payload).encode("utf-8"))


def test_engine_request_round_trip_is_bounded_strict_json() -> None:
    encoded = encode_request(
        request_id="req_018f0000000070008000000000000001",
        method="health",
        nonce="b" * 43,
        params={},
    )

    request = decode_request(encoded)

    assert request.engine_protocol_version == ENGINE_PROTOCOL_VERSION
    assert request.method == "health"
    assert request.params == {}


def test_job_notify_round_trip_contains_only_two_persisted_identifiers() -> None:
    reference = EngineJobReference(
        workspace_instance_id="a" * 32,
        job_id="job_018f0000-0000-7000-8000-000000000001",
    )

    encoded = encode_request(
        request_id="req_018f0000000070008000000000000001",
        method="job.notify",
        nonce="b" * 43,
        params={
            "workspace_instance_id": reference.workspace_instance_id,
            "job_id": reference.job_id,
        },
    )
    request = decode_request(encoded)

    assert request.method == "job.notify"
    assert request.job_reference == reference


@pytest.mark.parametrize(
    "params",
    (
        {},
        {"workspace_instance_id": "a" * 32},
        {
            "workspace_instance_id": "a" * 32,
            "job_id": "job_018f0000-0000-7000-8000-000000000001",
            "workspace_root": "C:/private/workspace",
        },
        {
            "workspace_instance_id": True,
            "job_id": "job_018f0000-0000-7000-8000-000000000001",
        },
        {
            "workspace_instance_id": "not-an-instance",
            "job_id": "job_018f0000-0000-7000-8000-000000000001",
        },
        {
            "workspace_instance_id": "a" * 32,
            "job_id": "not-a-job",
        },
    ),
)
def test_job_notify_rejects_missing_extra_or_invalid_references(params) -> None:
    with pytest.raises(EngineProtocolError):
        encode_request(
            request_id="request",
            method="job.notify",
            nonce="b" * 43,
            params=params,
        )


@pytest.mark.parametrize("method", ("hello", "health", "shutdown"))
def test_lifecycle_requests_reject_nonempty_params(method: str) -> None:
    with pytest.raises(EngineProtocolError):
        encode_request(
            request_id="request",
            method=method,
            nonce="b" * 43,
            params={"unexpected": True},
        )


def test_engine_error_response_contains_only_a_stable_code() -> None:
    encoded = encode_response(
        request_id="request",
        error={"code": "engine_notification_backpressure"},
    )

    decoded = decode_response(encoded, request_id="request")

    assert decoded["error"] == {"code": "engine_notification_backpressure"}
    with pytest.raises(EngineProtocolError):
        encode_response(
            request_id="request",
            error={
                "code": "engine_notification_backpressure",
                "message": "private detail",
            },
        )


@pytest.mark.parametrize(
    "payload",
    (
        b"",
        b"[]",
        b'{"engine_protocol_version":1}',
        b'{"engine_protocol_version":1,"request_id":"x","method":"unknown",'
        b'"nonce":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","params":{}}',
        b'{"engine_protocol_version":1,"request_id":"x","request_id":"y",'
        b'"method":"health","nonce":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
        b'"params":{}}',
        b"{\xff}",
    ),
)
def test_engine_request_rejects_malformed_or_unknown_messages(payload: bytes) -> None:
    with pytest.raises(EngineProtocolError):
        decode_request(payload)

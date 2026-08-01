from __future__ import annotations

import json

import pytest

from app.core.jobs.resource_lease import (
    JobExecutionAuthority,
    ResourceLeaseHandoff,
    ResourceOwner,
)
from app.engine.contracts import (
    EngineJobReference,
    EngineProtocolError,
    EngineWorkerLaunchV1,
)


REFERENCE = EngineJobReference(
    "1" * 32,
    "job_018f0000-0000-7000-8000-000000000001",
)
OWNER = ResourceOwner("workspace-id", "engine-worker-1")
HANDOFF = ResourceLeaseHandoff(
    handoff_version=1,
    resource_name="produce:heavy:v1",
    owner=OWNER,
    fencing_token=7,
    expires_at_ms=2_030_000,
    nonce="a" * 43,
)
AUTHORITY = JobExecutionAuthority(
    job_id=REFERENCE.job_id,
    owner_id=OWNER.process_instance_id,
    fencing_token=11,
)


def test_worker_launch_v1_is_canonical_bounded_and_round_trips() -> None:
    launch = EngineWorkerLaunchV1(
        launch_version=1,
        reference=REFERENCE,
        resource_handoff=HANDOFF,
        job_authority=AUTHORITY,
    )

    payload = launch.to_bytes()

    assert payload.endswith(b"\n")
    assert len(payload) <= 4 * 1024
    assert EngineWorkerLaunchV1.from_bytes(payload) == launch
    assert json.loads(payload) == {
        "job_authority": {
            "fencing_token": 11,
            "job_id": REFERENCE.job_id,
            "owner_id": "engine-worker-1",
        },
        "launch_version": 1,
        "reference": {
            "job_id": REFERENCE.job_id,
            "workspace_instance_id": "1" * 32,
        },
        "resource_handoff": {
            "expires_at_ms": 2_030_000,
            "fencing_token": 7,
            "handoff_version": 1,
            "nonce": "a" * 43,
            "owner": {
                "process_id": None,
                "process_instance_id": "engine-worker-1",
                "workspace_identity": "workspace-id",
            },
            "resource_name": "produce:heavy:v1",
        },
    }


def test_worker_launch_v1_rejects_semantically_equal_noncanonical_bytes() -> None:
    launch = EngineWorkerLaunchV1(1, REFERENCE, HANDOFF, AUTHORITY)
    value = json.loads(launch.to_bytes())
    noncanonical = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")

    with pytest.raises(EngineProtocolError):
        EngineWorkerLaunchV1.from_bytes(noncanonical)


@pytest.mark.parametrize(
    "mutation",
    ("duplicate", "extra", "wrong_job", "wrong_owner", "oversize"),
)
def test_worker_launch_v1_rejects_ambiguous_or_unbound_payloads(
    mutation: str,
) -> None:
    payload = json.loads(
        EngineWorkerLaunchV1(
            1,
            REFERENCE,
            HANDOFF,
            AUTHORITY,
        ).to_bytes()
    )
    if mutation == "duplicate":
        encoded = (
            b'{"launch_version":1,"launch_version":1,'
            + json.dumps(
                {
                    "reference": payload["reference"],
                    "resource_handoff": payload["resource_handoff"],
                    "job_authority": payload["job_authority"],
                },
                separators=(",", ":"),
            )[1:].encode("utf-8")
        )
    elif mutation == "extra":
        payload["unexpected"] = True
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    elif mutation == "wrong_job":
        payload["job_authority"]["job_id"] = (
            "job_018f0000-0000-7000-8000-000000000002"
        )
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    elif mutation == "wrong_owner":
        payload["job_authority"]["owner_id"] = "another-worker"
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    else:
        encoded = b"{" + b" " * (4 * 1024) + b"}"

    with pytest.raises(EngineProtocolError):
        EngineWorkerLaunchV1.from_bytes(encoded)

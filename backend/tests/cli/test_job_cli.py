from __future__ import annotations

import importlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from app.cli.errors import map_error
from app.cli.main import main
from app.core.domain.video import JobState, VideoProduceRequest
from app.core.errors import ErrorCategory
from app.core.jobs.external_operation import ExternalOperationGuard
from app.core.jobs.model import AttemptState, JobExecutionOwner
from app.core.packs.events import (
    JOB_PACK_ENVIRONMENT_EVENT,
    parse_job_pack_environment_payload,
)


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "workspace-v2"


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "工作 空间"
    shutil.copytree(FIXTURE_ROOT, root)
    shutil.rmtree(root / "raw" / "personal" / ".staging")
    for relative in (
        "raw/common",
        "raw/personal/.staging",
        "wiki/common",
        "wiki/personal",
        ".cache",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def runtime(tmp_path: Path):
    runtime_module = importlib.import_module("app.runtime")
    return runtime_module.create_fake_runtime(tmp_path / "machine")


def _request(
    workspace_root: Path,
    client_request_id: str,
) -> VideoProduceRequest:
    return VideoProduceRequest(
        request_schema_version=1,
        workspace_root=workspace_root,
        input_value="fixture://course",
        client_request_id=client_request_id,
    )


def _job_json(
    runtime: object,
    workspace_root: Path,
    *arguments: str,
) -> int:
    return main(
        ["job", *arguments, "--workspace", str(workspace_root), "--json"],
        runtime=runtime,
    )


def test_job_get_and_status_alias_return_result_refs_without_private_paths(
    runtime: object,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    submitted = runtime.submit_video(_request(workspace_root, "get-result"))
    completed = runtime.wait_job(submitted.job_id)
    assert completed.state is JobState.SUCCEEDED

    for command in ("get", "status"):
        code = _job_json(runtime, workspace_root, command, submitted.job_id)
        captured = capsys.readouterr()
        envelope = json.loads(captured.out)

        assert code == 0
        assert envelope["ok"] is True
        assert envelope["command"] == f"job {command}"
        assert envelope["data"]["job"]["state"] == "succeeded"
        assert envelope["data"]["job"]["retry"]["allowed"] is True
        refs = envelope["data"]["job"]["result_refs"]
        assert refs["bundle_id"] == completed.result.bundle_id
        assert refs["primary_draft_artifact_id"] == (
            completed.result.primary_draft_artifact_id
        )
        assert str(workspace_root) not in captured.out
        assert "jobs.sqlite" not in captured.out
        assert captured.err == ""


def test_job_get_locates_workspace_job_store_without_loading_execution_runtime(
    tmp_path: Path,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    from app.runtime_config import RuntimeConfigService
    from app.runtime_paths import resolve_runtime_paths

    local_data = tmp_path / "machine-data"
    producer = runtime_module.create_fake_runtime_for_workspace(
        workspace_root,
        local_app_data=local_data,
    )
    completed = producer.wait_job(
        producer.submit_video(_request(workspace_root, "default-query")).job_id
    )
    paths = resolve_runtime_paths(local_data_parent=local_data)

    code = main(
        [
            "job",
            "get",
            completed.job_id,
            "--workspace",
            str(workspace_root),
            "--json",
        ],
        config_service=RuntimeConfigService(paths=paths, environ={}),
    )
    envelope = json.loads(capsys.readouterr().out)

    assert code == 0
    assert envelope["data"]["job"]["state"] == "succeeded"


def test_job_get_failed_is_a_successful_query_with_actionable_failure(
    tmp_path: Path,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    service_module = importlib.import_module("app.core.application.video_service")
    failing = runtime_module.create_fake_runtime(
        tmp_path / "failing-machine",
        capabilities=service_module.VideoPreflightCapabilities(
            video_feature_pack=False
        ),
    )
    submitted = failing.submit_video(_request(workspace_root, "failed-status"))
    assert failing.wait_job(submitted.job_id).state is JobState.FAILED

    code = _job_json(failing, workspace_root, "get", submitted.job_id)
    envelope = json.loads(capsys.readouterr().out)

    assert code == 0
    job = envelope["data"]["job"]
    assert envelope["ok"] is True
    assert job["state"] == "failed"
    assert job["failure"]["code"] == "video_feature_pack_unavailable"
    assert job["failure"]["retryable"] is False
    assert job["failure"]["next_actions"]
    assert job["retry"]["allowed"] is True


def test_attempt_storage_capacity_failure_has_machine_state_recovery_action() -> None:
    mapped = map_error(
        code="attempt_storage_capacity_insufficient",
        category=ErrorCategory.RETRYABLE_RUNTIME,
        message="Attempt storage has insufficient free disk space",
        details={"required_bytes": 11, "available_bytes": 10},
    )

    assert mapped.error.retryable is True
    assert mapped.error.next_actions == (
        "Free space in the AllToNote machine-state location and retry",
    )


def test_job_list_is_bounded_cursor_paginated_and_state_filtered(
    runtime: object,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    submitted = [
        runtime.submit_video(_request(workspace_root, f"list-{index}"))
        for index in range(3)
    ]
    runtime.job_repository.cancel_job(submitted[0].job_id)

    code = _job_json(runtime, workspace_root, "list", "--limit", "2")
    first = json.loads(capsys.readouterr().out)
    assert code == 0
    assert len(first["data"]["jobs"]) == 2
    assert first["data"]["next_cursor"]

    code = _job_json(
        runtime,
        workspace_root,
        "list",
        "--limit",
        "2",
        "--cursor",
        first["data"]["next_cursor"],
    )
    second = json.loads(capsys.readouterr().out)
    assert code == 0
    assert len(second["data"]["jobs"]) == 1
    assert second["data"]["next_cursor"] is None
    first_ids = {item["job_id"] for item in first["data"]["jobs"]}
    second_ids = {item["job_id"] for item in second["data"]["jobs"]}
    assert first_ids.isdisjoint(second_ids)

    code = _job_json(
        runtime,
        workspace_root,
        "list",
        "--state",
        "cancelled",
    )
    filtered = json.loads(capsys.readouterr().out)
    assert code == 0
    assert [item["state"] for item in filtered["data"]["jobs"]] == [
        "cancelled"
    ]


def test_job_events_jsonl_is_versioned_bounded_and_redacted(
    runtime: object,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    submitted = runtime.submit_video(_request(workspace_root, "events"))
    runtime.job_repository.append_event(
        submitted.job_id,
        "step.finished",
        json.dumps(
            {"step": "acquire", "source_path": str(workspace_root / "private")},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ),
    )
    runtime.job_repository.append_event(
        submitted.job_id,
        "job.observed",
        '{"state":"queued"}',
    )

    code = main(
        [
            "job",
            "events",
            submitted.job_id,
            "--workspace",
            str(workspace_root),
            "--after-sequence",
            "0",
            "--limit",
            "1",
            "--jsonl",
        ],
        runtime=runtime,
    )
    captured = capsys.readouterr()
    lines = captured.out.splitlines()

    assert code == 0
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event_schema_version"] == 1
    assert event["job_id"] == submitted.job_id
    assert event["sequence"] == 1
    assert event["type"] == "step.finished"
    assert str(workspace_root) not in captured.out
    assert captured.err == ""

    code = main(
        [
            "job",
            "events",
            submitted.job_id,
            "--workspace",
            str(workspace_root),
            "--after-sequence",
            "1",
            "--json",
        ],
        runtime=runtime,
    )
    page = json.loads(capsys.readouterr().out)
    assert code == 0
    assert [item["sequence"] for item in page["data"]["events"]] == [2]


def test_job_wait_executes_without_timeout_and_failed_wait_is_nonzero(
    tmp_path: Path,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    successful = runtime_module.create_fake_runtime(tmp_path / "wait-success")
    success = successful.submit_video(_request(workspace_root, "wait-success"))

    code = _job_json(successful, workspace_root, "wait", success.job_id)
    succeeded = json.loads(capsys.readouterr().out)
    assert code == 0
    assert succeeded["data"]["job"]["state"] == "succeeded"

    service_module = importlib.import_module("app.core.application.video_service")
    failing = runtime_module.create_fake_runtime(
        tmp_path / "wait-failure",
        capabilities=service_module.VideoPreflightCapabilities(
            video_feature_pack=False
        ),
    )
    failed = failing.submit_video(_request(workspace_root, "wait-failure"))

    code = _job_json(failing, workspace_root, "wait", failed.job_id)
    failure = json.loads(capsys.readouterr().out)
    assert code == 10
    assert failure["ok"] is False
    assert failure["data"]["job_id"] == failed.job_id
    assert failure["job"]["state"] == "failed"


def test_job_wait_timeout_does_not_execute_or_cancel_job(
    runtime: object,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    submitted = runtime.submit_video(_request(workspace_root, "wait-timeout"))

    code = _job_json(
        runtime,
        workspace_root,
        "wait",
        submitted.job_id,
        "--timeout",
        "0.01",
    )
    envelope = json.loads(capsys.readouterr().out)
    persisted = runtime.get_job(submitted.job_id)

    assert code == 30
    assert envelope["error"]["code"] == "job_wait_timeout"
    assert envelope["error"]["retryable"] is True
    assert persisted.state is JobState.QUEUED
    assert persisted.cancellation_requested is False


def test_job_wait_observes_engine_owned_job_without_foreground_execution(
    runtime: object,
    workspace_root: Path,
) -> None:
    from app.job_runtime import JobRuntime

    submitted = runtime.submit_video(
        _request(workspace_root, "engine-owned-wait"),
        execution_owner=JobExecutionOwner.ENGINE,
    )
    foreground_calls: list[str] = []
    sleep_calls = 0

    def foreground_wait(job_id: str):
        foreground_calls.append(job_id)
        raise AssertionError("ENGINE-owned Job was executed by the waiting CLI")

    def finish_from_engine(_seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        runtime.cancel_job(submitted.job_id)

    job_runtime = JobRuntime(
        runtime.job_repository,
        wait_job=foreground_wait,
        current_config_snapshot=None,
        sleep=finish_from_engine,
    )

    observed = job_runtime.wait_for_job(submitted.job_id)

    assert observed.snapshot.state is JobState.CANCELLED
    assert sleep_calls == 1
    assert foreground_calls == []


def test_engine_authorized_job_runtime_executes_engine_owned_job(
    runtime: object,
    workspace_root: Path,
) -> None:
    from app.job_runtime import JobRuntime

    submitted = runtime.submit_video(
        _request(workspace_root, "engine-authorized-wait"),
        execution_owner=JobExecutionOwner.ENGINE,
    )
    execution_calls: list[str] = []

    def engine_wait(job_id: str):
        execution_calls.append(job_id)
        return runtime.cancel_job(job_id)

    job_runtime = JobRuntime(
        runtime.job_repository,
        wait_job=engine_wait,
        current_config_snapshot=None,
        execution_owner=JobExecutionOwner.ENGINE,
    )

    observed = job_runtime.wait_for_job(submitted.job_id)

    assert observed.snapshot.state is JobState.CANCELLED
    assert execution_calls == [submitted.job_id]


@pytest.mark.parametrize("timeout", ("0", "-1", "nan", "inf"))
def test_job_wait_rejects_invalid_timeout_without_mutation(
    timeout: str,
    runtime: object,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    submitted = runtime.submit_video(
        _request(workspace_root, f"invalid-timeout-{timeout}")
    )

    code = _job_json(
        runtime,
        workspace_root,
        "wait",
        submitted.job_id,
        "--timeout",
        timeout,
    )
    envelope = json.loads(capsys.readouterr().out)

    assert code == 2
    assert envelope["error"]["code"] == "job_wait_timeout_invalid"
    assert runtime.get_job(submitted.job_id).state is JobState.QUEUED


def test_job_wait_keyboard_interrupt_does_not_cancel_job(
    runtime: object,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    submitted = runtime.submit_video(_request(workspace_root, "wait-interrupt"))

    def interrupt(_job_id: str, event_sink: object | None = None):
        del event_sink
        raise KeyboardInterrupt

    runtime.wait_job = interrupt
    code = _job_json(runtime, workspace_root, "wait", submitted.job_id)
    envelope = json.loads(capsys.readouterr().out)
    persisted = runtime.get_job(submitted.job_id)

    assert code == 130
    assert envelope["error"]["code"] == "interrupted"
    assert persisted.cancellation_requested is False


def test_job_cancel_then_retry_creates_new_job_with_frozen_snapshots(
    runtime: object,
    workspace_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = runtime.submit_video(_request(workspace_root, "cancel-retry"))
    runtime.job_repository.append_event(
        original.job_id,
        JOB_PACK_ENVIRONMENT_EVENT,
        json.dumps(
            {
                "schema_version": 1,
                "packs": [
                    {
                        "pack_id": "media-basic",
                        "pack_version": "fixture-v1",
                        "platform": "windows-x86_64",
                        "manifest_sha256": "sha256:" + "a" * 64,
                    }
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ),
    )

    code = _job_json(runtime, workspace_root, "cancel", original.job_id)
    cancelled = json.loads(capsys.readouterr().out)
    assert code == 0
    assert cancelled["data"]["job"]["state"] == "cancelled"

    request_path = tmp_path / "retry.json"
    request_path.write_text(
        json.dumps(
            {
                "retry_request_schema_version": 1,
                "client_request_id": "cancel-retry-2",
                "expected_original_job_state": "cancelled",
                "confirmed_unknown_operation_ids": [],
            }
        ),
        encoding="utf-8",
    )
    code = _job_json(
        runtime,
        workspace_root,
        "retry",
        original.job_id,
        "--request",
        str(request_path),
    )
    retried = json.loads(capsys.readouterr().out)

    assert code == 0
    retry_job = retried["data"]["job"]
    assert retry_job["state"] == "queued"
    assert retry_job["job_id"] != original.job_id
    assert retry_job["retry_of_job_id"] == original.job_id
    assert runtime.get_job(original.job_id).state is JobState.CANCELLED
    events = runtime.job_repository.list_events(retry_job["job_id"])
    assert [event.event_type for event in events] == [
        "configuration.snapshot.v1",
        JOB_PACK_ENVIRONMENT_EVENT,
    ]
    inherited = parse_job_pack_environment_payload(events[1].payload_json)
    assert inherited.pack("media-basic").manifest_sha256 == "sha256:" + "a" * 64


def test_job_retry_requires_exact_unknown_operation_confirmation(
    runtime: object,
    workspace_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = runtime.submit_video(_request(workspace_root, "unknown-retry"))
    repo = runtime.job_repository
    repo.transition_job(original.job_id, JobState.RUNNING)
    authority = repo.claim_job(
        original.job_id,
        "unknown-retry-owner",
        ttl_seconds=300,
    ).authority
    attempt = repo.start_attempt(
        repo.create_attempt(
            original.job_id,
            "generate_draft",
            authority=authority,
        ).attempt_id,
        authority,
    )
    guard = ExternalOperationGuard(repo, authority)
    operation = guard.prepare(
        job_id=original.job_id,
        step_id=attempt.step_id,
        attempt_id=attempt.attempt_id,
        provider="fixture/provider-v1",
        request_hash="sha256:" + "4" * 64,
        summary_json="{}",
    )
    guard.start(operation.operation_id)
    guard.unknown(operation.operation_id, summary_json="{}")
    repo.cancel_job(original.job_id)
    repo.transition_attempt(
        attempt.attempt_id,
        AttemptState.CANCELLED,
        authority=authority,
    )
    repo.release_job_claim(authority)

    code = _job_json(runtime, workspace_root, "get", original.job_id)
    status = json.loads(capsys.readouterr().out)
    assert code == 0
    retry = status["data"]["job"]["retry"]
    assert retry["requires_unknown_operation_confirmation"] is True
    assert retry["unknown_operation_ids"] == [operation.operation_id]

    request_path = tmp_path / "unknown-retry.json"
    base_request = {
        "retry_request_schema_version": 1,
        "client_request_id": "unknown-retry-2",
        "expected_original_job_state": "cancelled",
        "confirmed_unknown_operation_ids": [],
    }
    request_path.write_text(json.dumps(base_request), encoding="utf-8")
    code = _job_json(
        runtime,
        workspace_root,
        "retry",
        original.job_id,
        "--request",
        str(request_path),
    )
    rejected = json.loads(capsys.readouterr().out)
    assert code == 20
    assert rejected["error"]["code"] == "retry_unknown_operations_unconfirmed"

    request_path.write_text(
        json.dumps(
            {
                **base_request,
                "confirmed_unknown_operation_ids": [operation.operation_id],
            }
        ),
        encoding="utf-8",
    )
    code = _job_json(
        runtime,
        workspace_root,
        "retry",
        original.job_id,
        "--request",
        str(request_path),
    )
    retried = json.loads(capsys.readouterr().out)
    assert code == 0
    assert retried["data"]["job"]["retry_of_job_id"] == original.job_id


def test_job_respond_requires_matching_explicit_waiting_challenge(
    runtime: object,
    workspace_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = runtime.job_repository
    job = repo.create_job(
        request_hash="sha256:" + "1" * 64,
        principal="local-user",
        client_request_id="respond",
    )
    repo.transition_job(job.job_id, JobState.RUNNING)
    authority = repo.claim_job(
        job.job_id,
        "respond-owner",
        ttl_seconds=300,
    ).authority
    attempt = repo.start_attempt(
        repo.create_attempt(
            job.job_id,
            "acquire",
            authority=authority,
        ).attempt_id,
        authority,
    )
    repo.transition_attempt(
        attempt.attempt_id,
        AttemptState.NEEDS_INPUT,
        authority=authority,
    )
    challenge = repo.create_challenge(
        job.job_id,
        attempt.attempt_id,
        '{"kind":"credential_profile"}',
    )
    repo.release_job_claim(authority)
    response_path = tmp_path / "response.json"
    response_path.write_text(
        '{"credential_profile":"profile-a"}', encoding="utf-8"
    )

    code = _job_json(
        runtime,
        workspace_root,
        "respond",
        job.job_id,
        "--challenge",
        challenge.challenge_id,
        "--response",
        str(response_path),
    )
    resumed = json.loads(capsys.readouterr().out)

    assert code == 0
    assert resumed["data"]["job"]["state"] == "queued"
    assert resumed["data"]["job"]["challenge_id"] is None
    assert resumed["data"]["job"]["active_attempt_id"] is not None


def test_job_respond_rejects_external_unknown_without_consuming_challenge(
    runtime: object,
    workspace_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = runtime.job_repository
    job = repo.create_job(
        request_hash="sha256:" + "2" * 64,
        principal="local-user",
        client_request_id="unknown-respond",
    )
    repo.transition_job(job.job_id, JobState.RUNNING)
    authority = repo.claim_job(
        job.job_id,
        "unknown-owner",
        ttl_seconds=300,
    ).authority
    attempt = repo.start_attempt(
        repo.create_attempt(
            job.job_id,
            "generate_draft",
            authority=authority,
        ).attempt_id,
        authority,
    )
    repo.transition_attempt(
        attempt.attempt_id,
        AttemptState.NEEDS_INPUT,
        authority=authority,
    )
    challenge = repo.create_challenge(
        job.job_id,
        attempt.attempt_id,
        '{"code":"external_outcome_unknown","operation_ids":["op_fixture"]}',
    )
    repo.release_job_claim(authority)
    response_path = tmp_path / "must-not-be-read.json"

    code = _job_json(
        runtime,
        workspace_root,
        "respond",
        job.job_id,
        "--challenge",
        challenge.challenge_id,
        "--response",
        str(response_path),
    )
    envelope = json.loads(capsys.readouterr().out)

    assert code == 20
    assert envelope["error"]["code"] == "external_outcome_unknown"
    assert runtime.get_job(job.job_id).state is JobState.WAITING_FOR_INPUT
    assert runtime.get_job(job.job_id).challenge_id == challenge.challenge_id


def test_job_cancel_repairs_legacy_cancelled_pending_challenge(
    runtime: object,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = runtime.job_repository
    job = repo.create_job(
        request_hash="sha256:" + "3" * 64,
        principal="local-user",
        client_request_id="legacy-cancelled-challenge",
    )
    repo.transition_job(job.job_id, JobState.RUNNING)
    authority = repo.claim_job(
        job.job_id,
        "legacy-cancel-owner",
        ttl_seconds=300,
    ).authority
    attempt = repo.start_attempt(
        repo.create_attempt(
            job.job_id,
            "generate_draft",
            authority=authority,
        ).attempt_id,
        authority,
    )
    repo.transition_attempt(
        attempt.attempt_id,
        AttemptState.NEEDS_INPUT,
        authority=authority,
    )
    challenge = repo.create_challenge(
        job.job_id,
        attempt.attempt_id,
        '{"code":"external_outcome_unknown"}',
    )
    repo.release_job_claim(authority)
    repo.cancel_job(job.job_id)
    with sqlite3.connect(repo.database_path) as connection:
        connection.execute(
            "UPDATE challenges SET state = 'pending' WHERE challenge_id = ?",
            (challenge.challenge_id,),
        )

    code = _job_json(runtime, workspace_root, "cancel", job.job_id)
    envelope = json.loads(capsys.readouterr().out)

    assert code == 0
    assert envelope["data"]["job"]["state"] == "cancelled"
    assert envelope["data"]["job"]["challenge_id"] is None


def test_job_permission_mismatch_is_indistinguishable_from_not_found(
    runtime: object,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hidden = runtime.job_repository.create_job(
        request_hash="sha256:" + "3" * 64,
        principal="another-principal",
        client_request_id="hidden",
    )

    code = _job_json(runtime, workspace_root, "get", hidden.job_id)
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)

    assert code == 2
    assert envelope["error"]["code"] == "job_not_found"
    assert "another-principal" not in captured.out
    assert "jobs.sqlite" not in captured.out


def test_job_list_rejects_cursor_reuse_with_different_filter(
    runtime: object,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for index in range(2):
        runtime.submit_video(_request(workspace_root, f"cursor-{index}"))

    _job_json(runtime, workspace_root, "list", "--limit", "1")
    first = json.loads(capsys.readouterr().out)
    code = _job_json(
        runtime,
        workspace_root,
        "list",
        "--state",
        "queued",
        "--cursor",
        first["data"]["next_cursor"],
    )
    envelope = json.loads(capsys.readouterr().out)

    assert code == 2
    assert envelope["error"]["code"] == "job_cursor_invalid"

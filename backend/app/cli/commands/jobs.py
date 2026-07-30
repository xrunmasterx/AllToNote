from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from app.cli.contracts import ApplicationResult
from app.cli.errors import ExitCode, map_domain_error, map_error_detail
from app.core.application.job_query_service import JobEventPage, JobView
from app.core.domain.production import RecipeProduceResult
from app.core.domain.video import JobState, RetryJobRequest
from app.core.errors import DomainError, ErrorCategory
from app.job_runtime import JobRuntime


_MAX_CONTROL_DOCUMENT_BYTES = 65_536
_RETRY_REQUEST_KEYS = frozenset(
    {
        "retry_request_schema_version",
        "client_request_id",
        "expected_original_job_state",
        "confirmed_unknown_operation_ids",
    }
)


@dataclass(frozen=True)
class JobCommandExecution:
    result: ApplicationResult
    exit_code: ExitCode
    jsonl_records: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "jsonl_records", tuple(self.jsonl_records))


def add_job_parsers(subparsers: argparse._SubParsersAction) -> None:
    job_parser = subparsers.add_parser("job")
    commands = job_parser.add_subparsers(dest="job_command", required=True)

    for command in ("get", "status"):
        parser = commands.add_parser(command)
        parser.add_argument("job_id")
        _add_common_options(parser)

    list_parser = commands.add_parser("list")
    list_parser.add_argument(
        "--state",
        action="append",
        choices=tuple(state.value for state in JobState),
    )
    list_parser.add_argument("--cursor")
    list_parser.add_argument("--limit", type=int, default=50)
    _add_common_options(list_parser)

    wait_parser = commands.add_parser("wait")
    wait_parser.add_argument("job_id")
    wait_parser.add_argument("--timeout", type=float)
    _add_common_options(wait_parser)

    events_parser = commands.add_parser("events")
    events_parser.add_argument("job_id")
    events_parser.add_argument("--after-sequence", type=int, default=0)
    events_parser.add_argument("--limit", type=int, default=100)
    events_format = events_parser.add_mutually_exclusive_group()
    events_format.add_argument("--json", action="store_true")
    events_format.add_argument("--jsonl", action="store_true")
    events_parser.add_argument("--workspace", type=Path)
    events_parser.add_argument("--config-profile")

    cancel_parser = commands.add_parser("cancel")
    cancel_parser.add_argument("job_id")
    _add_common_options(cancel_parser)

    respond_parser = commands.add_parser("respond")
    respond_parser.add_argument("job_id")
    respond_parser.add_argument("--challenge", required=True)
    respond_parser.add_argument("--response", required=True, type=Path)
    _add_common_options(respond_parser)

    retry_parser = commands.add_parser("retry")
    retry_parser.add_argument("job_id")
    retry_parser.add_argument("--request", required=True, type=Path)
    _add_common_options(retry_parser)


def execute_job_command(
    args: argparse.Namespace,
    correlation_id: str,
    *,
    runtime: JobRuntime,
    versions: Mapping[str, object],
) -> JobCommandExecution:
    command = f"job {args.job_command}"
    if args.job_command in {"get", "status"}:
        return _success(
            command,
            correlation_id,
            runtime.get_job(args.job_id),
            versions=versions,
        )
    if args.job_command == "list":
        states = tuple(JobState(value) for value in (args.state or ()))
        page = runtime.list_jobs(
            states=states,
            cursor=args.cursor,
            limit=args.limit,
        )
        jobs = [_job_projection(view) for view in page.jobs]
        return JobCommandExecution(
            result=ApplicationResult(
                command=command,
                correlation_id=correlation_id,
                ok=True,
                data={"jobs": jobs, "next_cursor": page.next_cursor},
                versions=versions,
                human_lines=tuple(
                    f"{job['job_id']}  {job['state']}" for job in jobs
                ),
            ),
            exit_code=ExitCode.SUCCESS,
        )
    if args.job_command == "events":
        page = runtime.get_job_events(
            args.job_id,
            after_sequence=args.after_sequence,
            limit=args.limit,
        )
        records = _event_records(page)
        return JobCommandExecution(
            result=ApplicationResult(
                command=command,
                correlation_id=correlation_id,
                ok=True,
                data={
                    "events": list(records),
                    "next_after_sequence": page.next_after_sequence,
                },
                versions=versions,
                human_lines=tuple(
                    f"{record['sequence']}  {record['type']}" for record in records
                ),
            ),
            exit_code=ExitCode.SUCCESS,
            jsonl_records=records,
        )
    if args.job_command == "wait":
        view = runtime.wait_for_job(
            args.job_id,
            timeout_seconds=args.timeout,
        )
        return _wait_result(
            command,
            correlation_id,
            view,
            versions=versions,
        )
    if args.job_command == "cancel":
        return _success(
            command,
            correlation_id,
            runtime.cancel_job(args.job_id),
            versions=versions,
        )
    if args.job_command == "respond":
        runtime.require_respondable(args.job_id, args.challenge)
        response = _read_json_object(
            args.response,
            invalid_code="challenge_response_invalid",
            invalid_message="Challenge response must be a bounded JSON object",
        )
        return _success(
            command,
            correlation_id,
            runtime.respond_job(args.job_id, args.challenge, response),
            versions=versions,
        )
    if args.job_command == "retry":
        request = _retry_request(args.request)
        return _success(
            command,
            correlation_id,
            runtime.retry_job(args.job_id, request),
            versions=versions,
        )
    raise DomainError(
        "cli_usage_invalid",
        ErrorCategory.INVALID_REQUEST,
        "Command arguments are invalid",
    )


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--config-profile")
    parser.add_argument("--json", action="store_true")


def _success(
    command: str,
    correlation_id: str,
    view: JobView,
    *,
    versions: Mapping[str, object],
) -> JobCommandExecution:
    job = _job_projection(view)
    return JobCommandExecution(
        result=ApplicationResult(
            command=command,
            correlation_id=correlation_id,
            ok=True,
            data={"job": job},
            job=job,
            warnings=(
                view.snapshot.result.warnings
                if view.snapshot.result is not None
                else ()
            ),
            versions=versions,
            human_lines=(
                f"Job: {view.snapshot.job_id}",
                f"State: {view.snapshot.state.value}",
            ),
        ),
        exit_code=ExitCode.SUCCESS,
    )


def _wait_result(
    command: str,
    correlation_id: str,
    view: JobView,
    *,
    versions: Mapping[str, object],
) -> JobCommandExecution:
    snapshot = view.snapshot
    job = _job_projection(view)
    if snapshot.state is JobState.FAILED:
        if snapshot.error is None:
            raise DomainError(
                "job_projection_invalid",
                ErrorCategory.INTERNAL,
                "Stored Job state and result projection are inconsistent",
            )
        mapped = map_error_detail(snapshot.error)
        return JobCommandExecution(
            result=ApplicationResult(
                command=command,
                correlation_id=correlation_id,
                ok=False,
                data={"job_id": snapshot.job_id, "state": snapshot.state.value},
                error=mapped.error,
                job=job,
                versions=versions,
            ),
            exit_code=mapped.exit_code,
        )
    if snapshot.state is JobState.CANCELLED:
        mapped = map_domain_error(
            DomainError(
                "job_cancelled",
                ErrorCategory.CANCELLED,
                "Job was cancelled",
            )
        )
        return JobCommandExecution(
            result=ApplicationResult(
                command=command,
                correlation_id=correlation_id,
                ok=False,
                data={"job_id": snapshot.job_id, "state": snapshot.state.value},
                error=mapped.error,
                job=job,
                versions=versions,
            ),
            exit_code=mapped.exit_code,
        )
    return _success(
        command,
        correlation_id,
        view,
        versions=versions,
    )


def _job_projection(view: JobView) -> dict[str, object]:
    snapshot = view.snapshot
    projection: dict[str, object] = {
        "job_id": snapshot.job_id,
        "state": snapshot.state.value,
        "cancellation_requested": snapshot.cancellation_requested,
        "active_attempt_id": snapshot.active_attempt_id,
        "challenge_id": snapshot.challenge_id,
        "retry_of_job_id": snapshot.retry_of_job_id,
        "created_at": view.created_at,
        "updated_at": view.updated_at,
        "retry": {
            "allowed": snapshot.state
            in {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED},
            "requires_unknown_operation_confirmation": bool(
                view.unknown_operation_ids
            ),
            "unknown_operation_ids": list(view.unknown_operation_ids),
        },
        "result_refs": _result_refs(snapshot.result),
    }
    if snapshot.error is not None:
        mapped = map_error_detail(snapshot.error).error
        projection["failure"] = {
            "code": mapped.code,
            "category": mapped.category,
            "message": mapped.message,
            "retryable": mapped.retryable,
            "next_actions": list(mapped.next_actions),
        }
    if view.pending_challenge is not None:
        projection["challenge"] = {
            "challenge_id": view.pending_challenge.challenge_id,
            "kind": view.pending_challenge.kind,
            "code": view.pending_challenge.code,
        }
    return projection


def _result_refs(result: object) -> dict[str, object] | None:
    if result is None:
        return None
    if isinstance(result, RecipeProduceResult):
        return {
            "result_kind": result.result_kind,
            "run_id": result.run_id,
            "bundle_id": result.bundle_id,
            "manifest_sha256": result.manifest_sha256,
            "commit_sha256": result.commit_sha256,
            "source_id": result.source_id,
            "source_revision_id": result.source_revision_id,
            "artifacts": dict(result.artifacts),
            "primary_draft_artifact_id": result.artifacts.get("primary_draft"),
            "quality_overall": result.quality_overall,
            "publish_eligible": result.publish_eligible,
        }
    return {
        "run_id": result.run_id,
        "bundle_id": result.bundle_id,
        "manifest_sha256": result.manifest_sha256,
        "commit_sha256": result.commit_sha256,
        "source_id": result.source_id,
        "source_revision_id": result.source_revision_id,
        "primary_draft_artifact_id": result.primary_draft_artifact_id,
        "transcript_artifact_id": result.transcript_artifact_id,
        "evidence_set_artifact_id": result.evidence_set_artifact_id,
        "quality_report_artifact_id": result.quality_report_artifact_id,
        "display_asset_ids": list(result.display_asset_ids),
        "quality_overall": result.quality_overall.value,
        "publish_eligible": result.publish_eligible,
    }


def _event_records(page: JobEventPage) -> tuple[dict[str, object], ...]:
    records: list[dict[str, object]] = []
    for event in page.events:
        try:
            payload = json.loads(event.payload_json)
        except (json.JSONDecodeError, TypeError, ValueError):
            raise DomainError(
                "job_event_invalid",
                ErrorCategory.INTERNAL,
                "Stored Job event is invalid",
            ) from None
        records.append(
            {
                "event_schema_version": 1,
                "event_id": event.event_id,
                "job_id": event.job_id,
                "sequence": event.sequence,
                "recorded_at": event.created_at,
                "type": event.event_type,
                "data": payload,
            }
        )
    return tuple(records)


def _retry_request(path: Path) -> RetryJobRequest:
    payload = _read_json_object(
        path,
        invalid_code="retry_request_invalid",
        invalid_message="Retry request must be a bounded versioned JSON object",
    )
    if frozenset(payload) != _RETRY_REQUEST_KEYS:
        raise DomainError(
            "retry_request_invalid",
            ErrorCategory.INVALID_REQUEST,
            "Retry request must use the exact versioned schema",
        )
    try:
        expected_state = JobState(payload["expected_original_job_state"])
        confirmed = payload["confirmed_unknown_operation_ids"]
        if type(confirmed) is not list:
            raise TypeError
        return RetryJobRequest(
            retry_request_schema_version=payload["retry_request_schema_version"],
            client_request_id=payload["client_request_id"],
            expected_original_job_state=expected_state,
            confirmed_unknown_operation_ids=tuple(confirmed),
        )
    except (TypeError, ValueError):
        raise DomainError(
            "retry_request_invalid",
            ErrorCategory.INVALID_REQUEST,
            "Retry request must use the exact versioned schema",
        ) from None


def _read_json_object(
    path: Path,
    *,
    invalid_code: str,
    invalid_message: str,
) -> dict[str, object]:
    try:
        if not path.is_file() or path.stat().st_size > _MAX_CONTROL_DOCUMENT_BYTES:
            raise ValueError
        raw = path.read_bytes()
        if len(raw) > _MAX_CONTROL_DOCUMENT_BYTES:
            raise ValueError
        value = json.loads(
            raw.decode("utf-8"),
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        if type(value) is not dict:
            raise ValueError
        return value
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise DomainError(
            invalid_code,
            ErrorCategory.INVALID_REQUEST,
            invalid_message,
        ) from None


__all__ = ["JobCommandExecution", "add_job_parsers", "execute_job_command"]

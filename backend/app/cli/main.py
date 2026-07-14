from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from app.core.domain.ids import new_typed_id
from app.core.domain.video import JobSnapshot, JobState, VideoProduceRequest
from app.core.errors import DomainError, ErrorCategory


RUNTIME_VERSION = "0.1.0"


class _VideoRuntime(Protocol):
    def submit_video(self, request: VideoProduceRequest) -> JobSnapshot: ...

    def wait_job(self, job_id: str, event_sink: object | None = None) -> JobSnapshot: ...


_EXIT_CODES = {
    ErrorCategory.INVALID_REQUEST: 2,
    ErrorCategory.WORKSPACE_INCOMPATIBLE: 10,
    ErrorCategory.CONFLICT: 20,
    ErrorCategory.RETRYABLE_RUNTIME: 30,
    ErrorCategory.POLICY_DENIED: 40,
    ErrorCategory.RECIPE_FAILED: 50,
    ErrorCategory.CANCELLED: 60,
    ErrorCategory.INTERNAL: 70,
}


def main(
    argv: Sequence[str] | None = None,
    *,
    runtime: _VideoRuntime | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="alltonote")
    subparsers = parser.add_subparsers(dest="command", required=True)
    version_parser = subparsers.add_parser("version")
    version_parser.add_argument("--json", action="store_true")
    produce_parser = subparsers.add_parser("produce")
    produce_subparsers = produce_parser.add_subparsers(
        dest="produce_kind", required=True
    )
    video_parser = produce_subparsers.add_parser("video")
    video_parser.add_argument("input_value")
    video_parser.add_argument("--workspace", required=True, type=Path)
    video_parser.add_argument("--wait", action="store_true")
    video_parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "version" and args.json:
        print(
            json.dumps(
                {
                    "alltonote_cli_protocol_version": 1,
                    "ok": True,
                    "data": {"runtime_version": RUNTIME_VERSION},
                },
                separators=(",", ":"),
            )
        )
        return 0
    if args.command == "version":
        print(RUNTIME_VERSION)
        return 0

    correlation_id = new_typed_id("corr")
    try:
        active_runtime = runtime or _default_runtime()
        request = VideoProduceRequest(
            request_schema_version=1,
            workspace_root=args.workspace.resolve(),
            input_value=args.input_value,
            client_request_id=correlation_id,
        )
        snapshot = active_runtime.submit_video(request)
        if args.wait:
            snapshot = active_runtime.wait_job(snapshot.job_id)
    except DomainError as error:
        _print_json(_error_envelope(correlation_id, error))
        return _EXIT_CODES[error.category]

    if snapshot.state is JobState.FAILED and snapshot.error is not None:
        _print_json(
            {
                "alltonote_cli_protocol_version": 1,
                "ok": False,
                "command": "produce video",
                "correlation_id": correlation_id,
                "data": {"job_id": snapshot.job_id, "state": snapshot.state.value},
                "error": {
                    "code": snapshot.error.code,
                    "category": snapshot.error.category.value,
                    "message": snapshot.error.message,
                    "details": dict(snapshot.error.details),
                },
                "warnings": [],
            }
        )
        return _EXIT_CODES[snapshot.error.category]

    result = snapshot.result
    data: dict[str, object] = {
        "job_id": snapshot.job_id,
        "state": snapshot.state.value,
    }
    if result is not None:
        data.update(
            {
                "run_id": result.run_id,
                "bundle_id": result.bundle_id,
                "manifest_sha256": result.manifest_sha256,
                "commit_sha256": result.commit_sha256,
                "workspace_relative_bundle_path": result.workspace_relative_bundle_path,
                "primary_draft_artifact_id": result.primary_draft_artifact_id,
                "quality": {
                    "overall": result.quality_overall.value,
                    "publish_eligible": result.publish_eligible,
                },
            }
        )
    _print_json(
        {
            "alltonote_cli_protocol_version": 1,
            "ok": True,
            "command": "produce video",
            "correlation_id": correlation_id,
            "data": data,
            "warnings": list(result.warnings) if result is not None else [],
        }
    )
    return 0


def _error_envelope(correlation_id: str, error: DomainError) -> dict[str, object]:
    return {
        "alltonote_cli_protocol_version": 1,
        "ok": False,
        "command": "produce video",
        "correlation_id": correlation_id,
        "error": {
            "code": error.code,
            "category": error.category.value,
            "message": error.message,
            "details": dict(error.details),
        },
        "warnings": [],
    }


def _print_json(envelope: dict[str, object]) -> None:
    print(
        json.dumps(
            envelope,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    )


def _default_runtime() -> _VideoRuntime:
    from app.runtime import create_fake_runtime

    return create_fake_runtime(Path.home() / ".alltonote" / "runtime")


def entrypoint() -> None:
    raise SystemExit(main())

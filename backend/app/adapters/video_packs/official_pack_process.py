from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from app.adapters.worker_process import (
    WorkerProcessTimeout,
    WorkerProcessUnavailable,
    run_worker_process,
)
from app.core.errors import DomainError, ErrorCategory


_ENVIRONMENT_KEYS = frozenset(
    {
        "COMSPEC",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "PATH",
        "PATHEXT",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "WINDIR",
    }
)
_MAXIMUM_REQUEST_BYTES = 64 * 1024


def minimal_worker_environment(
    source: Mapping[str, str] | None = None,
    *,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    values = os.environ if source is None else source
    environment = {
        key: value
        for key in _ENVIRONMENT_KEYS
        if type(value := values.get(key)) is str and value
    }
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    for key, value in (overrides or {}).items():
        if type(key) is not str or not key or type(value) is not str:
            raise ValueError("Worker environment override is invalid")
        environment[key] = value
    return environment


def _result_invalid() -> DomainError:
    return DomainError(
        "pack_worker_result_invalid",
        ErrorCategory.RECIPE_FAILED,
        "The isolated Pack worker returned an invalid result",
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def run_json_worker(
    command: Sequence[str],
    request: Mapping[str, object],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
    maximum_output_bytes: int,
    check_cancelled: Callable[[], None] | None = None,
) -> dict[str, object]:
    if (
        not command
        or any(type(value) is not str or not value for value in command)
        or not isinstance(request, Mapping)
        or not isinstance(environment, Mapping)
        or not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= 0
        or type(maximum_output_bytes) is not int
        or maximum_output_bytes < 1
    ):
        raise ValueError("Pack worker invocation is invalid")
    try:
        work_directory = Path(cwd).resolve(strict=True)
    except OSError as error:
        raise ValueError("Pack worker directory is invalid") from error
    if not work_directory.is_dir():
        raise ValueError("Pack worker directory is invalid")
    try:
        payload = json.dumps(
            dict(request),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("Pack worker request is invalid") from error
    if len(payload) > _MAXIMUM_REQUEST_BYTES:
        raise ValueError("Pack worker request is too large")
    if check_cancelled is not None:
        check_cancelled()

    with tempfile.TemporaryFile() as output_file:
        def check_running() -> None:
            if check_cancelled is not None:
                check_cancelled()
            if os.fstat(output_file.fileno()).st_size > maximum_output_bytes:
                raise _result_invalid()

        try:
            return_code = run_worker_process(
                command,
                cwd=work_directory,
                environment=environment,
                timeout_seconds=timeout_seconds,
                stdin_payload=payload,
                stdout=output_file,
                stderr=subprocess.DEVNULL,
                check_running=check_running,
            )
        except WorkerProcessUnavailable as error:
            raise DomainError(
                "pack_worker_unavailable",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "The isolated Pack worker could not be started",
            ) from error
        except WorkerProcessTimeout as error:
            raise DomainError(
                "pack_worker_timeout",
                ErrorCategory.RETRYABLE_RUNTIME,
                "The isolated Pack worker exceeded its time budget",
            ) from error

        if return_code != 0:
            raise DomainError(
                "pack_worker_failed",
                ErrorCategory.RECIPE_FAILED,
                "The isolated Pack worker failed",
            )
        if os.fstat(output_file.fileno()).st_size > maximum_output_bytes:
            raise _result_invalid()
        output_file.seek(0)
        output = output_file.read(maximum_output_bytes + 1)

    if len(output) > maximum_output_bytes:
        raise _result_invalid()
    try:
        result = json.loads(
            output.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non_finite_json")
            ),
        )
    except (UnicodeError, ValueError) as error:
        raise _result_invalid() from error
    if type(result) is not dict:
        raise _result_invalid()
    return result


__all__ = ["minimal_worker_environment", "run_json_worker"]

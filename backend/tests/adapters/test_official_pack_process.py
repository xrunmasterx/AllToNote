from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from app.adapters.video_packs.official_pack_process import (
    minimal_worker_environment,
    run_json_worker,
)
from app.core.errors import DomainError, ErrorCategory


def test_process_round_trips_bounded_json(tmp_path: Path) -> None:
    result = run_json_worker(
        (
            sys.executable,
            "-c",
            "import json,sys; value=json.load(sys.stdin); json.dump({'seen':value},sys.stdout)",
        ),
        {"marker": "one"},
        cwd=tmp_path,
        environment=minimal_worker_environment({}),
        timeout_seconds=5,
        maximum_output_bytes=1024,
    )
    assert result == {"seen": {"marker": "one"}}


def test_process_rejects_oversized_or_malformed_output(tmp_path: Path) -> None:
    with pytest.raises(DomainError) as oversized:
        run_json_worker(
            (sys.executable, "-c", "print('x' * 2048)"),
            {},
            cwd=tmp_path,
            environment=minimal_worker_environment({}),
            timeout_seconds=5,
            maximum_output_bytes=128,
        )
    assert oversized.value.code == "pack_worker_result_invalid"

    with pytest.raises(DomainError) as malformed:
        run_json_worker(
            (sys.executable, "-c", "print('not-json')"),
            {},
            cwd=tmp_path,
            environment=minimal_worker_environment({}),
            timeout_seconds=5,
            maximum_output_bytes=128,
        )
    assert malformed.value.code == "pack_worker_result_invalid"


def test_process_terminates_worker_as_soon_as_output_exceeds_limit(
    tmp_path: Path,
) -> None:
    started = time.monotonic()

    with pytest.raises(DomainError) as caught:
        run_json_worker(
            (
                sys.executable,
                "-c",
                (
                    "import os,time;"
                    "os.write(1,b'x'*(2*1024*1024));"
                    "time.sleep(60)"
                ),
            ),
            {},
            cwd=tmp_path,
            environment=minimal_worker_environment({}),
            timeout_seconds=5,
            maximum_output_bytes=128,
        )

    assert caught.value.code == "pack_worker_result_invalid"
    assert time.monotonic() - started < 3


def test_process_timeout_terminates_only_spawned_worker(tmp_path: Path) -> None:
    with pytest.raises(DomainError) as caught:
        run_json_worker(
            (sys.executable, "-c", "import time; time.sleep(60)"),
            {},
            cwd=tmp_path,
            environment=minimal_worker_environment({}),
            timeout_seconds=0.1,
            maximum_output_bytes=128,
        )
    assert caught.value.code == "pack_worker_timeout"


def test_process_timeout_applies_when_worker_never_reads_request(
    tmp_path: Path,
) -> None:
    started = time.monotonic()

    with pytest.raises(DomainError) as caught:
        run_json_worker(
            (sys.executable, "-c", "import time; time.sleep(60)"),
            {"payload": "x" * 60_000},
            cwd=tmp_path,
            environment=minimal_worker_environment({}),
            timeout_seconds=0.1,
            maximum_output_bytes=128,
        )

    assert caught.value.code == "pack_worker_timeout"
    assert time.monotonic() - started < 3


def _process_is_running(process_id: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(process_id, 0)
        except OSError:
            return False
        return True

    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        process_id,
    )
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        return bool(
            kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            and exit_code.value == still_active
        )
    finally:
        kernel32.CloseHandle(handle)


def test_process_cancellation_terminates_spawned_descendant(tmp_path: Path) -> None:
    marker = tmp_path / "descendant.pid"
    worker = (
        "import json,pathlib,subprocess,sys,time;"
        "json.load(sys.stdin);"
        f"child=subprocess.Popen([{sys.executable!r},'-c','import time;time.sleep(60)']);"
        f"pathlib.Path({str(marker)!r}).write_text(str(child.pid));"
        "time.sleep(60)"
    )

    def check_cancelled() -> None:
        if marker.is_file():
            raise DomainError(
                "job_cancelled",
                ErrorCategory.CANCELLED,
                "Job cancellation was requested",
            )

    with pytest.raises(DomainError) as caught:
        run_json_worker(
            (sys.executable, "-c", worker),
            {},
            cwd=tmp_path,
            environment=minimal_worker_environment({}),
            timeout_seconds=5,
            maximum_output_bytes=128,
            check_cancelled=check_cancelled,
        )

    assert caught.value.code == "job_cancelled"
    descendant_pid = int(marker.read_text(encoding="utf-8"))
    assert not _process_is_running(descendant_pid)


def test_minimal_environment_has_no_unapproved_secret(tmp_path: Path) -> None:
    source = {
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", "C:\\Windows"),
        "PATH": str(tmp_path),
        "ALLTONOTE_API_KEY": "secret",
        "OPENAI_API_KEY": "secret",
        "COOKIE": "secret",
    }
    environment = minimal_worker_environment(
        source,
        overrides={"PYTHONPATH": str(tmp_path / "runtime")},
    )

    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert environment["PYTHONPATH"] == str(tmp_path / "runtime")
    assert not {"ALLTONOTE_API_KEY", "OPENAI_API_KEY", "COOKIE"} & environment.keys()

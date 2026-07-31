from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import app.adapters.worker_process as worker_process
from app.core.errors import DomainError, ErrorCategory


def _process_is_running(process_id: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, process_id)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        return bool(
            kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            and exit_code.value == 259
        )
    finally:
        kernel32.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object contract")
def test_windows_job_kills_worker_when_owner_process_dies(tmp_path: Path) -> None:
    backend_root = Path(__file__).parents[2]
    marker = tmp_path / "worker.pid"
    worker = (
        "import os,pathlib,time;"
        f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid()));"
        "time.sleep(60)"
    )
    controller = (
        "import os,sys;"
        "from pathlib import Path;"
        "from app.adapters.worker_process import run_worker_process;"
        f"run_worker_process(({sys.executable!r},'-c',{worker!r}),"
        f"cwd=Path({str(backend_root)!r}),environment=os.environ,"
        "timeout_seconds=60)"
    )
    process = subprocess.Popen(
        (sys.executable, "-B", "-c", controller),
        cwd=backend_root,
        env=dict(os.environ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    deadline = time.monotonic() + 5
    while not marker.is_file() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.is_file()
    worker_pid = int(marker.read_text(encoding="utf-8"))
    assert _process_is_running(worker_pid)

    process.kill()
    process.wait(timeout=5)
    deadline = time.monotonic() + 2
    while _process_is_running(worker_pid) and time.monotonic() < deadline:
        time.sleep(0.05)

    assert not _process_is_running(worker_pid)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object contract")
def test_cleanup_failure_does_not_replace_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingProcess:
        pid = 123
        _handle = 456

        def poll(self):
            return None

        def wait(self, timeout):
            raise OSError("wait failed")

        def kill(self):
            raise OSError("kill failed")

    class FailingJob:
        def assign_and_resume(self, _process):
            pass

        def close(self):
            raise OSError("close failed")

    monkeypatch.setattr(worker_process, "_WindowsJob", FailingJob)
    monkeypatch.setattr(
        worker_process.subprocess,
        "Popen",
        lambda *_args, **_kwargs: FailingProcess(),
    )
    cancellation = DomainError(
        "job_cancelled",
        ErrorCategory.CANCELLED,
        "Job cancellation was requested",
    )
    checks = 0

    def check_running() -> None:
        nonlocal checks
        checks += 1
        if checks > 1:
            raise cancellation

    with pytest.raises(DomainError) as caught:
        worker_process.run_worker_process(
            ("worker",),
            cwd=tmp_path,
            environment={},
            timeout_seconds=1,
            check_running=check_running,
        )

    assert caught.value is cancellation


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object contract")
def test_activation_cleanup_failure_keeps_unavailable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingProcess:
        pid = 123
        _handle = 456

        def wait(self, timeout):
            raise OSError("wait failed")

        def kill(self):
            raise OSError("kill failed")

    class FailingJob:
        def assign_and_resume(self, _process):
            raise OSError("assign failed")

        def close(self):
            raise OSError("close failed")

    monkeypatch.setattr(worker_process, "_WindowsJob", FailingJob)
    monkeypatch.setattr(
        worker_process.subprocess,
        "Popen",
        lambda *_args, **_kwargs: FailingProcess(),
    )

    with pytest.raises(worker_process.WorkerProcessUnavailable):
        worker_process.run_worker_process(
            ("worker",),
            cwd=tmp_path,
            environment={},
            timeout_seconds=1,
        )

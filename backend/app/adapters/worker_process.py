from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import BinaryIO


_POLL_SECONDS = 0.05
_WAIT_SECONDS = 5.0
_CREATE_SUSPENDED = 0x00000004
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


class WorkerProcessTimeout(TimeoutError):
    pass


class WorkerProcessUnavailable(OSError):
    pass


class _WindowsJob:
    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("per_process_user_time_limit", ctypes.c_int64),
                ("per_job_user_time_limit", ctypes.c_int64),
                ("limit_flags", wintypes.DWORD),
                ("minimum_working_set_size", ctypes.c_size_t),
                ("maximum_working_set_size", ctypes.c_size_t),
                ("active_process_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority_class", wintypes.DWORD),
                ("scheduling_class", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("read_operation_count", ctypes.c_ulonglong),
                ("write_operation_count", ctypes.c_ulonglong),
                ("other_operation_count", ctypes.c_ulonglong),
                ("read_transfer_count", ctypes.c_ulonglong),
                ("write_transfer_count", ctypes.c_ulonglong),
                ("other_transfer_count", ctypes.c_ulonglong),
            ]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("basic_limit_information", _BasicLimitInformation),
                ("io_info", _IoCounters),
                ("process_memory_limit", ctypes.c_size_t),
                ("job_memory_limit", ctypes.c_size_t),
                ("peak_process_memory_used", ctypes.c_size_t),
                ("peak_job_memory_used", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll")
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        ntdll.NtResumeProcess.restype = ctypes.c_long

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        information = _ExtendedLimitInformation()
        information.basic_limit_information.limit_flags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not kernel32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise OSError(error, "SetInformationJobObject failed")
        self._ctypes = ctypes
        self._wintypes = wintypes
        self._kernel32 = kernel32
        self._ntdll = ntdll
        self._handle = handle

    def assign_and_resume(self, process: subprocess.Popen[bytes]) -> None:
        process_handle = self._wintypes.HANDLE(process._handle)
        if not self._kernel32.AssignProcessToJobObject(
            self._handle,
            process_handle,
        ):
            raise OSError(
                self._ctypes.get_last_error(),
                "AssignProcessToJobObject failed",
            )
        status = self._ntdll.NtResumeProcess(process_handle)
        if status != 0:
            raise OSError(status, "NtResumeProcess failed")

    def close(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is not None:
            self._kernel32.CloseHandle(handle)


def _wait_after_termination(process: subprocess.Popen[bytes]) -> None:
    try:
        process.wait(timeout=_WAIT_SECONDS)
        return
    except BaseException:
        pass
    try:
        process.kill()
    except BaseException:
        pass
    try:
        process.wait(timeout=_WAIT_SECONDS)
    except BaseException:
        pass


def _terminate_process_tree(
    process: subprocess.Popen[bytes],
    windows_job: _WindowsJob | None,
) -> None:
    if windows_job is not None:
        try:
            windows_job.close()
        except BaseException:
            pass
        _wait_after_termination(process)
        return
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except BaseException:
            try:
                process.kill()
            except BaseException:
                pass
        _wait_after_termination(process)
        return
    try:
        process.kill()
    except BaseException:
        pass
    _wait_after_termination(process)


def run_worker_process(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
    stdin_payload: bytes | None = None,
    stdout: int | BinaryIO = subprocess.DEVNULL,
    stderr: int | BinaryIO = subprocess.DEVNULL,
    check_running: Callable[[], None] | None = None,
) -> int:
    if (
        not command
        or any(type(value) is not str or not value for value in command)
        or not isinstance(environment, Mapping)
        or not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= 0
        or (stdin_payload is not None and type(stdin_payload) is not bytes)
    ):
        raise ValueError("Worker process invocation is invalid")
    try:
        work_directory = Path(cwd).resolve(strict=True)
    except OSError as error:
        raise ValueError("Worker process directory is invalid") from error
    if not work_directory.is_dir():
        raise ValueError("Worker process directory is invalid")
    if check_running is not None:
        check_running()

    windows_job: _WindowsJob | None = None
    popen_kwargs: dict[str, object] = {}
    if os.name == "nt":
        try:
            windows_job = _WindowsJob()
        except OSError as error:
            raise WorkerProcessUnavailable("Windows worker Job is unavailable") from error
        popen_kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | _CREATE_SUSPENDED
        )
    else:
        popen_kwargs["start_new_session"] = True

    input_file: BinaryIO | None = None
    if stdin_payload is not None:
        try:
            input_file = tempfile.TemporaryFile()
            input_file.write(stdin_payload)
            input_file.flush()
            input_file.seek(0)
        except OSError as error:
            if input_file is not None:
                input_file.close()
            if windows_job is not None:
                windows_job.close()
            raise WorkerProcessUnavailable(
                "Worker process input could not be staged"
            ) from error

    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            tuple(command),
            cwd=work_directory,
            env=dict(environment),
            stdin=input_file if input_file is not None else subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            **popen_kwargs,
        )
    except OSError as error:
        raise WorkerProcessUnavailable("Worker process could not be started") from error
    finally:
        if input_file is not None:
            input_file.close()
        if process is None and windows_job is not None:
            windows_job.close()

    assert process is not None
    try:
        if windows_job is not None:
            try:
                windows_job.assign_and_resume(process)
            except OSError as error:
                _terminate_process_tree(process, windows_job)
                raise WorkerProcessUnavailable(
                    "Windows worker Job could not be activated"
                ) from error
        deadline = time.monotonic() + float(timeout_seconds)
        while True:
            if check_running is not None:
                check_running()
            return_code = process.poll()
            if return_code is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkerProcessTimeout
            time.sleep(min(_POLL_SECONDS, remaining))
    except BaseException:
        _terminate_process_tree(process, windows_job)
        raise

    if windows_job is not None:
        windows_job.close()
    elif os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    return return_code


__all__ = [
    "WorkerProcessTimeout",
    "WorkerProcessUnavailable",
    "run_worker_process",
]

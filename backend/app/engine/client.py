from __future__ import annotations

import base64
import ctypes
import os
import subprocess
import sys
import time
from multiprocessing import AuthenticationError
from pathlib import Path
from uuid import uuid4

from filelock import Timeout

from app.core.errors import DomainError, ErrorCategory
from app.engine.contracts import (
    ENGINE_PROTOCOL_VERSION,
    MAX_FRAME_BYTES,
    EngineDescriptor,
    EngineJobReference,
    EngineProtocolError,
    decode_response,
    encode_request,
)
from app.engine.errors import ENGINE_REMOTE_ERROR_CATEGORIES
from app.engine.instance import (
    EngineFileLock,
    EngineInstancePaths,
    EngineState,
    EngineStatus,
    engine_lifecycle_supported,
    ensure_instance_root,
    process_start_identity,
    read_descriptor,
    validate_instance_root_if_present,
)
from app.engine.transport import connect_engine, receive_bytes
from app.runtime_paths import RuntimePaths


class LocalEngineClient:
    def __init__(
        self,
        paths: RuntimePaths,
        *,
        startup_timeout_seconds: float = 5.0,
        shutdown_timeout_seconds: float = 5.0,
        ipc_timeout_seconds: float = 1.0,
    ) -> None:
        if not engine_lifecycle_supported():
            raise DomainError(
                "engine_platform_unsupported",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "Engine lifecycle is unsupported on this platform",
            )
        self._paths = EngineInstancePaths.from_runtime_paths(paths)
        self._runtime_paths = paths
        self._startup_timeout = startup_timeout_seconds
        self._shutdown_timeout = shutdown_timeout_seconds
        self._ipc_timeout = ipc_timeout_seconds

    @property
    def descriptor_path(self) -> Path:
        return self._paths.descriptor

    def status(self) -> EngineStatus:
        validate_instance_root_if_present(self._paths)
        try:
            descriptor = read_descriptor(self._paths.descriptor)
        except EngineProtocolError as error:
            state = (
                EngineState.INCOMPATIBLE
                if error.code == "engine_protocol_incompatible"
                else EngineState.STALE
            )
            return EngineStatus(state, False)
        except DomainError:
            raise
        if descriptor is None:
            return EngineStatus(EngineState.STOPPED, False)
        if not self._identity_matches(descriptor):
            return EngineStatus(EngineState.STALE, False)
        try:
            response = self._request(descriptor, "health")
        except DomainError as error:
            state = (
                EngineState.INCOMPATIBLE
                if error.code == "engine_protocol_incompatible"
                else EngineState.STALE
            )
            return EngineStatus(state, False)
        if response.get("engine_id") != descriptor.engine_id:
            return EngineStatus(EngineState.STALE, False)
        return EngineStatus(
            EngineState.RUNNING,
            True,
            engine_id=descriptor.engine_id,
            started_at=descriptor.started_at,
        )

    def ensure(self) -> EngineStatus:
        ensure_instance_root(self._paths)
        launch_lock = EngineFileLock(
            self._paths.launch_lock,
            timeout=self._startup_timeout,
        )
        try:
            launch_lock.acquire()
        except Timeout as error:
            raise DomainError(
                "engine_start_busy",
                ErrorCategory.RETRYABLE_RUNTIME,
                "Engine startup is busy",
            ) from error
        try:
            return self._ensure_locked()
        finally:
            launch_lock.release()

    def notify_job(self, reference: EngineJobReference) -> dict[str, object]:
        if not isinstance(reference, EngineJobReference):
            raise DomainError(
                "engine_job_reference_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Engine Job reference is invalid",
            )
        ensure_instance_root(self._paths)
        launch_lock = EngineFileLock(
            self._paths.launch_lock,
            timeout=self._startup_timeout,
        )
        try:
            launch_lock.acquire()
        except Timeout as error:
            raise DomainError(
                "engine_start_busy",
                ErrorCategory.RETRYABLE_RUNTIME,
                "Engine startup is busy",
            ) from error
        try:
            status = self._ensure_locked()
            descriptor = read_descriptor(self._paths.descriptor)
            if descriptor is None or not self._identity_matches(descriptor):
                raise DomainError(
                    "engine_state_inconsistent",
                    ErrorCategory.CONFLICT,
                    "Engine lifecycle state is inconsistent",
                )
            data = self._request(
                descriptor,
                "job.notify",
                params={
                    "workspace_instance_id": reference.workspace_instance_id,
                    "job_id": reference.job_id,
                },
            )
            if (
                frozenset(data)
                != {
                    "engine_id",
                    "workspace_instance_id",
                    "job_id",
                    "state",
                    "scheduled",
                }
                or data["engine_id"] != status.engine_id
                or data["workspace_instance_id"] != reference.workspace_instance_id
                or data["job_id"] != reference.job_id
                or data["state"]
                not in {
                    "queued",
                    "running",
                    "waiting_for_input",
                    "succeeded",
                    "failed",
                    "cancelled",
                }
                or type(data["scheduled"]) is not bool
            ):
                raise DomainError(
                    "engine_response_invalid",
                    ErrorCategory.RETRYABLE_RUNTIME,
                    "Engine response is invalid",
                )
            return data
        finally:
            launch_lock.release()

    def _ensure_locked(self) -> EngineStatus:
        current = self.status()
        if current.running:
            descriptor = read_descriptor(self._paths.descriptor)
            if descriptor is None or not self._identity_matches(descriptor):
                raise DomainError(
                    "engine_state_inconsistent",
                    ErrorCategory.CONFLICT,
                    "Engine lifecycle state is inconsistent",
                )
            self._request(descriptor, "hello")
            return current
        if not self._lifetime_lock_is_free():
            raise DomainError(
                "engine_state_inconsistent",
                ErrorCategory.CONFLICT,
                "Engine lifecycle state is inconsistent",
            )
        self._remove_stale_descriptor()
        child = self._launch()
        deadline = time.monotonic() + self._startup_timeout
        while time.monotonic() < deadline:
            status = self.status()
            if status.running:
                return EngineStatus(
                    EngineState.RUNNING,
                    True,
                    engine_id=status.engine_id,
                    started_at=status.started_at,
                    started=True,
                )
            if child.poll() is not None:
                break
            time.sleep(0.02)
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
        raise DomainError(
            "engine_start_timeout",
            ErrorCategory.RETRYABLE_RUNTIME,
            "Engine did not become ready",
        )

    def stop(self) -> EngineStatus:
        validate_instance_root_if_present(self._paths)
        if not self._paths.root.exists():
            return EngineStatus(EngineState.STOPPED, False, stopped=True)
        launch_lock = EngineFileLock(
            self._paths.launch_lock,
            timeout=self._shutdown_timeout,
        )
        try:
            launch_lock.acquire()
        except Timeout as error:
            raise DomainError(
                "engine_stop_busy",
                ErrorCategory.RETRYABLE_RUNTIME,
                "Engine shutdown is busy",
            ) from error
        try:
            current = self.status()
            if not current.running:
                try:
                    descriptor = read_descriptor(self._paths.descriptor)
                except EngineProtocolError:
                    descriptor = None
                if descriptor is not None and self._identity_matches(descriptor):
                    raise DomainError(
                        "engine_stop_unavailable",
                        ErrorCategory.RETRYABLE_RUNTIME,
                        "Engine is alive but cannot be stopped safely",
                    )
                if not self._lifetime_lock_is_free():
                    raise DomainError(
                        "engine_state_inconsistent",
                        ErrorCategory.CONFLICT,
                        "Engine lifecycle state is inconsistent",
                    )
                self._remove_stale_descriptor()
                return EngineStatus(EngineState.STOPPED, False, stopped=True)
            descriptor = read_descriptor(self._paths.descriptor)
            if descriptor is None or not self._identity_matches(descriptor):
                raise DomainError(
                    "engine_state_inconsistent",
                    ErrorCategory.CONFLICT,
                    "Engine lifecycle state is inconsistent",
                )
            self._request(descriptor, "shutdown")
            deadline = time.monotonic() + self._shutdown_timeout
            while time.monotonic() < deadline:
                if (
                    not self._identity_matches(descriptor)
                    and self._lifetime_lock_is_free()
                ):
                    self._remove_stale_descriptor()
                    return EngineStatus(EngineState.STOPPED, False, stopped=True)
                time.sleep(0.02)
            raise DomainError(
                "engine_stop_timeout",
                ErrorCategory.RETRYABLE_RUNTIME,
                "Engine did not stop within the bounded timeout",
            )
        finally:
            launch_lock.release()

    def _identity_matches(self, descriptor: EngineDescriptor) -> bool:
        return (
            descriptor.scope_id == self._paths.scope_id
            and process_start_identity(descriptor.pid)
            == descriptor.process_start_identity
        )

    def _request(
        self,
        descriptor: EngineDescriptor,
        method: str,
        *,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        request_id = f"req_{uuid4().hex}"
        authkey = base64.urlsafe_b64decode(descriptor.nonce + "=")
        try:
            connection = connect_engine(
                descriptor.endpoint_name,
                authkey=authkey,
                expected_pid=descriptor.pid,
                timeout_seconds=self._ipc_timeout,
            )
            try:
                connection.send_bytes(
                    encode_request(
                        request_id=request_id,
                        method=method,
                        nonce=descriptor.nonce,
                        params=params or {},
                    )
                )
                response = decode_response(
                    receive_bytes(
                        connection,
                        MAX_FRAME_BYTES,
                        deadline=time.monotonic() + self._ipc_timeout,
                    ),
                    request_id=request_id,
                )
            finally:
                connection.close()
        except (
            AuthenticationError,
            EOFError,
            OSError,
            TimeoutError,
            EngineProtocolError,
        ) as error:
            code = (
                error.code
                if isinstance(error, EngineProtocolError)
                else "engine_unavailable"
            )
            category = (
                ErrorCategory.WORKSPACE_INCOMPATIBLE
                if code == "engine_protocol_incompatible"
                else ErrorCategory.RETRYABLE_RUNTIME
            )
            raise DomainError(code, category, "Engine is unavailable") from error
        if not response["ok"]:
            remote_error = response["error"]
            assert isinstance(remote_error, dict)
            remote_code = remote_error["code"]
            category = ENGINE_REMOTE_ERROR_CATEGORIES.get(remote_code)
            if category is not None:
                raise DomainError(
                    remote_code,
                    category,
                    "Engine request was rejected",
                )
            raise DomainError(
                "engine_request_failed",
                ErrorCategory.RETRYABLE_RUNTIME,
                "Engine request failed",
            )
        return response["data"]

    def _lifetime_lock_is_free(self) -> bool:
        probe = EngineFileLock(self._paths.lifetime_lock, timeout=0)
        try:
            probe.acquire()
        except Timeout:
            return False
        else:
            probe.release()
            return True

    def _remove_stale_descriptor(self) -> None:
        try:
            read_descriptor(self._paths.descriptor)
        except EngineProtocolError:
            pass
        except DomainError:
            raise
        try:
            self._paths.descriptor.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise DomainError(
                "engine_state_unavailable",
                ErrorCategory.RETRYABLE_RUNTIME,
                "Engine machine state is unavailable",
            ) from error

    def _launch(self) -> subprocess.Popen[bytes]:
        allowed_environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper()
            in {
                "APPDATA",
                "COMSPEC",
                "HOME",
                "LANG",
                "LC_ALL",
                "LOCALAPPDATA",
                "PATH",
                "PATHEXT",
                "PROGRAMDATA",
                "SYSTEMDRIVE",
                "SYSTEMROOT",
                "TEMP",
                "TMP",
                "TMPDIR",
                "USERPROFILE",
                "WINDIR",
            }
        }
        allowed_environment.update(
            {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
        )
        command = [
            str(Path(sys.executable).resolve()),
            "-I",
            "-B",
            "-m",
            "app.engine",
            "--engine-root",
            str(self._paths.root),
            "--log-root",
            str(self._paths.log_root),
            "--scope-id",
            self._paths.scope_id,
            "--config-dir",
            str(self._runtime_paths.config_dir),
            "--data-dir",
            str(self._runtime_paths.data_dir),
            "--cache-dir",
            str(self._runtime_paths.cache_dir),
            "--state-dir",
            str(self._runtime_paths.state_dir),
            "--runtime-log-dir",
            str(self._runtime_paths.log_dir),
        ]
        options: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "cwd": str(Path(sys.executable).resolve().parent),
            "env": allowed_environment,
            "close_fds": True,
        }
        if os.name == "nt":
            creation_flags = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
            if _windows_process_in_job():
                creation_flags |= 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
            options["creationflags"] = creation_flags
        else:
            options["start_new_session"] = True
        try:
            return subprocess.Popen(command, **options)
        except OSError as error:
            raise DomainError(
                "engine_detach_unavailable",
                ErrorCategory.POLICY_DENIED,
                "Engine cannot detach from the caller process",
            ) from error


def _windows_process_in_job() -> bool:
    if os.name != "nt":
        return False
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.IsProcessInJob.argtypes = (
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.BOOL),
    )
    kernel32.IsProcessInJob.restype = wintypes.BOOL
    in_job = wintypes.BOOL()
    if not kernel32.IsProcessInJob(
        kernel32.GetCurrentProcess(),
        None,
        ctypes.byref(in_job),
    ):
        raise DomainError(
            "engine_detach_unavailable",
            ErrorCategory.POLICY_DENIED,
            "Engine caller process containment cannot be inspected",
        )
    return bool(in_job.value)


__all__ = ["LocalEngineClient"]

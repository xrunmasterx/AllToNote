from __future__ import annotations

import ctypes
import hashlib
import os
import stat
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from uuid import uuid4

from filelock import FileLock

from app.core.errors import DomainError, ErrorCategory
from app.engine.contracts import (
    RUNTIME_COMPATIBILITY_MAJOR,
    EngineDescriptor,
    EngineProtocolError,
)
from app.runtime_paths import RuntimePaths


if os.name == "nt":
    import msvcrt
else:
    import fcntl


class EngineState(str, Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    STALE = "stale"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True)
class EngineStatus:
    state: EngineState
    running: bool
    engine_id: str | None = None
    started_at: str | None = None
    started: bool = False
    stopped: bool = False


@dataclass(frozen=True)
class EngineInstancePaths:
    root: Path
    log_root: Path
    scope_id: str
    descriptor: Path
    launch_lock: Path
    lifetime_lock: Path
    endpoint_kind: str
    endpoint_name: str

    @classmethod
    def from_runtime_paths(cls, paths: RuntimePaths) -> EngineInstancePaths:
        scope_id = engine_scope_id(paths.state_dir)
        return cls.from_roots(
            paths.engine_dir / scope_id,
            paths.engine_log_dir / scope_id,
            scope_id,
        )

    @classmethod
    def from_roots(
        cls,
        root: Path,
        log_root: Path,
        scope_id: str,
    ) -> EngineInstancePaths:
        resolved_root = Path(root).absolute()
        resolved_log = Path(log_root).absolute()
        endpoint_kind = (
            "windows-named-pipe" if os.name == "nt" else "unix-domain-socket"
        )
        endpoint_name = (
            rf"\\.\pipe\alltonote-engine-{scope_id[:32]}"
            if os.name == "nt"
            else str(resolved_root / "engine.sock")
        )
        return cls(
            root=resolved_root,
            log_root=resolved_log,
            scope_id=scope_id,
            descriptor=resolved_root / "endpoint.json",
            launch_lock=resolved_root / "launch.lock",
            lifetime_lock=resolved_root / "engine.lock",
            endpoint_kind=endpoint_kind,
            endpoint_name=endpoint_name,
        )


def _is_link_or_reparse(path: Path, metadata: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    )


def _lexically_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _ordinary_file(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        not _is_link_or_reparse(path, metadata)
        and stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
    )


def _open_matches(path: Path, file_descriptor: int) -> bool:
    try:
        metadata = path.lstat()
        opened = os.fstat(file_descriptor)
    except OSError:
        return False
    return (
        not _is_link_or_reparse(path, metadata)
        and stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and metadata.st_dev == opened.st_dev
        and metadata.st_ino == opened.st_ino
    )


class EngineFileLock(FileLock):
    """Keep a stable ordinary lock file for the complete Engine lifetime."""

    def _acquire(self) -> None:
        lock_path = Path(self.lock_file)
        if _lexically_exists(lock_path) and not _ordinary_file(lock_path):
            raise _state_unsafe()
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOINHERIT", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, self._context.mode)
        except OSError as error:
            if _lexically_exists(lock_path) and not _ordinary_file(lock_path):
                raise _state_unsafe() from error
            return
        if not _open_matches(lock_path, descriptor):
            os.close(descriptor)
            raise _state_unsafe()
        try:
            if os.name == "nt":
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(descriptor)
            return
        if not _open_matches(lock_path, descriptor):
            os.close(descriptor)
            raise _state_unsafe()
        self._context.lock_file_fd = descriptor

    def _release(self) -> None:
        descriptor = self._context.lock_file_fd
        if descriptor is None:
            return
        self._context.lock_file_fd = None
        if os.name == "nt":
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _state_unsafe() -> DomainError:
    return DomainError(
        "engine_state_root_unsafe",
        ErrorCategory.POLICY_DENIED,
        "Engine machine state is unsafe",
    )


def ensure_instance_root(paths: EngineInstancePaths) -> None:
    for candidate in (paths.root.parent, paths.root):
        if _lexically_exists(candidate):
            _validate_ordinary_directory(candidate)
        else:
            try:
                candidate.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise DomainError(
                    "engine_state_unavailable",
                    ErrorCategory.RETRYABLE_RUNTIME,
                    "Engine machine state is unavailable",
                ) from error
    try:
        paths.log_root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise DomainError(
            "engine_state_unavailable",
            ErrorCategory.RETRYABLE_RUNTIME,
            "Engine machine state is unavailable",
        ) from error


def validate_instance_root_if_present(paths: EngineInstancePaths) -> None:
    for candidate in (paths.root.parent, paths.root):
        if _lexically_exists(candidate):
            _validate_ordinary_directory(candidate)


def _validate_ordinary_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise _state_unsafe() from error
    if _is_link_or_reparse(path, metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise _state_unsafe()


def read_descriptor(path: Path) -> EngineDescriptor | None:
    if not _lexically_exists(path):
        return None
    if not _ordinary_file(path):
        raise _state_unsafe()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise _state_unsafe() from error
    try:
        if not _open_matches(path, descriptor):
            raise _state_unsafe()
        payload = os.read(descriptor, 16 * 1024 + 1)
        if not _open_matches(path, descriptor):
            raise _state_unsafe()
    finally:
        os.close(descriptor)
    return EngineDescriptor.from_bytes(payload)


def publish_descriptor(path: Path, descriptor: EngineDescriptor) -> None:
    temporary = path.with_name(f".endpoint-{uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(temporary, flags, 0o600)
        if not _open_matches(temporary, file_descriptor):
            raise _state_unsafe()
        payload = descriptor.to_bytes()
        written = 0
        while written < len(payload):
            written += os.write(file_descriptor, payload[written:])
        os.fsync(file_descriptor)
        if not _open_matches(temporary, file_descriptor):
            raise _state_unsafe()
        os.close(file_descriptor)
        file_descriptor = None
        os.replace(temporary, path)
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def remove_owned_descriptor(path: Path, *, engine_id: str, nonce: str) -> None:
    if not _lexically_exists(path) or not _ordinary_file(path):
        return
    native_handle: int | None = None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        if os.name == "nt":
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateFileW.argtypes = (
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            )
            kernel32.CreateFileW.restype = wintypes.HANDLE
            native_handle = kernel32.CreateFileW(
                str(path),
                0x80010000,  # GENERIC_READ | DELETE
                0x00000007,  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
                None,
                3,  # OPEN_EXISTING
                0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
                None,
            )
            if native_handle == wintypes.HANDLE(-1).value:
                return
            file_descriptor = msvcrt.open_osfhandle(native_handle, flags)
        else:
            file_descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        if not _open_matches(path, file_descriptor):
            return
        identity = os.fstat(file_descriptor)
        payload = os.read(file_descriptor, 16 * 1024 + 1)
        current = EngineDescriptor.from_bytes(payload)
        if current.engine_id != engine_id or current.nonce != nonce:
            return
        if not _open_matches(path, file_descriptor):
            return
        current_path = path.lstat()
        if (current_path.st_dev, current_path.st_ino) != (
            identity.st_dev,
            identity.st_ino,
        ):
            return
        if os.name == "nt":
            from ctypes import wintypes

            class FileDispositionInfo(ctypes.Structure):
                _fields_ = (("delete_file", wintypes.BOOL),)

            disposition = FileDispositionInfo(True)
            kernel32.SetFileInformationByHandle.argtypes = (
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
            )
            kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
            if not kernel32.SetFileInformationByHandle(
                native_handle,
                4,  # FileDispositionInfo
                ctypes.byref(disposition),
                ctypes.sizeof(disposition),
            ):
                raise OSError(ctypes.get_last_error())
        else:
            path.unlink()
    except (FileNotFoundError, OSError, EngineProtocolError):
        return
    finally:
        os.close(file_descriptor)


def engine_scope_id(state_dir: Path) -> str:
    identity = current_user_identity()
    canonical = os.path.normcase(str(Path(state_dir).resolve(strict=False)))
    material = f"{identity}\0{canonical}\0{RUNTIME_COMPATIBILITY_MAJOR}".encode(
        "utf-8"
    )
    return hashlib.sha256(material).hexdigest()


def engine_lifecycle_supported() -> bool:
    return os.name == "nt" or sys.platform.startswith("linux")


def current_user_identity() -> str:
    if os.name != "nt":
        return f"uid:{os.getuid()}"
    try:
        from ctypes import wintypes

        token_query = 0x0008
        token_user = 1
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        )
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = (
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.LPWSTR),
        )
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
        kernel32.LocalFree.restype = wintypes.HLOCAL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(),
            token_query,
            ctypes.byref(token),
        ):
            raise OSError(ctypes.get_last_error())
        try:
            needed = wintypes.DWORD()
            advapi32.GetTokenInformation(
                token, token_user, None, 0, ctypes.byref(needed)
            )
            buffer = ctypes.create_string_buffer(needed.value)
            if not advapi32.GetTokenInformation(
                token,
                token_user,
                buffer,
                needed,
                ctypes.byref(needed),
            ):
                raise OSError(ctypes.get_last_error())
            sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            sid_text = wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(
                sid_pointer, ctypes.byref(sid_text)
            ):
                raise OSError(ctypes.get_last_error())
            try:
                return f"sid:{sid_text.value}"
            finally:
                kernel32.LocalFree(sid_text)
        finally:
            kernel32.CloseHandle(token)
    except (AttributeError, OSError, ValueError) as error:
        raise DomainError(
            "engine_user_identity_unavailable",
            ErrorCategory.POLICY_DENIED,
            "Engine user identity is unavailable",
        ) from error


def process_start_identity(pid: int) -> str | None:
    if type(pid) is not int or pid <= 0:
        return None
    if os.name != "nt":
        try:
            stat_fields = Path(f"/proc/{pid}/stat").read_text(
                encoding="ascii"
            ).split()
            return f"proc-start:{stat_fields[21]}"
        except (FileNotFoundError, IndexError, OSError, UnicodeError):
            return None
    try:
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        )
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if not handle:
            return None
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return f"windows-filetime:{value}"
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError):
        return None


__all__ = [
    "EngineFileLock",
    "EngineInstancePaths",
    "EngineState",
    "EngineStatus",
    "current_user_identity",
    "engine_lifecycle_supported",
    "engine_scope_id",
    "ensure_instance_root",
    "process_start_identity",
    "publish_descriptor",
    "read_descriptor",
    "remove_owned_descriptor",
    "validate_instance_root_if_present",
]

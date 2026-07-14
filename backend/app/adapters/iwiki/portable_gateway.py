from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
import re
import stat
import unicodedata
from collections import OrderedDict
from importlib import metadata, resources
from pathlib import Path, PurePosixPath
from threading import Lock

from iwiki.errors import ErrorCode, IWikiError
from iwiki.portable import (
    CommitResult,
    PortableBundleRef,
    PortableContractInfo,
    PortableValidationReport,
    PreparedBundle,
    ValidationLevel,
    commit_prepared_bundle,
    inspect_portable_contract,
    prepare_bundle_commit,
    validate_bundle,
)
from iwiki.workspace import Workspace, open_workspace

from app.core.errors import DomainError, ErrorCategory
from app.core.ports.portable import (
    CandidateBundleLocation,
    CandidateBundleWriterPort,
    CandidateLocationCapabilityPort,
)


_RUNTIME_LOCK_KEYS = frozenset(
    {
        "iwiki_package",
        "portable_api_version",
        "portable_contract_id",
        "schema_set_id",
        "schema_sha256",
        "source_commit",
    }
)

_TRUSTED_IWIKI_DISTRIBUTION = "llm-iwiki"
_CONSUMED_TOMBSTONE_LIMIT = 128
_CLAIM_ACQUIRED = 1
_CLAIM_IN_PROGRESS = 2
_CLAIM_CONSUMED = 3
_CLAIM_UNKNOWN = 4
_COMPATIBILITY_ERRORS = (
    AttributeError,
    ImportError,
    KeyError,
    OSError,
    RuntimeError,
    TypeError,
    UnicodeError,
    ValueError,
)
_STAGING_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_NONCE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_WINDOWS_DEVICE_STEMS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
    | {f"com{number}" for number in "\u00b9\u00b2\u00b3"}
    | {f"lpt{number}" for number in "\u00b9\u00b2\u00b3"}
)
_JOB_ID = re.compile(
    r"job_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_READ_ATTRIBUTES = 0x80
_FILE_SHARE_READ = 0x1
_FILE_SHARE_WRITE = 0x2
_OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_WINDOWS_UNSUPPORTED_DIRECTORY_FLUSH_ERRORS = frozenset({5, 6, 87})


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


_ERROR_MAPPINGS = {
    ErrorCode.INVALID_ARGUMENT: (
        "portable_request_invalid",
        ErrorCategory.INVALID_REQUEST,
        "iwiki rejected the portable request",
    ),
    ErrorCode.INVALID_WORKSPACE: (
        "workspace_invalid",
        ErrorCategory.WORKSPACE_INCOMPATIBLE,
        "iwiki rejected the workspace",
    ),
    ErrorCode.SCHEMA_TOO_NEW: (
        "portable_schema_too_new",
        ErrorCategory.WORKSPACE_INCOMPATIBLE,
        "iwiki workspace schema is newer than supported",
    ),
    ErrorCode.VALIDATION_FAILED: (
        "portable_bundle_validation_failed",
        ErrorCategory.RECIPE_FAILED,
        "iwiki rejected the portable bundle",
    ),
    ErrorCode.CONFLICT: (
        "bundle_id_conflict",
        ErrorCategory.CONFLICT,
        "iwiki reported a bundle conflict",
    ),
    ErrorCode.PERMISSION_DENIED: (
        "workspace_write_denied",
        ErrorCategory.POLICY_DENIED,
        "iwiki denied workspace access",
    ),
    ErrorCode.RETRYABLE_RUNTIME: (
        "iwiki_runtime_retryable",
        ErrorCategory.RETRYABLE_RUNTIME,
        "iwiki operation failed temporarily",
    ),
    ErrorCode.INTERNAL: (
        "iwiki_internal_error",
        ErrorCategory.INTERNAL,
        "iwiki operation failed",
    ),
}


def _map_iwiki_error(error: IWikiError) -> DomainError:
    code, category, message = _ERROR_MAPPINGS[error.code]
    return DomainError(
        code,
        category,
        message,
        {
            "upstream": "iwiki",
            "upstream_code": error.code.wire_value,
        },
    )


def _contract_incompatible() -> DomainError:
    return DomainError(
        "portable_contract_incompatible",
        ErrorCategory.WORKSPACE_INCOMPATIBLE,
        "Installed iwiki portable contract is incompatible with this runtime",
    )


def _prepared_bundle_invalid() -> DomainError:
    return DomainError(
        "portable_prepared_bundle_invalid",
        ErrorCategory.INVALID_REQUEST,
        "Prepared bundle was not created by this gateway or was already consumed",
    )


def _canonical_distribution_name(name: str) -> str:
    if not name.isascii():
        return ""
    return re.sub(r"[-_.]+", "-", name).lower()


def _load_runtime_lock() -> dict[str, object]:
    try:
        payload = json.loads(
            resources.files("app")
            .joinpath("runtime-lock.json")
            .read_text(encoding="utf-8")
        )
        if type(payload) is not dict or frozenset(payload) != _RUNTIME_LOCK_KEYS:
            raise _contract_incompatible()

        package_spec = payload["iwiki_package"]
        api_version = payload["portable_api_version"]
        string_fields = (
            payload["portable_contract_id"],
            payload["schema_set_id"],
            payload["schema_sha256"],
            payload["source_commit"],
        )
        if (
            type(package_spec) is not str
            or package_spec.count("==") != 1
            or type(api_version) is not int
            or any(
                type(value) is not str or not value for value in string_fields
            )
        ):
            raise _contract_incompatible()

        package_name, expected_version = package_spec.split("==", 1)
        if (
            _canonical_distribution_name(package_name)
            != _TRUSTED_IWIKI_DISTRIBUTION
            or not expected_version
        ):
            raise _contract_incompatible()
        installed_version = metadata.version(_TRUSTED_IWIKI_DISTRIBUTION)
    except _COMPATIBILITY_ERRORS:
        raise _contract_incompatible() from None
    if installed_version != expected_version:
        raise _contract_incompatible()
    return payload


def _open_locked_workspace(
    workspace_root: Path,
) -> tuple[Workspace, PortableContractInfo]:
    runtime_lock = _load_runtime_lock()
    try:
        workspace = open_workspace(workspace_root, writable=True)
        info = inspect_portable_contract(workspace)
        if (
            info.iwiki_sdk_api_version != runtime_lock["portable_api_version"]
            or info.contract_id != runtime_lock["portable_contract_id"]
            or info.schema_set_id != runtime_lock["schema_set_id"]
            or info.schema_set_sha256 != runtime_lock["schema_sha256"]
        ):
            raise _contract_incompatible()
    except IWikiError as error:
        raise _map_iwiki_error(error) from None
    except _COMPATIBILITY_ERRORS:
        raise _contract_incompatible() from None
    return workspace, info


def _candidate_error(
    code: str,
    category: ErrorCategory,
    message: str,
) -> DomainError:
    return DomainError(code, category, message)


def _location_invalid() -> DomainError:
    return _candidate_error(
        "video_bundle_location_invalid",
        ErrorCategory.WORKSPACE_INCOMPATIBLE,
        "Candidate location is not a safe workspace staging path",
    )


def _write_failed() -> DomainError:
    return _candidate_error(
        "video_bundle_write_failed",
        ErrorCategory.RETRYABLE_RUNTIME,
        "Candidate bundle could not be written durably",
    )


def _writer_closed() -> DomainError:
    return _candidate_error(
        "video_bundle_writer_closed",
        ErrorCategory.CONFLICT,
        "Candidate bundle writer is closed",
    )


def _payload_parts(relative_path: str) -> tuple[str, ...]:
    if (
        type(relative_path) is not str
        or not relative_path
        or "\\" in relative_path
        or "\0" in relative_path
    ):
        raise _location_invalid()
    path = PurePosixPath(relative_path)
    if (
        path.is_absolute()
        or str(path) != relative_path
        or any(part in {"", ".", ".."} for part in path.parts)
        or relative_path.casefold() in {"bundle.json", "commit.json"}
    ):
        raise _location_invalid()
    for part in path.parts:
        _require_portable_component(part)
    return path.parts


def _require_portable_component(component: str) -> None:
    try:
        component.encode("utf-8", errors="strict")
    except UnicodeError:
        raise _location_invalid() from None
    stem = component.rstrip(" .").split(".", 1)[0].casefold()
    if (
        not component
        or unicodedata.normalize("NFC", component) != component
        or component in {".", ".."}
        or component.endswith((" ", "."))
        or any(character in component for character in ("/", "\\", ":", "\0"))
        or stem in _WINDOWS_DEVICE_STEMS
    ):
        raise _location_invalid()


def _normalized_windows_path(path: str | Path) -> str:
    value = str(path)
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return os.path.normcase(os.path.abspath(value))


class _CandidateLocationCapability:
    def __init__(
        self,
        workspace_root: Path,
        local_instance_id: str,
        nonce: str,
    ) -> None:
        self._workspace_root = workspace_root
        self._local_instance_id = local_instance_id
        self._nonce = nonce
        self._consumed = False
        self._lock = Lock()

    def begin(self, job_id: str) -> CandidateBundleWriterPort:
        if type(job_id) is not str or _JOB_ID.fullmatch(job_id) is None:
            raise _location_invalid()
        with self._lock:
            if self._consumed:
                raise _candidate_error(
                    "video_bundle_location_consumed",
                    ErrorCategory.CONFLICT,
                    "Candidate location capability was already consumed",
                )
            self._consumed = True
        workspace, _ = _open_locked_workspace(self._workspace_root)
        try:
            target_root = workspace.resolve_contract_path("raw_personal")
            candidate_path = (
                target_root
                / ".staging"
                / self._local_instance_id
                / f"{job_id}.{self._nonce}"
                / "bundle.partial"
            )
            staging_relative_path = workspace.relative(candidate_path)
            return _FileCandidateBundleWriter(
                workspace_root=workspace.root,
                target_root=target_root,
                candidate_path=candidate_path,
                staging_relative_path=staging_relative_path,
            )
        except DomainError:
            raise
        except MemoryError:
            raise
        except (IWikiError, OSError, RuntimeError, TypeError, ValueError):
            raise _location_invalid() from None


class _FileCandidateBundleWriter:
    def __init__(
        self,
        *,
        workspace_root: Path,
        target_root: Path,
        candidate_path: Path,
        staging_relative_path: str,
    ) -> None:
        self._workspace_root = workspace_root.resolve(strict=True)
        self._target_root = target_root.resolve(strict=True)
        self._candidate_path = candidate_path
        self._staging_relative_path = staging_relative_path
        self._closed = False
        self._completed = False
        self._lock = Lock()
        self._posix_fds: list[int] = []
        self._posix_links: list[tuple[int, str, int]] = []
        self._posix_directories: dict[tuple[str, ...], int] = {}
        self._windows_handles: list[int] = []
        self._windows_directories: dict[tuple[str, ...], tuple[Path, int]] = {}
        try:
            if os.name == "nt":
                self._initialize_windows()
            else:
                self._initialize_posix()
        except DomainError:
            self._close_resources()
            raise
        except MemoryError:
            self._close_resources()
            raise
        except Exception:
            self._close_resources()
            raise _location_invalid() from None

    def write_payload(self, relative_path: str, data: bytes) -> None:
        with self._lock:
            self._require_open()
            if type(data) is not bytes:
                raise _write_failed()
            parts = _payload_parts(relative_path)
            try:
                self._verify_chain()
                self._write_file(parts, data)
                self._verify_chain()
            except DomainError:
                raise
            except MemoryError:
                raise
            except Exception:
                raise _write_failed() from None

    def complete(self, manifest: bytes) -> CandidateBundleLocation:
        with self._lock:
            self._require_open()
            if type(manifest) is not bytes:
                raise _write_failed()
            location = CandidateBundleLocation(
                workspace_root=self._workspace_root,
                candidate_path=self._candidate_path,
                staging_relative_path=self._staging_relative_path,
                target_area="raw_personal",
            )
            marker_written = False
            try:
                self._verify_chain()
                self._write_file(
                    ("bundle.json",),
                    manifest,
                    discard_on_failure=True,
                )
                marker_written = True
                self._sync_directories()
            except BaseException as error:
                if marker_written:
                    self._discard_completion_marker_preserving_primary()
                if isinstance(error, DomainError):
                    raise
                if isinstance(error, MemoryError):
                    raise
                if isinstance(error, Exception):
                    raise _write_failed() from None
                raise
            self._completed = True
            return location

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._close_resources()

    def _require_open(self) -> None:
        if self._closed or self._completed:
            raise _writer_closed()

    @staticmethod
    def _directory_flags() -> int:
        return (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )

    def _initialize_posix(self) -> None:
        target_relative = self._target_root.relative_to(self._workspace_root)
        parent_relative = self._candidate_path.parent.relative_to(self._workspace_root)
        workspace_fd = os.open(self._workspace_root, self._directory_flags())
        self._posix_fds.append(workspace_fd)
        current_fd = workspace_fd
        target_count = len(target_relative.parts)
        for index, part in enumerate(parent_relative.parts):
            create = index >= target_count
            child_fd = self._open_posix_directory(current_fd, part, create=create)
            self._posix_links.append((current_fd, part, child_fd))
            current_fd = child_fd
        try:
            os.mkdir("bundle.partial", mode=0o700, dir_fd=current_fd)
        except FileExistsError:
            raise _candidate_error(
                "video_bundle_candidate_exists",
                ErrorCategory.CONFLICT,
                "Candidate path already exists",
            ) from None
        candidate_fd = self._open_posix_directory(
            current_fd,
            "bundle.partial",
            create=False,
        )
        self._posix_links.append((current_fd, "bundle.partial", candidate_fd))
        self._posix_directories[()] = candidate_fd

    def _open_posix_directory(
        self,
        parent_fd: int,
        name: str,
        *,
        create: bool,
    ) -> int:
        if create:
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
        descriptor = os.open(name, self._directory_flags(), dir_fd=parent_fd)
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise _location_invalid()
        self._posix_fds.append(descriptor)
        return descriptor

    def _initialize_windows(self) -> None:
        relative_parent = self._candidate_path.parent.relative_to(self._workspace_root)
        current = self._workspace_root
        self._open_windows_directory(current)
        target_relative = self._target_root.relative_to(self._workspace_root)
        target_count = len(target_relative.parts)
        for index, part in enumerate(relative_parent.parts):
            current = current / part
            if index >= target_count:
                try:
                    current.mkdir()
                except FileExistsError:
                    pass
            self._open_windows_directory(current)
        try:
            self._candidate_path.mkdir()
        except FileExistsError:
            raise _candidate_error(
                "video_bundle_candidate_exists",
                ErrorCategory.CONFLICT,
                "Candidate path already exists",
            ) from None
        candidate_handle = self._open_windows_directory(self._candidate_path)
        self._windows_directories[()] = (self._candidate_path, candidate_handle)

    @staticmethod
    def _kernel32():
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        kernel32.GetFileInformationByHandle.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_ByHandleFileInformation),
        )
        kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        kernel32.GetFinalPathNameByHandleW.argtypes = (
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        kernel32.FlushFileBuffers.restype = wintypes.BOOL
        kernel32.FlushFileBuffers.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        return kernel32

    def _open_windows_directory(self, path: Path) -> int:
        kernel32 = self._kernel32()
        handle = kernel32.CreateFileW(
            str(path),
            _FILE_READ_ATTRIBUTES,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        handle_value = ctypes.cast(handle, ctypes.c_void_p).value
        if handle_value == _INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        information = _ByHandleFileInformation()
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
            kernel32.CloseHandle(handle)
            raise ctypes.WinError(ctypes.get_last_error())
        if (
            not information.dwFileAttributes & _FILE_ATTRIBUTE_DIRECTORY
            or information.dwFileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT
        ):
            kernel32.CloseHandle(handle)
            raise _location_invalid()
        required = kernel32.GetFinalPathNameByHandleW(handle, None, 0, 0)
        if required == 0:
            kernel32.CloseHandle(handle)
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_unicode_buffer(required + 1)
        if kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0) == 0:
            kernel32.CloseHandle(handle)
            raise ctypes.WinError(ctypes.get_last_error())
        if _normalized_windows_path(buffer.value) != _normalized_windows_path(path):
            kernel32.CloseHandle(handle)
            raise _location_invalid()
        value = int(handle_value)
        self._windows_handles.append(value)
        return value

    def _write_file(
        self,
        parts: tuple[str, ...],
        data: bytes,
        *,
        discard_on_failure: bool = False,
    ) -> None:
        created = False
        descriptor: int | None = None
        try:
            if os.name == "nt":
                parent_path, _ = self._windows_parent(parts[:-1])
                path = parent_path / parts[-1]
                flags = (
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_BINARY", 0)
                )
                descriptor = os.open(path, flags, 0o600)
            else:
                parent_fd = self._posix_parent(parts[:-1])
                flags = (
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                descriptor = os.open(parts[-1], flags, 0o600, dir_fd=parent_fd)
            created = True
            stream = os.fdopen(descriptor, "wb")
            descriptor = None
            with stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException:
                    pass
            if created and discard_on_failure:
                self._discard_completion_marker_preserving_primary()
            raise

    def _posix_parent(self, parts: tuple[str, ...]) -> int:
        key: tuple[str, ...] = ()
        descriptor = self._posix_directories[key]
        for part in parts:
            key = (*key, part)
            existing = self._posix_directories.get(key)
            if existing is None:
                existing = self._open_posix_directory(descriptor, part, create=True)
                self._posix_links.append((descriptor, part, existing))
                self._posix_directories[key] = existing
            descriptor = existing
        return descriptor

    def _windows_parent(self, parts: tuple[str, ...]) -> tuple[Path, int]:
        key: tuple[str, ...] = ()
        path, handle = self._windows_directories[key]
        for part in parts:
            key = (*key, part)
            existing = self._windows_directories.get(key)
            if existing is None:
                path = path / part
                try:
                    path.mkdir()
                except FileExistsError:
                    pass
                handle = self._open_windows_directory(path)
                existing = (path, handle)
                self._windows_directories[key] = existing
            path, handle = existing
        return path, handle

    def _verify_chain(self) -> None:
        try:
            if os.name == "nt":
                for path, handle in self._windows_directories.values():
                    self._verify_windows_directory(path, handle)
                return
            flags = self._directory_flags()
            for parent_fd, name, child_fd in self._posix_links:
                probe = os.open(name, flags, dir_fd=parent_fd)
                try:
                    expected = os.fstat(child_fd)
                    actual = os.fstat(probe)
                    if (actual.st_dev, actual.st_ino) != (
                        expected.st_dev,
                        expected.st_ino,
                    ):
                        raise _location_invalid()
                finally:
                    os.close(probe)
        except DomainError:
            raise
        except OSError:
            raise _location_invalid() from None

    def _discard_completion_marker(self) -> None:
        try:
            if os.name == "nt":
                parent_path, _ = self._windows_parent(())
                os.unlink(parent_path / "bundle.json")
            else:
                os.unlink(
                    "bundle.json",
                    dir_fd=self._posix_directories[()],
                )
        except FileNotFoundError:
            pass

    def _discard_completion_marker_preserving_primary(self) -> None:
        try:
            self._discard_completion_marker()
        except BaseException:
            pass

    def _verify_windows_directory(self, path: Path, handle: int) -> None:
        kernel32 = self._kernel32()
        information = _ByHandleFileInformation()
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
            raise ctypes.WinError(ctypes.get_last_error())
        if (
            not information.dwFileAttributes & _FILE_ATTRIBUTE_DIRECTORY
            or information.dwFileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise _location_invalid()
        required = kernel32.GetFinalPathNameByHandleW(handle, None, 0, 0)
        if required == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_unicode_buffer(required + 1)
        if kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0) == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        if _normalized_windows_path(buffer.value) != _normalized_windows_path(path):
            raise _location_invalid()

    def _sync_directories(self) -> None:
        if os.name == "nt":
            kernel32 = self._kernel32()
            for handle in reversed(self._windows_handles):
                if not kernel32.FlushFileBuffers(handle):
                    error = ctypes.get_last_error()
                    # Windows does not guarantee directory-handle flushing. These
                    # errors mean that directory sync is unavailable; file fsync is
                    # still mandatory and all directory handles remain pinned.
                    if error not in _WINDOWS_UNSUPPORTED_DIRECTORY_FLUSH_ERRORS:
                        raise ctypes.WinError(error)
            return
        for descriptor in reversed(self._posix_fds):
            os.fsync(descriptor)

    def _close_resources(self) -> None:
        if os.name == "nt" and self._windows_handles:
            kernel32 = self._kernel32()
            while self._windows_handles:
                kernel32.CloseHandle(self._windows_handles.pop())
        while self._posix_fds:
            try:
                os.close(self._posix_fds.pop())
            except OSError:
                pass


class IWikiPortableGateway:
    def __init__(self) -> None:
        self._prepared: dict[int, tuple[PreparedBundle, Path]] = {}
        self._claimed: dict[int, PreparedBundle] = {}
        self._consumed: OrderedDict[int, PreparedBundle] = OrderedDict()
        self._prepared_lock = Lock()

    def _claim_prepared(
        self,
        prepared: PreparedBundle,
    ) -> tuple[int, tuple[PreparedBundle, Path] | None]:
        prepared_id = id(prepared)
        with self._prepared_lock:
            binding = self._prepared.get(prepared_id)
            if binding is not None and binding[0] is prepared:
                del self._prepared[prepared_id]
                self._claimed[prepared_id] = prepared
                return _CLAIM_ACQUIRED, binding
            if self._claimed.get(prepared_id) is prepared:
                return _CLAIM_IN_PROGRESS, None
            if self._consumed.get(prepared_id) is prepared:
                self._consumed.move_to_end(prepared_id)
                return _CLAIM_CONSUMED, None
            return _CLAIM_UNKNOWN, None

    def _finish_prepared(self, prepared: PreparedBundle) -> None:
        prepared_id = id(prepared)
        with self._prepared_lock:
            claimed = self._claimed.pop(prepared_id)
            assert claimed is prepared
            self._consumed[prepared_id] = prepared
            if len(self._consumed) > _CONSUMED_TOMBSTONE_LIMIT:
                self._consumed.popitem(last=False)

    @staticmethod
    def _close_prepared(prepared: PreparedBundle) -> None:
        try:
            prepared.close()
        except IWikiError as error:
            raise _map_iwiki_error(error) from None
        except _COMPATIBILITY_ERRORS:
            raise _contract_incompatible() from None

    def candidate_location(
        self,
        workspace_root: Path,
        *,
        local_instance_id: str,
        nonce: str,
    ) -> CandidateLocationCapabilityPort:
        if not isinstance(workspace_root, Path):
            raise _location_invalid()
        for component, pattern in (
            (local_instance_id, _STAGING_COMPONENT),
            (nonce, _NONCE_COMPONENT),
        ):
            if type(component) is not str or pattern.fullmatch(component) is None:
                raise _location_invalid()
            _require_portable_component(component)
        return _CandidateLocationCapability(
            workspace_root,
            local_instance_id,
            nonce,
        )

    def inspect(self, workspace_root: Path) -> PortableContractInfo:
        _, info = _open_locked_workspace(workspace_root)
        return info

    def validate_candidate(
        self,
        workspace_root: Path,
        staging_relative_path: str,
    ) -> PortableValidationReport:
        workspace, _ = _open_locked_workspace(workspace_root)
        try:
            return validate_bundle(
                workspace,
                PortableBundleRef.staging(staging_relative_path),
                ValidationLevel.SEMANTIC,
            )
        except IWikiError as error:
            raise _map_iwiki_error(error) from None
        except _COMPATIBILITY_ERRORS:
            raise _contract_incompatible() from None

    def prepare_candidate(
        self,
        workspace_root: Path,
        staging_relative_path: str,
        *,
        expected_bundle_id: str,
        expected_manifest_sha256: str,
    ) -> PreparedBundle:
        workspace, _ = _open_locked_workspace(workspace_root)
        try:
            prepared = prepare_bundle_commit(
                workspace,
                PortableBundleRef.staging(staging_relative_path),
                expected_bundle_id=expected_bundle_id,
                expected_manifest_sha256=expected_manifest_sha256,
            )
        except IWikiError as error:
            raise _map_iwiki_error(error) from None
        except _COMPATIBILITY_ERRORS:
            raise _contract_incompatible() from None
        with self._prepared_lock:
            self._prepared[id(prepared)] = (prepared, workspace.root)
        return prepared

    def commit_prepared(self, prepared: PreparedBundle) -> CommitResult:
        if not isinstance(prepared, PreparedBundle):
            raise _prepared_bundle_invalid()
        claim_state, binding = self._claim_prepared(prepared)
        if claim_state != _CLAIM_ACQUIRED or binding is None:
            raise _prepared_bundle_invalid()
        try:
            _open_locked_workspace(binding[1])
            try:
                result = commit_prepared_bundle(prepared)
            except IWikiError as error:
                if error.code is ErrorCode.INVALID_ARGUMENT:
                    raise _prepared_bundle_invalid() from None
                raise _map_iwiki_error(error) from None
            except _COMPATIBILITY_ERRORS:
                raise _contract_incompatible() from None
        except BaseException:
            try:
                self._close_prepared(prepared)
            except Exception:
                pass
            raise
        else:
            self._close_prepared(prepared)
            return result
        finally:
            self._finish_prepared(prepared)

    def discard_prepared(self, prepared: PreparedBundle) -> None:
        if not isinstance(prepared, PreparedBundle):
            raise _prepared_bundle_invalid()
        claim_state, _ = self._claim_prepared(prepared)
        if claim_state == _CLAIM_CONSUMED:
            return
        if claim_state != _CLAIM_ACQUIRED:
            raise _prepared_bundle_invalid()
        try:
            self._close_prepared(prepared)
        finally:
            self._finish_prepared(prepared)

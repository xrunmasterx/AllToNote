"""Secure logical activation for document-basic Packs.

The active pointer change is atomic. Power-loss durability remains platform-dependent.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from errno import EACCES, ENOSYS
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout

from app.adapters.documents.document_basic_pack import PACK_ID, PACK_VERSION
from app.adapters.documents.document_basic_pack_verifier import (
    DocumentPackTrustKey,
    VerifiedDocumentPack,
    _MAX_FILE_BYTES,
    _MAX_MANIFEST_BYTES,
    _MAX_TOTAL_BYTES,
    _MAX_TREE_ENTRY_COUNT,
    _is_link_or_reparse,
    _normalized_relative_path,
    _read_regular_file,
    _same_open_file,
    canonical_receipt_bytes,
    verify_document_basic_pack_generation,
    verify_document_basic_pack_manifest,
    verify_document_basic_pack_source,
)
from app.core.errors import DomainError, ErrorCategory
from app.runtime_paths import RuntimePaths


_LOCK_TIMEOUT_SECONDS = 30
_CONTROL_FILE_LIMIT = 16 * 1024
_SHA256_PATTERN = re.compile(r"sha256:([0-9a-f]{64})\Z")
_ACTIVE_KEYS = frozenset(
    {"schema_version", "pack_id", "pack_version", "manifest_sha256"}
)
_STAGE_PATTERN = re.compile(r"\.stage-[0-9a-f]{32}\Z")
_ACTIVE_TEMP_PATTERN = re.compile(r"\.active-[0-9a-f]{32}\.tmp\Z")


if os.name == "nt":
    import msvcrt
else:
    import fcntl


class _PackFileLock(FileLock):
    """Lock a stable ordinary inode without truncating a linked target."""

    def _acquire(self) -> None:
        lock_path = Path(self.lock_file)
        if _lexically_exists(lock_path) and not _ordinary_file(lock_path):
            raise _install_conflict()
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOINHERIT", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            file_descriptor = os.open(lock_path, flags, self._context.mode)
        except OSError as error:
            if _lexically_exists(lock_path) and not _ordinary_file(lock_path):
                raise _install_conflict() from error
            if os.name == "nt" and error.errno == EACCES:
                return
            raise
        if not _open_lock_matches_path(lock_path, file_descriptor):
            os.close(file_descriptor)
            raise _install_conflict()
        try:
            if os.name == "nt":
                msvcrt.locking(file_descriptor, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(file_descriptor)
            if os.name == "nt":
                if error.errno != EACCES:
                    raise
            elif error.errno == ENOSYS:
                raise NotImplementedError(
                    "The Pack filesystem does not support flock"
                ) from error
            return
        if not _open_lock_matches_path(lock_path, file_descriptor):
            os.close(file_descriptor)
            raise _install_conflict()
        self._context.lock_file_fd = file_descriptor

    def _release(self) -> None:
        file_descriptor = self._context.lock_file_fd
        if file_descriptor is None:
            return
        self._context.lock_file_fd = None
        if os.name == "nt":
            msvcrt.locking(file_descriptor, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(file_descriptor, fcntl.LOCK_UN)
        os.close(file_descriptor)


@dataclass(frozen=True)
class DocumentPackInstallResult:
    manifest_sha256: str
    generation: Path
    active_pointer: Path
    result: str


@dataclass(frozen=True)
class _ActiveState:
    kind: str
    digest: str | None = None
    raw: bytes | None = None


class _UnsafeSource(Exception):
    pass


def _install_conflict() -> DomainError:
    return DomainError(
        "pack_install_conflict",
        ErrorCategory.CONFLICT,
        "The document-basic Pack installation conflicts with managed state",
    )


def _install_failed(error: BaseException) -> DomainError:
    return DomainError(
        "pack_install_failed",
        ErrorCategory.RETRYABLE_RUNTIME,
        "The document-basic Pack could not be installed",
    )


def _ordinary_directory(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return not _is_link_or_reparse(metadata) and stat.S_ISDIR(metadata.st_mode)


def _ordinary_file(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        not _is_link_or_reparse(metadata)
        and stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
    )


def _open_lock_matches_path(path: Path, file_descriptor: int) -> bool:
    try:
        metadata = path.lstat()
        opened = os.fstat(file_descriptor)
    except OSError:
        return False
    return (
        not _is_link_or_reparse(metadata)
        and stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and metadata.st_dev == opened.st_dev
        and metadata.st_ino == opened.st_ino
    )


def _lexically_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _ensure_managed_roots(paths: RuntimePaths) -> tuple[Path, Path]:
    pack_root = paths.data_dir / "packs" / PACK_ID / PACK_VERSION
    installs_root = pack_root / "installs"
    try:
        paths.data_dir.mkdir(parents=True, exist_ok=True)
        if not _ordinary_directory(paths.data_dir):
            raise _install_conflict()
        current = paths.data_dir
        for part in ("packs", PACK_ID, PACK_VERSION, "installs"):
            current /= part
            current.mkdir(exist_ok=True)
            if not _ordinary_directory(current):
                raise _install_conflict()
    except DomainError:
        raise
    except OSError as error:
        raise _install_failed(error) from error
    return pack_root, installs_root


def _validate_source_location(source: Path, pack_root: Path) -> Path:
    try:
        absolute = Path(os.path.abspath(os.fspath(Path(source).expanduser())))
        resolved_source = absolute.resolve(strict=True)
        resolved_pack = pack_root.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as error:
        raise DomainError(
            "pack_source_invalid",
            ErrorCategory.INVALID_REQUEST,
            "The document-basic Pack source is unavailable",
        ) from error
    if (
        resolved_source == resolved_pack
        or resolved_source.is_relative_to(resolved_pack)
        or resolved_pack.is_relative_to(resolved_source)
    ):
        raise DomainError(
            "pack_source_invalid",
            ErrorCategory.INVALID_REQUEST,
            "The document-basic Pack source must be outside managed Pack state",
        )
    return absolute


def _safe_relative(path: Path, root: Path) -> str:
    try:
        return _normalized_relative_path(path.relative_to(root).as_posix())
    except (DomainError, RuntimeError, ValueError) as error:
        raise _UnsafeSource("unsafe_source_path") from error


def _copy_regular_file(
    source: Path,
    destination: Path,
    *,
    byte_limit: int,
) -> int:
    try:
        metadata = source.lstat()
        if (
            _is_link_or_reparse(metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > byte_limit
        ):
            raise _UnsafeSource("unsafe_source_file")
        source_stream = source.open("rb")
    except _UnsafeSource:
        raise
    except OSError as error:
        raise _UnsafeSource("source_file_unavailable") from error

    try:
        opened = os.fstat(source_stream.fileno())
        if not _same_open_file(metadata, opened):
            raise _UnsafeSource("source_changed_before_copy")
        byte_count = 0
        with destination.open("xb") as destination_stream:
            while True:
                try:
                    chunk = source_stream.read(1024 * 1024)
                except OSError as error:
                    raise _UnsafeSource("source_changed_during_copy") from error
                if not chunk:
                    break
                byte_count += len(chunk)
                if byte_count > byte_limit or byte_count > metadata.st_size:
                    raise _UnsafeSource("source_size_changed")
                destination_stream.write(chunk)
            destination_stream.flush()
            os.fsync(destination_stream.fileno())
        try:
            finished = os.fstat(source_stream.fileno())
        except OSError as error:
            raise _UnsafeSource("source_changed_after_copy") from error
        if byte_count != metadata.st_size or not _same_open_file(opened, finished):
            raise _UnsafeSource("source_changed_during_copy")
        return byte_count
    finally:
        source_stream.close()


def _materialize_source(source: Path, stage: Path) -> None:
    try:
        metadata = source.lstat()
    except OSError as error:
        raise DomainError(
            "pack_source_invalid",
            ErrorCategory.INVALID_REQUEST,
            "The document-basic Pack source is unavailable",
        ) from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise DomainError(
            "pack_source_invalid",
            ErrorCategory.INVALID_REQUEST,
            "The document-basic Pack source must be a directory",
        )
    if _is_link_or_reparse(metadata):
        raise DomainError(
            "pack_archive_unsafe",
            ErrorCategory.INVALID_REQUEST,
            "The document-basic Pack source contains unsafe filesystem entries",
        )

    entry_count = 0
    total_bytes = 0

    def raise_walk_error(error: OSError) -> None:
        raise _UnsafeSource("source_walk_failed") from error

    try:
        for current, directories, filenames in os.walk(
            source,
            followlinks=False,
            onerror=raise_walk_error,
        ):
            directories.sort()
            filenames.sort()
            current_path = Path(current)
            destination_directory = stage / current_path.relative_to(source)
            if not _ordinary_directory(destination_directory):
                raise OSError("unsafe_stage_directory")
            for name in directories:
                entry_count += 1
                if entry_count > _MAX_TREE_ENTRY_COUNT:
                    raise _UnsafeSource("source_entry_limit")
                child = current_path / name
                _safe_relative(child, source)
                child_metadata = child.lstat()
                if _is_link_or_reparse(child_metadata) or not stat.S_ISDIR(
                    child_metadata.st_mode
                ):
                    raise _UnsafeSource("unsafe_source_directory")
                target = destination_directory / name
                target.mkdir()
                if not _ordinary_directory(target):
                    raise OSError("unsafe_stage_directory")
            for name in filenames:
                entry_count += 1
                if entry_count > _MAX_TREE_ENTRY_COUNT:
                    raise _UnsafeSource("source_entry_limit")
                child = current_path / name
                relative = _safe_relative(child, source)
                file_limit = (
                    _MAX_MANIFEST_BYTES
                    if relative == "manifest.json"
                    else _MAX_FILE_BYTES
                )
                copied = _copy_regular_file(
                    child,
                    destination_directory / name,
                    byte_limit=file_limit,
                )
                total_bytes += copied
                if total_bytes > _MAX_TOTAL_BYTES + _MAX_MANIFEST_BYTES:
                    raise _UnsafeSource("source_total_limit")
    except DomainError:
        raise
    except _UnsafeSource as error:
        raise DomainError(
            "pack_archive_unsafe",
            ErrorCategory.INVALID_REQUEST,
            "The document-basic Pack source contains unsafe filesystem entries",
        ) from error


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate_json_key")
        payload[key] = value
    return payload


def _read_active_state(path: Path) -> _ActiveState:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return _ActiveState("absent")
    except OSError:
        return _ActiveState("unsafe")
    if (
        _is_link_or_reparse(metadata)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > _CONTROL_FILE_LIMIT
    ):
        return _ActiveState("unsafe")
    raw: bytes | None = None
    try:
        raw = _read_regular_file(path, _CONTROL_FILE_LIMIT)
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non_finite_json")
            ),
        )
    except (OSError, UnicodeError, ValueError):
        return _ActiveState("malformed", raw=raw)
    if type(payload) is not dict or frozenset(payload) != _ACTIVE_KEYS:
        return _ActiveState("malformed", raw=raw)
    manifest_sha256 = payload.get("manifest_sha256")
    match = (
        _SHA256_PATTERN.fullmatch(manifest_sha256)
        if type(manifest_sha256) is str
        else None
    )
    if (
        type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != 1
        or payload.get("pack_id") != PACK_ID
        or payload.get("pack_version") != PACK_VERSION
        or match is None
    ):
        return _ActiveState("malformed", raw=raw)
    return _ActiveState("valid", digest=match.group(1), raw=raw)


def _control_bytes(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        dict(payload),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_receipt(stage: Path, manifest_sha256: str) -> None:
    receipt = canonical_receipt_bytes(manifest_sha256)
    with (stage / "receipt.json").open("xb") as stream:
        stream.write(receipt)
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_active_pointer(
    pack_root: Path,
    active_path: Path,
    *,
    manifest_sha256: str,
    expected_state: _ActiveState,
) -> None:
    if _read_active_state(active_path) != expected_state:
        raise _install_conflict()
    temporary = pack_root / f".active-{uuid4().hex}.tmp"
    payload = _control_bytes(
        {
            "schema_version": 1,
            "pack_id": PACK_ID,
            "pack_version": PACK_VERSION,
            "manifest_sha256": manifest_sha256,
        }
    )
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, active_path)
        try:
            _sync_directory(pack_root)
        except OSError:
            # os.replace is the activation commit point; do not report a false failure.
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _remove_stage(stage: Path, installs_root: Path) -> None:
    try:
        if (
            stage.parent == installs_root
            and _STAGE_PATTERN.fullmatch(stage.name)
            and _ordinary_directory(stage)
        ):
            shutil.rmtree(stage)
    except OSError:
        pass


def _cleanup_orphans(pack_root: Path, installs_root: Path) -> None:
    for child in installs_root.iterdir():
        if _STAGE_PATTERN.fullmatch(child.name) and _ordinary_directory(child):
            shutil.rmtree(child)
    for child in pack_root.iterdir():
        if _ACTIVE_TEMP_PATTERN.fullmatch(child.name) and _ordinary_file(child):
            child.unlink()


def _verified_existing_generation(
    generation: Path,
    *,
    manifest_sha256: str,
    trusted_keys: Mapping[str, DocumentPackTrustKey],
    platform_tag: str | None,
) -> VerifiedDocumentPack | None:
    if not _lexically_exists(generation):
        return None
    if not _ordinary_directory(generation):
        raise _install_conflict()
    try:
        verified = verify_document_basic_pack_generation(
            generation,
            trusted_keys=trusted_keys,
            platform_tag=platform_tag,
        )
    except DomainError as error:
        raise _install_conflict() from error
    if verified.manifest_sha256 != manifest_sha256:
        raise _install_conflict()
    return verified


def install_document_basic_pack(
    source: Path,
    *,
    paths: RuntimePaths,
    trusted_keys: Mapping[str, DocumentPackTrustKey],
    probe: Callable[[Path, Path], None],
    repair: bool = False,
    environ: Mapping[str, str] | None = None,
    platform_tag: str | None = None,
) -> DocumentPackInstallResult:
    environment = os.environ if environ is None else environ
    if environment.get("ALLTONOTE_DOCUMENT_BASIC_PYTHON") or environment.get(
        "ALLTONOTE_DOCUMENT_BASIC_ARTIFACTS"
    ):
        raise DomainError(
            "pack_override_active",
            ErrorCategory.CONFLICT,
            "Managed document-basic Pack installation is blocked by an active override",
        )

    pack_root = paths.data_dir / "packs" / PACK_ID / PACK_VERSION
    source_root = _validate_source_location(Path(source), pack_root)
    pack_root, installs_root = _ensure_managed_roots(paths)
    lock_path = pack_root / "install.lock"
    try:
        with _PackFileLock(lock_path, timeout=_LOCK_TIMEOUT_SECONDS):
            pack_root, installs_root = _ensure_managed_roots(paths)
            if not _ordinary_file(lock_path):
                raise _install_conflict()
            _cleanup_orphans(pack_root, installs_root)
            active_path = pack_root / "active.json"
            active = _read_active_state(active_path)
            if active.kind == "valid" and not repair:
                requested = verify_document_basic_pack_manifest(
                    source_root,
                    trusted_keys=trusted_keys,
                    platform_tag=platform_tag,
                )
                digest = requested.manifest_sha256.removeprefix("sha256:")
                if active.digest == digest:
                    generation = installs_root / digest
                    installed = _verified_existing_generation(
                        generation,
                        manifest_sha256=requested.manifest_sha256,
                        trusted_keys=trusted_keys,
                        platform_tag=platform_tag,
                    )
                    if installed is not None:
                        if _read_active_state(active_path) != active:
                            raise _install_conflict()
                        return DocumentPackInstallResult(
                            manifest_sha256=installed.manifest_sha256,
                            generation=generation,
                            active_pointer=active_path,
                            result="already_active",
                        )

            stage = installs_root / f".stage-{uuid4().hex}"
            stage.mkdir()
            if not _ordinary_directory(stage):
                raise _install_conflict()
            try:
                _materialize_source(source_root, stage)
                verified = verify_document_basic_pack_source(
                    stage,
                    trusted_keys=trusted_keys,
                    platform_tag=platform_tag,
                )
                probe(
                    stage.joinpath(*verified.python_relative_path.split("/")),
                    stage / "artifacts",
                )
                _write_receipt(stage, verified.manifest_sha256)
                verify_document_basic_pack_generation(
                    stage,
                    trusted_keys=trusted_keys,
                    platform_tag=platform_tag,
                )

                digest = verified.manifest_sha256.removeprefix("sha256:")
                generation = installs_root / digest
                active = _read_active_state(active_path)
                if active.kind == "unsafe":
                    raise _install_conflict()
                if active.kind == "malformed" and not repair:
                    raise _install_conflict()
                if active.kind == "valid" and active.digest != digest:
                    raise _install_conflict()

                existing_generation = _verified_existing_generation(
                    generation,
                    manifest_sha256=verified.manifest_sha256,
                    trusted_keys=trusted_keys,
                    platform_tag=platform_tag,
                )
                generation_exists = existing_generation is not None
                if active.kind == "valid" and not generation_exists and not repair:
                    raise _install_conflict()
                if not generation_exists:
                    stage.rename(generation)
                    _sync_directory(installs_root)

                if active.kind == "absent":
                    _write_active_pointer(
                        pack_root,
                        active_path,
                        manifest_sha256=verified.manifest_sha256,
                        expected_state=active,
                    )
                    result = "installed"
                elif active.kind == "malformed":
                    _write_active_pointer(
                        pack_root,
                        active_path,
                        manifest_sha256=verified.manifest_sha256,
                        expected_state=active,
                    )
                    result = "repaired"
                elif generation_exists:
                    result = "already_active"
                else:
                    result = "repaired"
                return DocumentPackInstallResult(
                    manifest_sha256=verified.manifest_sha256,
                    generation=generation,
                    active_pointer=active_path,
                    result=result,
                )
            finally:
                _remove_stage(stage, installs_root)
    except Timeout as error:
        raise DomainError(
            "pack_install_busy",
            ErrorCategory.RETRYABLE_RUNTIME,
            "Another document-basic Pack installation is in progress",
        ) from error
    except DomainError:
        raise
    except OSError as error:
        raise _install_failed(error) from error


__all__ = ["DocumentPackInstallResult", "install_document_basic_pack"]

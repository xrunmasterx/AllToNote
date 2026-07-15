from __future__ import annotations

import json
import hashlib
import os
import re
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from uuid import uuid4

from app.core.domain.ids import sha256_digest, utc_now_millis
from app.core.application.video_acquisition import AttemptStoredAsset, StoredAssetRole
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.model import (
    CheckpointMetadata,
    CheckpointRecord,
    JobEvent,
)
from app.core.jobs.resource_lease import ExecutionAuthority
from app.core.ports.jobs import AttemptMetadataRepositoryPort
from app.core.ports.source import CancellationTokenPort


ContentValidator = Callable[[bytes], bool]
_EVENT_FIELDS = frozenset(
    {
        "event_id",
        "job_id",
        "sequence",
        "event_type",
        "payload_json",
        "created_at",
    }
)
_SAFE_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_WINDOWS_RESERVED_SEGMENTS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


def _projection_invalid() -> DomainError:
    return DomainError(
        "event_projection_invalid",
        ErrorCategory.WORKSPACE_INCOMPATIBLE,
        "Event projection contains an invalid record",
    )


def _storage_path_invalid() -> DomainError:
    return DomainError(
        "attempt_storage_path_invalid",
        ErrorCategory.WORKSPACE_INCOMPATIBLE,
        "Attempt storage path is unsafe",
    )


def _stored_asset_invalid() -> DomainError:
    return DomainError(
        "attempt_stored_asset_invalid",
        ErrorCategory.WORKSPACE_INCOMPATIBLE,
        "Attempt stored asset is invalid",
    )


def _storage_io_failed() -> DomainError:
    return DomainError(
        "attempt_storage_io_failed",
        ErrorCategory.RETRYABLE_RUNTIME,
        "Attempt storage could not publish the asset",
    )


class FileAttemptStorage:
    def __init__(
        self,
        root: Path,
        metadata_repository: AttemptMetadataRepositoryPort,
        *,
        validators: Mapping[str, ContentValidator],
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._metadata_repository = metadata_repository
        self._validators = dict(validators)

    def save_checkpoint(
        self,
        record: CheckpointRecord,
        authority: ExecutionAuthority,
    ) -> CheckpointMetadata:
        self._validate_job_id(record.job_id)
        if not self._content_is_valid(record.schema_id, record.payload):
            raise DomainError(
                "checkpoint_content_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Checkpoint payload does not match its registered schema",
            )

        checkpoint_id = f"cp_{uuid4().hex}"
        relative_path = (
            Path("jobs")
            / record.job_id
            / "checkpoints"
            / f"{checkpoint_id}.payload"
        )
        target = self._checked_target(relative_path, create_parent=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(record.payload)
                handle.flush()
                os.fsync(handle.fileno())
            self._checked_target(relative_path, create_parent=False)
            os.replace(temporary, target)
            metadata = CheckpointMetadata(
                checkpoint_id=checkpoint_id,
                job_id=record.job_id,
                step_id=record.step_id,
                attempt_id=record.attempt_id,
                relative_path=relative_path.as_posix(),
                schema_id=record.schema_id,
                input_hash=record.input_hash,
                output_hash=sha256_digest(record.payload),
                byte_length=len(record.payload),
                metadata_json=record.metadata_json,
                created_at=utc_now_millis(),
            )
            return self._metadata_repository.record_checkpoint(metadata, authority)
        finally:
            if temporary.exists():
                temporary.unlink()

    def validate_checkpoint(
        self,
        metadata: CheckpointMetadata,
        *,
        expected_schema_id: str,
        expected_input_hash: str,
    ) -> bool:
        if (
            metadata.schema_id != expected_schema_id
            or metadata.input_hash != expected_input_hash
        ):
            return False
        relative_path = Path(metadata.relative_path)
        try:
            self._validate_job_id(metadata.job_id)
        except DomainError:
            return False
        expected_path = (
            Path("jobs")
            / metadata.job_id
            / "checkpoints"
            / f"{metadata.checkpoint_id}.payload"
        )
        if relative_path.parts != expected_path.parts:
            return False
        try:
            resolved = self._checked_target(relative_path, create_parent=False)
            if (
                not resolved.is_file()
                or resolved.stat().st_size != metadata.byte_length
            ):
                return False
            payload = resolved.read_bytes()
        except (DomainError, OSError):
            return False
        return (
            sha256_digest(payload) == metadata.output_hash
            and self._content_is_valid(metadata.schema_id, payload)
        )

    def snapshot_asset(
        self,
        source_path: Path,
        *,
        job_id: str,
        attempt_id: str,
        role: StoredAssetRole,
        expected_sha256: str,
        authority: ExecutionAuthority,
        token: CancellationTokenPort,
    ) -> AttemptStoredAsset:
        self._validate_job_id(job_id)
        self._validate_job_id(attempt_id)
        if (
            role is not StoredAssetRole.SOURCE_MEDIA
            or type(expected_sha256) is not str
            or len(expected_sha256) != 71
            or not expected_sha256.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in expected_sha256[7:])
        ):
            raise _stored_asset_invalid()
        self._metadata_repository.authorize_attempt_storage(
            job_id, attempt_id, authority
        )
        token.raise_if_cancelled()
        source = self._checked_regular_source(Path(source_path))
        suffix = source.suffix
        if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix) is None:
            suffix = ".media"
        relative = (
            Path("jobs")
            / job_id
            / "attempts"
            / attempt_id
            / "assets"
            / f"{role.value}{suffix.lower()}"
        )
        try:
            target = self._checked_target(relative, create_parent=True)
            if os.path.lexists(target):
                raise _stored_asset_invalid()
        except DomainError:
            raise
        except OSError:
            raise _storage_io_failed() from None
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.partial")
        verified_length = 0
        try:
            with source.open("rb") as source_stream:
                source_stat = os.fstat(source_stream.fileno())
                path_stat = os.lstat(source)
                if (
                    not stat.S_ISREG(source_stat.st_mode)
                    or (source_stat.st_dev, source_stat.st_ino)
                    != (path_stat.st_dev, path_stat.st_ino)
                    or self._path_chain_has_reparse_point(source)
                ):
                    raise _stored_asset_invalid()
                with temporary.open("xb") as target_stream:
                    while chunk := source_stream.read(1024 * 1024):
                        token.raise_if_cancelled()
                        if target_stream.write(chunk) != len(chunk):
                            raise OSError("short attempt asset write")
                    target_stream.flush()
                    os.fsync(target_stream.fileno())
            verified_digest, verified_length = self._verified_file_digest(temporary)
            if verified_length <= 0 or verified_digest != expected_sha256:
                raise _stored_asset_invalid()
            token.raise_if_cancelled()
            self._metadata_repository.authorize_attempt_storage(
                job_id, attempt_id, authority
            )
            self._checked_target(relative, create_parent=False)
            os.replace(temporary, target)
        except DomainError:
            raise
        except OSError:
            raise _storage_io_failed() from None
        finally:
            try:
                if os.path.lexists(temporary):
                    temporary.unlink()
            except OSError:
                pass
        return AttemptStoredAsset(
            relative_locator=relative.as_posix(),
            sha256=expected_sha256,
            byte_length=verified_length,
            role=role,
        )

    def resolve_asset(
        self,
        stored: AttemptStoredAsset,
        *,
        expected_job_id: str,
        expected_attempt_id: str,
    ) -> Path:
        if not isinstance(stored, AttemptStoredAsset):
            raise _stored_asset_invalid()
        try:
            self._validate_job_id(expected_job_id)
            self._validate_job_id(expected_attempt_id)
        except DomainError:
            raise _stored_asset_invalid() from None
        expected_prefix = (
            "jobs",
            expected_job_id,
            "attempts",
            expected_attempt_id,
            "assets",
        )
        relative = Path(stored.relative_locator)
        if relative.parts[:5] != expected_prefix or len(relative.parts) != 6:
            raise _stored_asset_invalid()
        try:
            resolved = self._checked_target(relative, create_parent=False)
            if self._path_chain_has_reparse_point(resolved):
                raise _stored_asset_invalid()
            digest, byte_length = self._verified_file_digest(resolved)
        except (DomainError, OSError):
            raise _stored_asset_invalid() from None
        if (
            byte_length != stored.byte_length
            or digest != stored.sha256
        ):
            raise _stored_asset_invalid()
        return resolved

    def append_event(
        self, job_id: str, event_type: str, payload_json: str
    ) -> JobEvent:
        self._validate_job_id(job_id)
        event = self._metadata_repository.append_event(
            job_id, event_type, payload_json
        )
        projection = self._projection_path(job_id)
        self._validate_existing_projection(projection)
        self._publish_until_caught_up(job_id)
        return event

    def reconcile_event_projection(self, job_id: str) -> tuple[JobEvent, ...]:
        self._validate_job_id(job_id)
        projection = self._projection_path(job_id)
        self._validate_existing_projection(projection)
        return self._publish_until_caught_up(job_id)

    def _publish_until_caught_up(self, job_id: str) -> tuple[JobEvent, ...]:
        while True:
            events = self._metadata_repository.list_events(job_id)
            self._write_projection(job_id, events)
            last_sequence = events[-1].sequence if events else 0
            if not self._metadata_repository.list_events(
                job_id, after_sequence=last_sequence
            ):
                return events

    def _write_projection(
        self, job_id: str, events: tuple[JobEvent, ...]
    ) -> None:
        relative_path = Path("jobs") / job_id / "events.jsonl"
        projection = self._checked_target(relative_path, create_parent=True)
        payload = b"".join(self._event_line(event) for event in events)
        temporary = projection.with_name(f".{projection.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self._checked_target(relative_path, create_parent=False)
            os.replace(temporary, projection)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _projection_path(self, job_id: str) -> Path:
        return self.root / "jobs" / job_id / "events.jsonl"

    def _validate_existing_projection(self, projection: Path) -> None:
        relative_path = projection.relative_to(self.root)
        checked = self._checked_target(relative_path, create_parent=True)
        if not os.path.lexists(checked):
            return
        try:
            self._validate_projection(checked.read_bytes())
        except OSError as error:
            raise _projection_invalid() from error

    @staticmethod
    def _validate_job_id(job_id: str) -> None:
        if (
            type(job_id) is not str
            or len(job_id) > 255
            or _SAFE_JOB_ID.fullmatch(job_id) is None
            or job_id.endswith(".")
            or job_id.split(".", 1)[0].upper() in _WINDOWS_RESERVED_SEGMENTS
        ):
            raise _storage_path_invalid()

    def _checked_target(
        self, relative_path: Path, *, create_parent: bool
    ) -> Path:
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise _storage_path_invalid()
        candidate = self.root / relative_path
        current = self.root
        for part in relative_path.parts[:-1]:
            current /= part
            if not os.path.lexists(current):
                if not create_parent:
                    raise _storage_path_invalid()
                try:
                    current.mkdir()
                except FileExistsError:
                    pass
            if self._is_reparse_point(current) or not current.is_dir():
                raise _storage_path_invalid()
            try:
                if not current.resolve(strict=True).is_relative_to(self.root):
                    raise _storage_path_invalid()
            except OSError as error:
                raise _storage_path_invalid() from error
        if self._is_reparse_point(candidate):
            raise _storage_path_invalid()
        try:
            if not candidate.resolve(strict=False).is_relative_to(self.root):
                raise _storage_path_invalid()
        except OSError as error:
            raise _storage_path_invalid() from error
        return candidate

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        try:
            path_stat = os.lstat(path)
        except FileNotFoundError:
            return False
        attributes = getattr(path_stat, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return stat.S_ISLNK(path_stat.st_mode) or bool(attributes & reparse_flag)

    @classmethod
    def _path_chain_has_reparse_point(cls, path: Path) -> bool:
        current = path
        while True:
            if cls._is_reparse_point(current):
                return True
            if current.parent == current:
                return False
            current = current.parent

    @classmethod
    def _checked_regular_source(cls, path: Path) -> Path:
        try:
            if cls._path_chain_has_reparse_point(path):
                raise _stored_asset_invalid()
            resolved = path.resolve(strict=True)
            path_stat = os.lstat(resolved)
            unresolved = path.resolve(strict=False)
        except DomainError:
            raise
        except (OSError, RuntimeError):
            raise _storage_io_failed() from None
        if resolved != unresolved or not stat.S_ISREG(path_stat.st_mode):
            raise _stored_asset_invalid()
        return resolved

    @classmethod
    def _verified_file_digest(cls, path: Path) -> tuple[str, int]:
        if cls._path_chain_has_reparse_point(path):
            raise _stored_asset_invalid()
        digest = hashlib.sha256()
        byte_length = 0
        with path.open("rb") as stream:
            opened_stat = os.fstat(stream.fileno())
            path_stat = os.lstat(path)
            if (
                not stat.S_ISREG(opened_stat.st_mode)
                or (opened_stat.st_dev, opened_stat.st_ino)
                != (path_stat.st_dev, path_stat.st_ino)
            ):
                raise _stored_asset_invalid()
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                byte_length += len(chunk)
        return f"sha256:{digest.hexdigest()}", byte_length

    def _content_is_valid(self, schema_id: str, payload: bytes) -> bool:
        validator = self._validators.get(schema_id)
        if validator is None:
            return False
        try:
            return validator(payload) is True
        except Exception:
            return False

    @staticmethod
    def _event_line(event: JobEvent) -> bytes:
        record = {
            "created_at": event.created_at,
            "event_id": event.event_id,
            "event_type": event.event_type,
            "job_id": event.job_id,
            "payload_json": event.payload_json,
            "sequence": event.sequence,
        }
        return (
            json.dumps(
                record,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            + b"\n"
        )

    @staticmethod
    def _validate_projection(payload: bytes) -> None:
        lines = payload.splitlines(keepends=True)
        previous_sequence = 0
        for index, raw_line in enumerate(lines):
            is_final = index == len(lines) - 1
            terminated = raw_line.endswith(b"\n")
            try:
                record = json.loads(raw_line)
            except UnicodeDecodeError as error:
                if (
                    is_final
                    and not terminated
                    and error.reason == "unexpected end of data"
                    and error.end == len(raw_line)
                ):
                    return
                raise _projection_invalid() from None
            except json.JSONDecodeError as error:
                if (
                    is_final
                    and not terminated
                    and FileAttemptStorage._is_incomplete_json(error)
                ):
                    return
                raise _projection_invalid() from None
            try:
                FileAttemptStorage._validate_event_record(record)
                sequence = record["sequence"]
                if sequence <= previous_sequence:
                    raise TypeError
                previous_sequence = sequence
            except (TypeError, ValueError):
                raise _projection_invalid() from None
            if is_final and not terminated:
                raise _projection_invalid()

    @staticmethod
    def _is_incomplete_json(error: json.JSONDecodeError) -> bool:
        if error.msg == "Unterminated string starting at":
            return True
        if error.pos == len(error.doc):
            return True
        if error.msg != "Expecting value":
            return False
        suffix = error.doc[error.pos :]
        return bool(suffix) and any(
            literal.startswith(suffix) and literal != suffix
            for literal in ("true", "false", "null")
        )

    @staticmethod
    def _validate_event_record(record: object) -> None:
        if type(record) is not dict or set(record) != _EVENT_FIELDS:
            raise TypeError
        text_fields = (
            "event_id",
            "job_id",
            "event_type",
            "payload_json",
            "created_at",
        )
        if any(type(record[field]) is not str for field in text_fields):
            raise TypeError
        if type(record["sequence"]) is not int or record["sequence"] < 1:
            raise TypeError


__all__ = ["FileAttemptStorage"]

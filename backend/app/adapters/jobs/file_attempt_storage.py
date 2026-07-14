from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from uuid import uuid4

from app.core.domain.ids import sha256_digest, utc_now_millis
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.model import (
    CheckpointMetadata,
    CheckpointRecord,
    JobEvent,
)
from app.core.ports.jobs import AttemptMetadataRepositoryPort


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


def _projection_invalid() -> DomainError:
    return DomainError(
        "event_projection_invalid",
        ErrorCategory.WORKSPACE_INCOMPATIBLE,
        "Event projection contains an invalid record",
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

    def save_checkpoint(self, record: CheckpointRecord) -> CheckpointMetadata:
        if not self._content_is_valid(record.schema_id, record.payload):
            raise DomainError(
                "checkpoint_content_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Checkpoint payload does not match its registered schema",
            )

        checkpoint_id = f"cp_{uuid4().hex}"
        relative_path = Path("checkpoints") / f"{checkpoint_id}.payload"
        target = self.root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(record.payload)
                handle.flush()
                os.fsync(handle.fileno())
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
            return self._metadata_repository.record_checkpoint(metadata)
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
        if relative_path.is_absolute() or ".." in relative_path.parts:
            return False
        candidate = self.root / relative_path
        try:
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(self.root) or candidate.is_symlink():
                return False
            if (
                not resolved.is_file()
                or resolved.stat().st_size != metadata.byte_length
            ):
                return False
            payload = resolved.read_bytes()
        except OSError:
            return False
        return (
            sha256_digest(payload) == metadata.output_hash
            and self._content_is_valid(metadata.schema_id, payload)
        )

    def append_event(
        self, job_id: str, event_type: str, payload_json: str
    ) -> JobEvent:
        event = self._metadata_repository.append_event(
            job_id, event_type, payload_json
        )
        projection = self._projection_path(job_id)
        projection.parent.mkdir(parents=True, exist_ok=True)
        with projection.open("ab") as handle:
            handle.write(self._event_line(event))
            handle.flush()
            os.fsync(handle.fileno())
        return event

    def reconcile_event_projection(self, job_id: str) -> tuple[JobEvent, ...]:
        projection = self._projection_path(job_id)
        if projection.exists():
            try:
                self._validate_projection(projection.read_bytes())
            except OSError as error:
                raise _projection_invalid() from error
        events = self._metadata_repository.list_events(job_id)
        payload = b"".join(self._event_line(event) for event in events)
        projection.parent.mkdir(parents=True, exist_ok=True)
        temporary = projection.with_name(f".{projection.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, projection)
        finally:
            if temporary.exists():
                temporary.unlink()
        return events

    def _projection_path(self, job_id: str) -> Path:
        return self.root / "events" / f"{job_id}.jsonl"

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
                FileAttemptStorage._validate_event_record(record)
                sequence = record["sequence"]
                if sequence <= previous_sequence:
                    raise TypeError
                previous_sequence = sequence
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ):
                if is_final and not terminated:
                    return
                raise _projection_invalid() from None
            if is_final and not terminated:
                raise _projection_invalid()

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

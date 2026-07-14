from __future__ import annotations

import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.core.domain.ids import sha256_digest
from app.core.domain.video import JobState
from app.core.errors import DomainError, ErrorCategory


TRANSCRIPT_SCHEMA = "evidence.transcript.v1"
DRAFT_SCHEMA = "knowledge.draft.markdown.v1"
TRANSCRIPT_PAYLOAD = b'{"record_type":"transcript_header"}\n'
DRAFT_PAYLOAD = b"# Draft\n"


def _task6_api():
    model = importlib.import_module("app.core.jobs.model")
    storage_module = importlib.import_module(
        "app.adapters.jobs.file_attempt_storage"
    )
    recovery = importlib.import_module("app.core.jobs.recovery")
    return (
        model.CheckpointMetadata,
        model.CheckpointRecord,
        model.JobEvent,
        storage_module.FileAttemptStorage,
        recovery.ArtifactStepDescriptor,
        recovery.RecoveryPlanner,
    )


def _validators():
    return {
        TRANSCRIPT_SCHEMA: lambda payload: payload.startswith(
            b'{"record_type":"transcript_header"}'
        ),
        DRAFT_SCHEMA: lambda payload: payload.startswith(b"# "),
    }


def _job_attempt(
    repository: SqliteJobRepository,
    *,
    step_id: str = "normalize_transcript",
):
    job = repository.create_job(
        request_hash=sha256_digest(b"request"),
        principal="local-user",
        client_request_id=None,
    )
    repository.transition_job(job.job_id, JobState.RUNNING)
    attempt = repository.create_attempt(job.job_id, step_id)
    return job, attempt


def _record(
    repository: SqliteJobRepository,
    *,
    payload: bytes = TRANSCRIPT_PAYLOAD,
    schema_id: str = TRANSCRIPT_SCHEMA,
    input_hash: str | None = None,
    step_id: str = "normalize_transcript",
):
    _, CheckpointRecord, _, _, _, _ = _task6_api()
    job, attempt = _job_attempt(repository, step_id=step_id)
    return CheckpointRecord(
        job_id=job.job_id,
        step_id=step_id,
        attempt_id=attempt.attempt_id,
        schema_id=schema_id,
        input_hash=input_hash or sha256_digest(b"input"),
        payload=payload,
        metadata_json='{"source":"test"}',
    )


def _storage(tmp_path: Path, repository: object):
    _, _, _, FileAttemptStorage, _, _ = _task6_api()
    return FileAttemptStorage(
        tmp_path / "attempt-storage",
        repository,
        validators=_validators(),
    )


def _projection_path(storage_root: Path, job_id: str) -> Path:
    return storage_root / "events" / f"{job_id}.jsonl"


def test_task6_value_objects_are_frozen_and_checkpoint_payload_is_bytes() -> None:
    CheckpointMetadata, CheckpointRecord, JobEvent, _, _, _ = _task6_api()
    record = CheckpointRecord(
        job_id="job_1",
        step_id="normalize_transcript",
        attempt_id="att_1",
        schema_id=TRANSCRIPT_SCHEMA,
        input_hash=sha256_digest(b"input"),
        payload=TRANSCRIPT_PAYLOAD,
        metadata_json="{}",
    )
    metadata = CheckpointMetadata(
        checkpoint_id="cp_1",
        job_id=record.job_id,
        step_id=record.step_id,
        attempt_id=record.attempt_id,
        relative_path="checkpoints/job_1/att_1/cp_1.payload",
        schema_id=record.schema_id,
        input_hash=record.input_hash,
        output_hash=sha256_digest(record.payload),
        byte_length=len(record.payload),
        metadata_json=record.metadata_json,
        created_at="2026-07-15T00:00:00.000Z",
    )
    event = JobEvent(
        event_id="evt_1",
        job_id=record.job_id,
        sequence=1,
        event_type="checkpoint.saved",
        payload_json="{}",
        created_at="2026-07-15T00:00:00.000Z",
    )

    with pytest.raises(FrozenInstanceError):
        record.payload = b"changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        metadata.relative_path = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        event.sequence = 2  # type: ignore[misc]
    with pytest.raises(TypeError):
        CheckpointRecord(
            job_id="job_1",
            step_id="normalize_transcript",
            attempt_id="att_1",
            schema_id=TRANSCRIPT_SCHEMA,
            input_hash=sha256_digest(b"input"),
            payload=bytearray(b"mutable"),
            metadata_json="{}",
        )


def test_save_checkpoint_writes_unique_immutable_payload_then_metadata(
    tmp_path: Path,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "machine-root")
    storage = _storage(tmp_path, repository)
    first_record = _record(repository)

    first = storage.save_checkpoint(first_record)
    second_record = replace(first_record, payload=TRANSCRIPT_PAYLOAD + b"second\n")
    second = storage.save_checkpoint(second_record)

    first_path = storage.root / first.relative_path
    second_path = storage.root / second.relative_path
    assert first.checkpoint_id != second.checkpoint_id
    assert first.relative_path != second.relative_path
    assert first_path.read_bytes() == first_record.payload
    assert second_path.read_bytes() == second_record.payload
    assert first.output_hash == sha256_digest(first_record.payload)
    assert first.byte_length == len(first_record.payload)
    assert repository.latest_checkpoint(
        first.job_id, first.step_id
    ) == second
    assert not tuple(storage.root.rglob("*.tmp"))


def test_invalid_content_is_rejected_before_file_or_metadata_write(
    tmp_path: Path,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "machine-root")
    storage = _storage(tmp_path, repository)
    record = _record(repository, payload=b"not a transcript")

    with pytest.raises(DomainError) as caught:
        storage.save_checkpoint(record)

    assert caught.value.code == "checkpoint_content_invalid"
    assert caught.value.category is ErrorCategory.INVALID_REQUEST
    assert repository.latest_checkpoint(record.job_id, record.step_id) is None
    assert not tuple((storage.root / "checkpoints").rglob("*.payload"))


class _FailingRecordMetadataPort:
    def __init__(self, repository: SqliteJobRepository) -> None:
        self.repository = repository

    def record_checkpoint(self, metadata):
        raise DomainError(
            "checkpoint_metadata_injected_failure",
            ErrorCategory.INTERNAL,
            "Injected metadata failure",
        )

    def latest_checkpoint(self, job_id: str, step_id: str):
        return self.repository.latest_checkpoint(job_id, step_id)

    def append_event(self, job_id: str, event_type: str, payload_json: str):
        return self.repository.append_event(job_id, event_type, payload_json)

    def list_events(self, job_id: str, after_sequence: int = 0):
        return self.repository.list_events(job_id, after_sequence)


def test_metadata_failure_after_replace_leaves_ignored_orphan_payload(
    tmp_path: Path,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "machine-root")
    storage = _storage(tmp_path, _FailingRecordMetadataPort(repository))
    record = _record(repository)

    with pytest.raises(DomainError, match="checkpoint_metadata_injected_failure"):
        storage.save_checkpoint(record)

    payloads = tuple((storage.root / "checkpoints").rglob("*.payload"))
    assert len(payloads) == 1
    assert payloads[0].read_bytes() == record.payload
    assert repository.latest_checkpoint(record.job_id, record.step_id) is None


@pytest.mark.parametrize(
    "mutation",
    (
        "schema",
        "input",
        "path_absolute",
        "path_traversal",
        "length",
        "output_hash",
        "missing",
        "content",
    ),
)
def test_checkpoint_reuse_validates_all_frozen_conditions(
    tmp_path: Path,
    mutation: str,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "machine-root")
    storage = _storage(tmp_path, repository)
    record = _record(repository)
    metadata = storage.save_checkpoint(record)
    candidate = metadata
    expected_schema = metadata.schema_id
    expected_input = metadata.input_hash
    if mutation == "schema":
        expected_schema = DRAFT_SCHEMA
    elif mutation == "input":
        expected_input = sha256_digest(b"different input")
    elif mutation == "path_absolute":
        candidate = replace(metadata, relative_path=str((tmp_path / "outside").resolve()))
    elif mutation == "path_traversal":
        candidate = replace(metadata, relative_path="../outside")
    elif mutation == "length":
        candidate = replace(metadata, byte_length=metadata.byte_length + 1)
    elif mutation == "output_hash":
        candidate = replace(metadata, output_hash=sha256_digest(b"different"))
    elif mutation == "missing":
        (storage.root / metadata.relative_path).unlink()
    elif mutation == "content":
        changed = b"x" * metadata.byte_length
        (storage.root / metadata.relative_path).write_bytes(changed)
        candidate = replace(metadata, output_hash=sha256_digest(changed))

    assert not storage.validate_checkpoint(
        candidate,
        expected_schema_id=expected_schema,
        expected_input_hash=expected_input,
    )


def test_validate_checkpoint_accepts_registered_valid_payload(tmp_path: Path) -> None:
    repository = SqliteJobRepository.open(tmp_path / "machine-root")
    storage = _storage(tmp_path, repository)
    record = _record(repository)
    metadata = storage.save_checkpoint(record)

    assert storage.validate_checkpoint(
        metadata,
        expected_schema_id=record.schema_id,
        expected_input_hash=record.input_hash,
    )


def test_recovery_ignores_orphans_and_rewinds_from_first_invalid_artifact(
    tmp_path: Path,
) -> None:
    _, _, _, _, ArtifactStepDescriptor, RecoveryPlanner = _task6_api()
    repository = SqliteJobRepository.open(tmp_path / "machine-root")
    storage = _storage(tmp_path, repository)
    transcript = _record(repository)
    transcript_metadata = storage.save_checkpoint(transcript)
    draft = _record(
        repository,
        payload=DRAFT_PAYLOAD,
        schema_id=DRAFT_SCHEMA,
        input_hash=transcript_metadata.output_hash,
        step_id="generate_draft",
    )
    draft_metadata = storage.save_checkpoint(draft)
    (storage.root / draft_metadata.relative_path).write_bytes(b"changed")
    orphan = storage.root / "checkpoints" / transcript.job_id / "orphan.payload"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"not metadata-backed")
    steps = (
        ArtifactStepDescriptor(
            checkpoint_step_id="normalize_transcript",
            pending_step="normalize_transcript",
            schema_id=TRANSCRIPT_SCHEMA,
            input_hash=transcript.input_hash,
        ),
        ArtifactStepDescriptor(
            checkpoint_step_id="generate_draft",
            pending_step="generate_draft",
            schema_id=DRAFT_SCHEMA,
            input_hash=draft.input_hash,
        ),
        ArtifactStepDescriptor(
            checkpoint_step_id="assemble_bundle",
            pending_step="assemble_bundle",
            schema_id="bundle.candidate.v1",
            input_hash=sha256_digest(b"draft"),
        ),
    )

    plan = RecoveryPlanner(repository, storage).plan_remaining_steps(
        transcript.job_id, steps
    )

    assert plan.reusable_checkpoint_steps == ("normalize_transcript",)
    assert plan.pending_steps == ("generate_draft", "assemble_bundle")
    assert "normalize_transcript" not in plan.pending_steps


def test_recovery_rewinds_transcript_and_all_downstream_steps_when_it_is_corrupt(
    tmp_path: Path,
) -> None:
    _, _, _, _, ArtifactStepDescriptor, RecoveryPlanner = _task6_api()
    repository = SqliteJobRepository.open(tmp_path / "machine-root")
    storage = _storage(tmp_path, repository)
    transcript = _record(repository)
    metadata = storage.save_checkpoint(transcript)
    (storage.root / metadata.relative_path).write_bytes(b"corrupt")
    steps = (
        ArtifactStepDescriptor(
            "normalize_transcript",
            "normalize_transcript",
            TRANSCRIPT_SCHEMA,
            transcript.input_hash,
        ),
        ArtifactStepDescriptor(
            "generate_draft",
            "generate_draft",
            DRAFT_SCHEMA,
            metadata.output_hash,
        ),
    )

    plan = RecoveryPlanner(repository, storage).plan_remaining_steps(
        transcript.job_id, steps
    )

    assert plan.reusable_checkpoint_steps == ()
    assert plan.pending_steps == ("normalize_transcript", "generate_draft")


def test_event_sequences_are_strictly_monotonic_per_job_across_connections(
    tmp_path: Path,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "machine-root")
    job, _ = _job_attempt(repository)
    other_job, _ = _job_attempt(repository)

    def append(index: int):
        independent = SqliteJobRepository.open(repository.machine_root)
        return independent.append_event(
            job.job_id,
            "step.progress",
            json.dumps({"index": index}, separators=(",", ":")),
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        events = tuple(pool.map(append, range(12)))
    other = repository.append_event(other_job.job_id, "job.started", "{}")

    assert sorted(event.sequence for event in events) == list(range(1, 13))
    assert [event.sequence for event in repository.list_events(job.job_id)] == list(
        range(1, 13)
    )
    assert other.sequence == 1
    assert repository.list_events(job.job_id, after_sequence=10) == tuple(
        repository.list_events(job.job_id)[10:]
    )


def test_append_event_commits_sqlite_before_projection_and_reconcile_backfills(
    tmp_path: Path,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "machine-root")
    job, _ = _job_attempt(repository)
    storage = _storage(tmp_path, repository)
    events_path = storage.root / "events"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text("blocks-directory", encoding="utf-8")

    with pytest.raises(OSError):
        storage.append_event(job.job_id, "checkpoint.saved", '{"step":"draft"}')

    committed = repository.list_events(job.job_id)
    assert len(committed) == 1
    events_path.unlink()
    reconciled = storage.reconcile_event_projection(job.job_id)
    assert reconciled == committed
    projection = _projection_path(storage.root, job.job_id)
    assert projection.read_bytes().endswith(b"\n")
    assert len(projection.read_text(encoding="utf-8").splitlines()) == 1


def test_reconcile_uses_only_sqlite_and_discards_valid_file_only_rows(
    tmp_path: Path,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "machine-root")
    job, _ = _job_attempt(repository)
    storage = _storage(tmp_path, repository)
    committed = storage.append_event(job.job_id, "job.started", "{}")
    projection = _projection_path(storage.root, job.job_id)
    file_only = {
        "event_id": "evt_file_only",
        "job_id": job.job_id,
        "sequence": committed.sequence + 1,
        "event_type": "file.only",
        "payload_json": "{}",
        "created_at": committed.created_at,
    }
    with projection.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(file_only, sort_keys=True, separators=(",", ":")))
        handle.write("\n")

    storage.reconcile_event_projection(job.job_id)

    lines = projection.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event_id"] == committed.event_id
    assert "evt_file_only" not in projection.read_text(encoding="utf-8")


def test_reconcile_tolerates_only_truncated_unterminated_final_line(
    tmp_path: Path,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "machine-root")
    job, _ = _job_attempt(repository)
    storage = _storage(tmp_path, repository)
    committed = storage.append_event(job.job_id, "job.started", "{}")
    projection = _projection_path(storage.root, job.job_id)
    with projection.open("ab") as handle:
        handle.write(b'{"event_id":"truncated"')

    assert storage.reconcile_event_projection(job.job_id) == (committed,)
    assert projection.read_bytes().endswith(b"\n")
    assert b"truncated" not in projection.read_bytes()


def test_reconcile_rejects_non_monotonic_projection_without_overwrite(
    tmp_path: Path,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "machine-root")
    job, _ = _job_attempt(repository)
    storage = _storage(tmp_path, repository)
    committed = storage.append_event(job.job_id, "job.started", "{}")
    projection = _projection_path(storage.root, job.job_id)
    records = (
        {
            "event_id": "evt_file_2",
            "job_id": job.job_id,
            "sequence": 2,
            "event_type": "file.only",
            "payload_json": "{}",
            "created_at": committed.created_at,
        },
        {
            "event_id": committed.event_id,
            "job_id": job.job_id,
            "sequence": 1,
            "event_type": committed.event_type,
            "payload_json": committed.payload_json,
            "created_at": committed.created_at,
        },
    )
    original = b"".join(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
        for record in records
    )
    projection.write_bytes(original)

    with pytest.raises(DomainError, match="event_projection_invalid"):
        storage.reconcile_event_projection(job.job_id)

    assert projection.read_bytes() == original


@pytest.mark.parametrize(
    "projection_bytes",
    (
        b'not-json\n{"event_id":"later"}\n',
        b"not-json\n",
    ),
)
def test_reconcile_rejects_bad_middle_or_newline_terminated_final_record_without_overwrite(
    tmp_path: Path,
    projection_bytes: bytes,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "machine-root")
    job, _ = _job_attempt(repository)
    storage = _storage(tmp_path, repository)
    projection = _projection_path(storage.root, job.job_id)
    projection.parent.mkdir(parents=True, exist_ok=True)
    projection.write_bytes(projection_bytes)

    with pytest.raises(DomainError) as caught:
        storage.reconcile_event_projection(job.job_id)

    assert caught.value.code == "event_projection_invalid"
    assert caught.value.category is ErrorCategory.WORKSPACE_INCOMPATIBLE
    assert projection.read_bytes() == projection_bytes


def test_sqlite_repository_structurally_satisfies_metadata_port(tmp_path: Path) -> None:
    ports = importlib.import_module("app.core.ports.jobs")
    repository = SqliteJobRepository.open(tmp_path / "machine-root")

    assert {
        "record_checkpoint",
        "latest_checkpoint",
        "append_event",
        "list_events",
    }.issubset(set(dir(repository)))
    assert {
        name
        for name in ports.AttemptMetadataRepositoryPort.__dict__
        if not name.startswith("_")
    } == {
        "record_checkpoint",
        "latest_checkpoint",
        "append_event",
        "list_events",
    }

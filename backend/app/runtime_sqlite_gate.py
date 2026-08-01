from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import time
from contextlib import closing
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.context import BaseContext
from pathlib import Path
from typing import Mapping

from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.core.domain.ids import sha256_digest
from app.core.domain.video import JobState
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.model import JobExecutionOwner


DEFAULT_CONNECTION_COUNTS = (1, 4, 8, 16)
_GATE_SCHEMA_VERSION = 1
_UNCOMMITTED_EXIT_CODE = 23
_ACKNOWLEDGED_EXIT_CODE = 24
_REPARSE_POINT = 0x400


@dataclass(frozen=True)
class _SqliteWalGateProfile:
    connection_counts: tuple[int, ...] = DEFAULT_CONNECTION_COUNTS
    operations_per_worker: int = 8
    busy_timeout_ms: int = 5_000
    process_timeout_ms: int = 30_000
    writer_lock_hold_ms: int = 50

    def __post_init__(self) -> None:
        if not self.connection_counts or any(
            type(value) is not int or value < 1 for value in self.connection_counts
        ):
            raise ValueError("connection_counts must contain positive integers")
        for value in (
            self.operations_per_worker,
            self.busy_timeout_ms,
            self.process_timeout_ms,
            self.writer_lock_hold_ms,
        ):
            if type(value) is not int or value < 1:
                raise ValueError("SQLite WAL gate limits must be positive integers")
        if self.process_timeout_ms <= self.busy_timeout_ms:
            raise ValueError("process_timeout_ms must exceed busy_timeout_ms")
        if self.operations_per_worker < 2:
            raise ValueError("operations_per_worker must permit checkpoint overlap")


@dataclass(frozen=True)
class SqliteWalGateReport:
    sqlite_version: str
    sqlite_source_id: str
    connection_counts: tuple[int, ...]
    normal_write_matrix: tuple[Mapping[str, object], ...]
    mixed_read_write_matrix: tuple[Mapping[str, object], ...]
    checkpoint_matrix: tuple[Mapping[str, object], ...]
    forced_busy: tuple[Mapping[str, object], ...]
    portable_commit_writer_lock: Mapping[str, object]
    crash_recovery: Mapping[str, object]
    integrity: Mapping[str, object]
    sqlite_version_eligible: bool

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": _GATE_SCHEMA_VERSION,
            "scenarios_passed": True,
            "sqlite_version_eligible": self.sqlite_version_eligible,
            "parallel_job_execution_enabled": False,
            "sqlite_version": self.sqlite_version,
            "sqlite_source_id": self.sqlite_source_id,
            "connection_counts": list(self.connection_counts),
            "normal_write_matrix": [dict(item) for item in self.normal_write_matrix],
            "mixed_read_write_matrix": [
                dict(item) for item in self.mixed_read_write_matrix
            ],
            "checkpoint_matrix": [dict(item) for item in self.checkpoint_matrix],
            "forced_busy": [dict(item) for item in self.forced_busy],
            "portable_commit_writer_lock": dict(
                self.portable_commit_writer_lock
            ),
            "crash_recovery": dict(self.crash_recovery),
            "integrity": dict(self.integrity),
        }


def _raise_gate_failure(
    code: str,
    message: str,
    details: Mapping[str, object] | None = None,
) -> None:
    raise DomainError(
        code,
        ErrorCategory.WORKSPACE_INCOMPATIBLE,
        message,
        details,
    )


def _configure_worker_busy_timeout(timeout_ms: int) -> None:
    from app.adapters.jobs import sqlite_repository

    sqlite_repository._BUSY_TIMEOUT_MS = timeout_ms


def _loaded_sqlite_source_id() -> str:
    with closing(sqlite3.connect(":memory:")) as connection:
        return str(connection.execute("SELECT sqlite_source_id()").fetchone()[0])


def _gate_worker(
    machine_root: str,
    worker_index: int,
    operations: int,
    busy_timeout_ms: int,
    client_prefix: str,
    mode: str,
    seed_job_id: str,
    activity_event: object,
    checkpoint_started_event: object,
    control: Connection,
) -> None:
    worker_sqlite_version = sqlite3.sqlite_version
    worker_sqlite_source_id = _loaded_sqlite_source_id()
    control.send({"kind": "ready", "worker": worker_index})
    if not control.poll(30) or control.recv() != "start":
        control.send(
            {
                "kind": "result",
                "worker": worker_index,
                "succeeded": 0,
                "read_succeeded": 0,
                "checkpoint_succeeded": 0,
                "busy_count": 0,
                "error_codes": ["gate_start_timeout"],
                "latencies_ms": [],
                "read_latencies_ms": [],
                "checkpoint_latencies_ms": [],
                "checkpoint_results": [],
                "overlap_handshake": False,
                "sqlite_version": worker_sqlite_version,
                "sqlite_source_id": worker_sqlite_source_id,
            }
        )
        control.close()
        return
    wave_started = time.perf_counter()
    _configure_worker_busy_timeout(busy_timeout_ms)
    succeeded = 0
    read_succeeded = 0
    checkpoint_succeeded = 0
    busy_count = 0
    error_codes: list[str] = []
    latencies_ms: list[float] = []
    read_latencies_ms: list[float] = []
    checkpoint_latencies_ms: list[float] = []
    checkpoint_results: list[tuple[int, int, int]] = []
    checkpoint_connection: sqlite3.Connection | None = None
    activity_ready = True
    overlap_handshake = mode != "checkpoint"
    try:
        repository = SqliteJobRepository.open(Path(machine_root))
        if mode == "checkpoint" and worker_index == 0:
            checkpoint_connection = sqlite3.connect(
                repository.database_path,
                isolation_level=None,
                timeout=busy_timeout_ms / 1_000,
            )
            checkpoint_connection.execute(
                f"PRAGMA busy_timeout = {busy_timeout_ms}"
            )
            if not activity_event.wait(30):
                error_codes.append("checkpoint_activity_timeout")
                activity_ready = False
            else:
                checkpoint_started_event.set()
                overlap_handshake = True
        for operation in range(operations if activity_ready else 0):
            operation_kind = "write"
            if mode == "mixed" and operation % 2:
                operation_kind = "read"
            elif mode == "checkpoint" and worker_index == 0:
                operation_kind = "checkpoint"
            started = time.perf_counter()
            try:
                if operation_kind == "read":
                    repository.get_job(seed_job_id)
                    read_succeeded += 1
                elif operation_kind == "checkpoint":
                    assert checkpoint_connection is not None
                    row = checkpoint_connection.execute(
                        "PRAGMA wal_checkpoint(PASSIVE)"
                    ).fetchone()
                    checkpoint_results.append(
                        (int(row[0]), int(row[1]), int(row[2]))
                    )
                    checkpoint_succeeded += 1
                else:
                    repository.create_job(
                        request_hash=sha256_digest(
                            f"{client_prefix}:{worker_index}:{operation}"
                        ),
                        principal=f"gate-{client_prefix}-{worker_index}",
                        client_request_id=(
                            f"{client_prefix}-{worker_index}-{operation}"
                        ),
                    )
                    succeeded += 1
                    if mode == "checkpoint" and operation == 0:
                        activity_event.set()
                        if checkpoint_started_event.wait(30):
                            overlap_handshake = True
                        else:
                            error_codes.append("checkpoint_start_timeout")
                            break
            except DomainError as error:
                if error.code == "job_store_busy":
                    busy_count += 1
                else:
                    error_codes.append(error.code)
            except Exception:
                error_codes.append(
                    "checkpoint_error"
                    if operation_kind == "checkpoint"
                    else "unexpected_worker_error"
                )
            finally:
                elapsed = (time.perf_counter() - started) * 1_000
                if operation_kind == "read":
                    read_latencies_ms.append(elapsed)
                elif operation_kind == "checkpoint":
                    checkpoint_latencies_ms.append(elapsed)
                else:
                    latencies_ms.append(elapsed)
    except DomainError as error:
        if error.code == "job_store_busy":
            busy_count += 1
            latencies_ms.append((time.perf_counter() - wave_started) * 1_000)
        else:
            error_codes.append(error.code)
    except Exception:
        error_codes.append("unexpected_worker_error")
    finally:
        if checkpoint_connection is not None:
            checkpoint_connection.close()
    control.send(
        {
            "kind": "result",
            "worker": worker_index,
            "succeeded": succeeded,
            "read_succeeded": read_succeeded,
            "checkpoint_succeeded": checkpoint_succeeded,
            "busy_count": busy_count,
            "error_codes": error_codes,
            "latencies_ms": latencies_ms,
            "read_latencies_ms": read_latencies_ms,
            "checkpoint_latencies_ms": checkpoint_latencies_ms,
            "checkpoint_results": checkpoint_results,
            "overlap_handshake": overlap_handshake,
            "sqlite_version": worker_sqlite_version,
            "sqlite_source_id": worker_sqlite_source_id,
        }
    )
    control.close()


def _spawn_context() -> BaseContext:
    import multiprocessing

    return multiprocessing.get_context("spawn")


def _pipe_get(connection: Connection, deadline: float) -> Mapping[str, object]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _raise_gate_failure(
            "sqlite_wal_gate_timeout",
            "The SQLite WAL gate exceeded its bounded process timeout",
        )
    if not connection.poll(remaining):
        _raise_gate_failure(
            "sqlite_wal_gate_timeout",
            "The SQLite WAL gate exceeded its bounded process timeout",
        )
    try:
        item = connection.recv()
    except EOFError:
        _raise_gate_failure(
            "sqlite_wal_gate_worker_failed",
            "A SQLite WAL gate worker exited before returning a result",
        )
    if not isinstance(item, Mapping):
        _raise_gate_failure(
            "sqlite_wal_gate_protocol_invalid",
            "A SQLite WAL gate worker returned an invalid result",
        )
    return item


def _run_worker_wave(
    machine_root: Path,
    *,
    workers: int,
    operations_per_worker: int,
    busy_timeout_ms: int,
    process_timeout_ms: int,
    client_prefix: str,
    mode: str = "write",
    seed_job_id: str = "",
) -> tuple[Mapping[str, object], ...]:
    context = _spawn_context()
    activity_event = context.Event()
    checkpoint_started_event = context.Event()
    if mode == "checkpoint" and workers == 1:
        activity_event.set()
    connections: list[tuple[Connection, Connection]] = [
        context.Pipe(duplex=True) for _ in range(workers)
    ]
    processes = [
        context.Process(
            target=_gate_worker,
            args=(
                str(machine_root),
                worker,
                operations_per_worker,
                busy_timeout_ms,
                client_prefix,
                mode,
                seed_job_id,
                activity_event,
                checkpoint_started_event,
                connections[worker][1],
            ),
        )
        for worker in range(workers)
    ]
    deadline = time.monotonic() + process_timeout_ms / 1_000
    try:
        for process in processes:
            process.start()
        for _, child_connection in connections:
            child_connection.close()
        for parent_connection, _ in connections:
            item = _pipe_get(parent_connection, deadline)
            if item.get("kind") != "ready":
                _raise_gate_failure(
                    "sqlite_wal_gate_protocol_invalid",
                    "A SQLite WAL gate worker did not become ready",
                )
        for parent_connection, _ in connections:
            parent_connection.send("start")
        results: list[Mapping[str, object]] = []
        for parent_connection, _ in connections:
            item = _pipe_get(parent_connection, deadline)
            if item.get("kind") != "result":
                _raise_gate_failure(
                    "sqlite_wal_gate_protocol_invalid",
                    "A SQLite WAL gate worker returned an invalid phase",
                )
            results.append(item)
        parent_source_id = _loaded_sqlite_source_id()
        if any(
            item.get("sqlite_version") != sqlite3.sqlite_version
            or item.get("sqlite_source_id") != parent_source_id
            for item in results
        ):
            _raise_gate_failure(
                "sqlite_wal_gate_identity_mismatch",
                "A SQLite WAL gate worker loaded a different SQLite build",
            )
        for process in processes:
            remaining = max(deadline - time.monotonic(), 0.0)
            process.join(remaining)
            if process.is_alive():
                process.kill()
                process.join(5)
                _raise_gate_failure(
                    "sqlite_wal_gate_timeout",
                    "The SQLite WAL gate exceeded its bounded process timeout",
                )
            if process.exitcode != 0:
                _raise_gate_failure(
                    "sqlite_wal_gate_worker_failed",
                    "A SQLite WAL gate worker exited unexpectedly",
                )
        return tuple(sorted(results, key=lambda item: int(item["worker"])))
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join(5)
        for parent_connection, child_connection in connections:
            parent_connection.close()
            child_connection.close()


def _latency_summary(values: list[float]) -> dict[str, object]:
    if not values:
        return {"count": 0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
    ordered = sorted(values)

    def percentile(percent: int) -> float:
        index = max(0, (len(ordered) * percent + 99) // 100 - 1)
        return round(ordered[index], 3)

    return {
        "count": len(ordered),
        "p50_ms": percentile(50),
        "p95_ms": percentile(95),
        "max_ms": round(ordered[-1], 3),
    }


def _database_invariants(machine_root: Path) -> dict[str, object]:
    repository = SqliteJobRepository.open(machine_root)
    with closing(repository._connect()) as connection:
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    return {
        "integrity_check": integrity,
        "foreign_key_violations": len(foreign_keys),
        "checkpoint_busy": int(checkpoint[0]),
        "wal_frames_remaining": max(int(checkpoint[1]) - int(checkpoint[2]), 0),
        "journal_mode": str(journal_mode),
        "user_version": int(user_version),
    }


def _database_invariants_pass(invariants: Mapping[str, object]) -> bool:
    return invariants == {
        "integrity_check": "ok",
        "foreign_key_violations": 0,
        "checkpoint_busy": 0,
        "wal_frames_remaining": 0,
        "journal_mode": "wal",
        "user_version": 4,
    }


def _normal_write_matrix(
    root: Path,
    profile: _SqliteWalGateProfile,
) -> tuple[Mapping[str, object], ...]:
    matrix: list[Mapping[str, object]] = []
    for connections in profile.connection_counts:
        machine_root = root / f"normal-{connections}"
        SqliteJobRepository.open(machine_root)
        started = time.perf_counter()
        results = _run_worker_wave(
            machine_root,
            workers=connections,
            operations_per_worker=profile.operations_per_worker,
            busy_timeout_ms=profile.busy_timeout_ms,
            process_timeout_ms=profile.process_timeout_ms,
            client_prefix=f"normal-{connections}",
        )
        latencies = [
            float(value)
            for result in results
            for value in result["latencies_ms"]
        ]
        succeeded = sum(int(result["succeeded"]) for result in results)
        busy_count = sum(int(result["busy_count"]) for result in results)
        error_codes = sorted(
            str(code) for result in results for code in result["error_codes"]
        )
        expected = connections * profile.operations_per_worker
        invariants = _database_invariants(machine_root)
        with closing(SqliteJobRepository.open(machine_root)._connect()) as connection:
            stored = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        if (
            succeeded != expected
            or stored != expected
            or busy_count
            or error_codes
            or not _database_invariants_pass(invariants)
        ):
            _raise_gate_failure(
                "sqlite_wal_gate_normal_write_failed",
                "SQLite did not pass the normal multi-process write gate",
                {
                    "connections": connections,
                    "expected": expected,
                    "succeeded": succeeded,
                    "stored": stored,
                    "busy_count": busy_count,
                    "error_codes": error_codes,
                    **invariants,
                },
            )
        matrix.append(
            {
                "connections": connections,
                "attempted": expected,
                "succeeded": succeeded,
                "busy_count": busy_count,
                "error_codes": error_codes,
                "duration_ms": round((time.perf_counter() - started) * 1_000, 3),
                "latency": _latency_summary(latencies),
                "checkpoint_busy": invariants["checkpoint_busy"],
                "wal_frames_remaining": invariants["wal_frames_remaining"],
            }
        )
    return tuple(matrix)


def _mixed_read_write_matrix(
    root: Path,
    profile: _SqliteWalGateProfile,
) -> tuple[Mapping[str, object], ...]:
    matrix: list[Mapping[str, object]] = []
    for connections in profile.connection_counts:
        machine_root = root / f"mixed-{connections}"
        repository = SqliteJobRepository.open(machine_root)
        seed = repository.create_job(
            request_hash=sha256_digest(f"mixed-seed-{connections}"),
            principal="gate-mixed-seed",
            client_request_id=f"mixed-seed-{connections}",
        )
        started = time.perf_counter()
        results = _run_worker_wave(
            machine_root,
            workers=connections,
            operations_per_worker=profile.operations_per_worker,
            busy_timeout_ms=profile.busy_timeout_ms,
            process_timeout_ms=profile.process_timeout_ms,
            client_prefix=f"mixed-{connections}",
            mode="mixed",
            seed_job_id=seed.job_id,
        )
        expected_writes = connections * (
            (profile.operations_per_worker + 1) // 2
        )
        expected_reads = connections * (profile.operations_per_worker // 2)
        write_succeeded = sum(int(item["succeeded"]) for item in results)
        read_succeeded = sum(int(item["read_succeeded"]) for item in results)
        busy_count = sum(int(item["busy_count"]) for item in results)
        error_codes = sorted(
            str(code) for item in results for code in item["error_codes"]
        )
        write_latencies = [
            float(value)
            for item in results
            for value in item["latencies_ms"]
        ]
        read_latencies = [
            float(value)
            for item in results
            for value in item["read_latencies_ms"]
        ]
        invariants = _database_invariants(machine_root)
        with closing(repository._connect()) as connection:
            stored = int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        if (
            write_succeeded != expected_writes
            or read_succeeded != expected_reads
            or stored != expected_writes + 1
            or busy_count
            or error_codes
            or not _database_invariants_pass(invariants)
        ):
            _raise_gate_failure(
                "sqlite_wal_gate_mixed_read_write_failed",
                "SQLite did not pass the mixed read and write gate",
                {
                    "connections": connections,
                    "expected_writes": expected_writes,
                    "write_succeeded": write_succeeded,
                    "expected_reads": expected_reads,
                    "read_succeeded": read_succeeded,
                    "stored": stored,
                    "busy_count": busy_count,
                    "error_codes": error_codes,
                    **invariants,
                },
            )
        matrix.append(
            {
                "connections": connections,
                "write_succeeded": write_succeeded,
                "read_succeeded": read_succeeded,
                "busy_count": busy_count,
                "error_codes": error_codes,
                "duration_ms": round((time.perf_counter() - started) * 1_000, 3),
                "write_latency": _latency_summary(write_latencies),
                "read_latency": _latency_summary(read_latencies),
            }
        )
    return tuple(matrix)


def _checkpoint_matrix(
    root: Path,
    profile: _SqliteWalGateProfile,
) -> tuple[Mapping[str, object], ...]:
    matrix: list[Mapping[str, object]] = []
    for connections in profile.connection_counts:
        machine_root = root / f"checkpoint-{connections}"
        repository = SqliteJobRepository.open(machine_root)
        for operation in range(profile.operations_per_worker):
            repository.create_job(
                request_hash=sha256_digest(
                    f"checkpoint-seed-{connections}-{operation}"
                ),
                principal="gate-checkpoint-seed",
                client_request_id=f"checkpoint-seed-{connections}-{operation}",
            )
        started = time.perf_counter()
        results = _run_worker_wave(
            machine_root,
            workers=connections,
            operations_per_worker=profile.operations_per_worker,
            busy_timeout_ms=profile.busy_timeout_ms,
            process_timeout_ms=profile.process_timeout_ms,
            client_prefix=f"checkpoint-{connections}",
            mode="checkpoint",
        )
        expected_writes = (connections - 1) * profile.operations_per_worker
        expected_checkpoints = profile.operations_per_worker
        write_succeeded = sum(int(item["succeeded"]) for item in results)
        checkpoint_succeeded = sum(
            int(item["checkpoint_succeeded"]) for item in results
        )
        busy_count = sum(int(item["busy_count"]) for item in results)
        error_codes = sorted(
            str(code) for item in results for code in item["error_codes"]
        )
        checkpoint_latencies = [
            float(value)
            for item in results
            for value in item["checkpoint_latencies_ms"]
        ]
        checkpoint_results = [
            tuple(int(value) for value in row)
            for item in results
            for row in item["checkpoint_results"]
        ]
        max_log_frames = max((row[1] for row in checkpoint_results), default=0)
        max_checkpointed_frames = max(
            (row[2] for row in checkpoint_results), default=0
        )
        overlap_handshake = all(
            bool(item["overlap_handshake"]) for item in results
        )
        invariants = _database_invariants(machine_root)
        with closing(repository._connect()) as connection:
            stored = int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        if (
            write_succeeded != expected_writes
            or checkpoint_succeeded != expected_checkpoints
            or stored != profile.operations_per_worker + expected_writes
            or busy_count
            or error_codes
            or not overlap_handshake
            or not _database_invariants_pass(invariants)
        ):
            _raise_gate_failure(
                "sqlite_wal_gate_checkpoint_failed",
                "SQLite did not pass the concurrent write and checkpoint gate",
                {
                    "connections": connections,
                    "expected_writes": expected_writes,
                    "write_succeeded": write_succeeded,
                    "expected_checkpoints": expected_checkpoints,
                    "checkpoint_succeeded": checkpoint_succeeded,
                    "stored": stored,
                    "busy_count": busy_count,
                    "error_codes": error_codes,
                    "overlap_handshake": overlap_handshake,
                    "max_log_frames": max_log_frames,
                    "max_checkpointed_frames": max_checkpointed_frames,
                    **invariants,
                },
            )
        matrix.append(
            {
                "connections": connections,
                "write_succeeded": write_succeeded,
                "checkpoint_succeeded": checkpoint_succeeded,
                "overlap_handshake": overlap_handshake,
                "checkpoint_busy_results": sum(
                    1 for row in checkpoint_results if row[0] != 0
                ),
                "max_log_frames": max_log_frames,
                "max_checkpointed_frames": max_checkpointed_frames,
                "busy_count": busy_count,
                "error_codes": error_codes,
                "duration_ms": round((time.perf_counter() - started) * 1_000, 3),
                "checkpoint_latency": _latency_summary(checkpoint_latencies),
                "final_checkpoint_busy": invariants["checkpoint_busy"],
                "wal_frames_remaining": invariants["wal_frames_remaining"],
            }
        )
    return tuple(matrix)


def _forced_busy_matrix(
    root: Path,
    profile: _SqliteWalGateProfile,
) -> tuple[Mapping[str, object], ...]:
    matrix: list[Mapping[str, object]] = []
    for connections in profile.connection_counts:
        if connections == 1:
            matrix.append(
                {
                    "connections": 1,
                    "contenders": 0,
                    "busy_count": 0,
                    "retry_succeeded": 0,
                    "latency": _latency_summary([]),
                }
            )
            continue
        machine_root = root / f"forced-{connections}"
        repository = SqliteJobRepository.open(machine_root)
        holder = sqlite3.connect(repository.database_path, isolation_level=None)
        holder.execute("PRAGMA journal_mode = WAL")
        holder.execute("BEGIN IMMEDIATE")
        try:
            blocked = _run_worker_wave(
                machine_root,
                workers=connections - 1,
                operations_per_worker=1,
                busy_timeout_ms=profile.busy_timeout_ms,
                process_timeout_ms=profile.process_timeout_ms,
                client_prefix=f"forced-{connections}",
            )
        finally:
            holder.rollback()
            holder.close()
        busy_count = sum(int(result["busy_count"]) for result in blocked)
        blocked_errors = [
            str(code) for result in blocked for code in result["error_codes"]
        ]
        blocked_succeeded = sum(int(result["succeeded"]) for result in blocked)
        latencies = [
            float(value)
            for result in blocked
            for value in result["latencies_ms"]
        ]
        retried = _run_worker_wave(
            machine_root,
            workers=connections - 1,
            operations_per_worker=1,
            busy_timeout_ms=profile.busy_timeout_ms,
            process_timeout_ms=profile.process_timeout_ms,
            client_prefix=f"forced-{connections}",
        )
        retry_succeeded = sum(int(result["succeeded"]) for result in retried)
        retry_busy = sum(int(result["busy_count"]) for result in retried)
        retry_errors = [
            str(code) for result in retried for code in result["error_codes"]
        ]
        expected = connections - 1
        if (
            busy_count != expected
            or blocked_succeeded != 0
            or blocked_errors
            or retry_succeeded != expected
            or retry_busy
            or retry_errors
        ):
            _raise_gate_failure(
                "sqlite_wal_gate_busy_recovery_failed",
                "SQLite did not pass the forced busy and retry gate",
            )
        matrix.append(
            {
                "connections": connections,
                "contenders": expected,
                "busy_count": busy_count,
                "retry_succeeded": retry_succeeded,
                "latency": _latency_summary(latencies),
            }
        )
    return tuple(matrix)


def _portable_commit_writer_lock(
    root: Path,
    profile: _SqliteWalGateProfile,
) -> Mapping[str, object]:
    machine_root = root / "portable-commit-writer-lock"
    repository = SqliteJobRepository.open(machine_root)
    job = repository.create_job(
        request_hash=sha256_digest("portable-commit-writer-lock"),
        principal="gate-portable-commit",
        client_request_id="portable-commit-writer-lock",
    )
    repository.transition_job(job.job_id, JobState.RUNNING)
    authority = repository.claim_job(
        job.job_id,
        "sqlite-wal-gate:portable-commit",
        ttl_seconds=300,
    ).authority
    attempt = repository.create_attempt(
        job.job_id,
        "portable-commit",
        authority=authority,
    )
    attempt = repository.start_attempt(attempt.attempt_id, authority)

    transaction_started = time.perf_counter()
    with repository.commit_guard(job.job_id, attempt.attempt_id, authority):
        lock_acquired = time.perf_counter()
        time.sleep(profile.writer_lock_hold_ms / 1_000)
    transaction_finished = time.perf_counter()
    final_state = repository.get_job(job.job_id).state.value
    if final_state != JobState.SUCCEEDED.value:
        _raise_gate_failure(
            "sqlite_wal_gate_writer_lock_failed",
            "SQLite did not pass the portable commit writer-lock gate",
        )
    return {
        "requested_callback_ms": profile.writer_lock_hold_ms,
        "writer_lock_held_ms": round(
            (transaction_finished - lock_acquired) * 1_000,
            3,
        ),
        "transaction_ms": round(
            (transaction_finished - transaction_started) * 1_000,
            3,
        ),
        "job_state": final_state,
    }


def _crash_uncommitted_worker(machine_root: str, busy_timeout_ms: int) -> None:
    _configure_worker_busy_timeout(busy_timeout_ms)
    repository = SqliteJobRepository.open(Path(machine_root))
    try:
        with repository._transaction(immediate=True) as connection:
            repository._create_job(
                connection,
                request_hash=sha256_digest("gate-uncommitted"),
                request_json=None,
                principal="gate-crash",
                client_request_id="uncommitted",
                retry_of_job_id=None,
                execution_owner=JobExecutionOwner.FOREGROUND,
            )
            os._exit(_UNCOMMITTED_EXIT_CODE)
    except BaseException:
        os._exit(70)


def _crash_acknowledged_worker(
    machine_root: str,
    busy_timeout_ms: int,
    acknowledgement_path: str,
) -> None:
    _configure_worker_busy_timeout(busy_timeout_ms)
    try:
        repository = SqliteJobRepository.open(Path(machine_root))
        job = repository.create_job(
            request_hash=sha256_digest("gate-acknowledged"),
            principal="gate-crash",
            client_request_id="acknowledged",
            initial_events=(("gate.acknowledged", "{}"),),
        )
        with Path(acknowledgement_path).open("x", encoding="utf-8") as output:
            output.write(job.job_id)
            output.flush()
            os.fsync(output.fileno())
        os._exit(_ACKNOWLEDGED_EXIT_CODE)
    except BaseException:
        os._exit(70)


def _run_crash_recovery(
    root: Path,
    profile: _SqliteWalGateProfile,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    machine_root = root / "crash-recovery"
    SqliteJobRepository.open(machine_root)
    context = _spawn_context()
    uncommitted = context.Process(
        target=_crash_uncommitted_worker,
        args=(str(machine_root), profile.busy_timeout_ms),
    )
    uncommitted.start()
    uncommitted.join(profile.process_timeout_ms / 1_000)
    if uncommitted.is_alive():
        uncommitted.kill()
        uncommitted.join(5)
        _raise_gate_failure(
            "sqlite_wal_gate_timeout",
            "The SQLite crash gate exceeded its bounded process timeout",
        )
    repository = SqliteJobRepository.open(machine_root)
    with closing(repository._connect()) as connection:
        uncommitted_count = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE client_request_id = 'uncommitted'"
        ).fetchone()[0]

    acknowledgement = root / "acknowledged-job-id.txt"
    acknowledged = context.Process(
        target=_crash_acknowledged_worker,
        args=(
            str(machine_root),
            profile.busy_timeout_ms,
            str(acknowledgement),
        ),
    )
    acknowledged.start()
    acknowledged.join(profile.process_timeout_ms / 1_000)
    if acknowledged.is_alive():
        acknowledged.kill()
        acknowledged.join(5)
        _raise_gate_failure(
            "sqlite_wal_gate_timeout",
            "The SQLite crash gate exceeded its bounded process timeout",
        )
    if acknowledgement.is_file():
        acknowledged_job_id = acknowledgement.read_text(encoding="utf-8")
    else:
        acknowledged_job_id = ""
    repository = SqliteJobRepository.open(machine_root)
    with closing(repository._connect()) as connection:
        acknowledged_count = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE client_request_id = 'acknowledged'"
        ).fetchone()[0]
    acknowledged_present = False
    acknowledged_binding_present = False
    acknowledged_event_present = False
    if acknowledged_job_id:
        try:
            acknowledged_present = (
                repository.get_job(acknowledged_job_id).job_id
                == acknowledged_job_id
            )
            acknowledged_binding_present = bool(
                repository.get_job_execution_binding(acknowledged_job_id)
            )
            acknowledged_event_present = (
                len(repository.list_events(acknowledged_job_id)) == 1
            )
        except DomainError:
            acknowledged_present = False
    repository.create_job(
        request_hash=sha256_digest("gate-post-crash"),
        principal="gate-crash",
        client_request_id="post-crash",
    )
    crash_report = {
        "uncommitted_exit_code": uncommitted.exitcode,
        "uncommitted_row_absent": uncommitted_count == 0,
        "acknowledged_exit_code": acknowledged.exitcode,
        "acknowledged_row_present": acknowledged_count == 1
        and acknowledged_present,
        "acknowledged_binding_present": acknowledged_binding_present,
        "acknowledged_event_present": acknowledged_event_present,
        "post_crash_write_succeeded": True,
    }
    if crash_report != {
        "uncommitted_exit_code": _UNCOMMITTED_EXIT_CODE,
        "uncommitted_row_absent": True,
        "acknowledged_exit_code": _ACKNOWLEDGED_EXIT_CODE,
        "acknowledged_row_present": True,
        "acknowledged_binding_present": True,
        "acknowledged_event_present": True,
        "post_crash_write_succeeded": True,
    }:
        _raise_gate_failure(
            "sqlite_wal_gate_crash_recovery_failed",
            "SQLite did not pass the crash and reopen gate",
        )
    invariants = _database_invariants(machine_root)
    if not _database_invariants_pass(invariants):
        _raise_gate_failure(
            "sqlite_wal_gate_integrity_failed",
            "SQLite did not pass the post-crash integrity gate",
        )
    return crash_report, invariants


def _validate_gate_root(root: Path) -> Path:
    candidate = Path(root).expanduser().absolute()
    try:
        metadata = candidate.lstat()
    except OSError as error:
        raise DomainError(
            "sqlite_wal_gate_root_invalid",
            ErrorCategory.INVALID_REQUEST,
            "The SQLite WAL gate root must be an existing ordinary directory",
        ) from error
    attributes = getattr(metadata, "st_file_attributes", 0)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or candidate.is_symlink()
        or attributes & _REPARSE_POINT
    ):
        raise DomainError(
            "sqlite_wal_gate_root_invalid",
            ErrorCategory.INVALID_REQUEST,
            "The SQLite WAL gate root must be an existing ordinary directory",
        )
    return candidate.resolve(strict=True)


def _run_sqlite_wal_gate(
    root: Path,
    profile: _SqliteWalGateProfile,
) -> SqliteWalGateReport:
    validated_root = _validate_gate_root(root)
    from app.runtime_info import _sqlite_parallel_jobs_supported

    if not _sqlite_parallel_jobs_supported(sqlite3.sqlite_version):
        _raise_gate_failure(
            "sqlite_wal_gate_version_ineligible",
            "The loaded SQLite version is not admitted for the WAL gate",
            {"sqlite_version": sqlite3.sqlite_version},
        )
    with tempfile.TemporaryDirectory(
        prefix=".alltonote-sqlite-wal-gate-",
        dir=validated_root,
    ) as temporary:
        gate_root = Path(temporary)
        normal = _normal_write_matrix(gate_root, profile)
        mixed = _mixed_read_write_matrix(gate_root, profile)
        checkpoints = _checkpoint_matrix(gate_root, profile)
        forced = _forced_busy_matrix(gate_root, profile)
        writer_lock = _portable_commit_writer_lock(gate_root, profile)
        crash, integrity = _run_crash_recovery(gate_root, profile)
        return SqliteWalGateReport(
            sqlite_version=sqlite3.sqlite_version,
            sqlite_source_id=_loaded_sqlite_source_id(),
            connection_counts=profile.connection_counts,
            normal_write_matrix=normal,
            mixed_read_write_matrix=mixed,
            checkpoint_matrix=checkpoints,
            forced_busy=forced,
            portable_commit_writer_lock=writer_lock,
            crash_recovery=crash,
            integrity=integrity,
            sqlite_version_eligible=True,
        )


def run_sqlite_wal_gate(root: Path) -> dict[str, object]:
    return _run_sqlite_wal_gate(root, _SqliteWalGateProfile()).to_mapping()


__all__ = [
    "DEFAULT_CONNECTION_COUNTS",
    "SqliteWalGateReport",
    "run_sqlite_wal_gate",
]

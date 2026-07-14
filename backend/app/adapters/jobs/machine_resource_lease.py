from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.resource_lease import (
    ResourceLease,
    ResourceOwner,
    validate_lease_ttl,
)


_BUSY_TIMEOUT_MS = 5_000


class MachineResourceLeaseStore:
    def __init__(
        self,
        machine_root: Path,
        *,
        clock: Callable[[], int],
    ) -> None:
        self.machine_root = machine_root
        self.database_path = machine_root / "leases.sqlite"
        self._clock = clock

    @classmethod
    def open(
        cls,
        machine_root: Path,
        *,
        clock: Callable[[], int] | None = None,
    ) -> MachineResourceLeaseStore:
        resolved_root = Path(machine_root).resolve()
        resolved_root.mkdir(parents=True, exist_ok=True)
        store = cls(
            resolved_root,
            clock=clock or (lambda: time.time_ns() // 1_000_000),
        )
        with store._transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS resource_leases (
                    resource_name TEXT PRIMARY KEY,
                    workspace_identity TEXT NOT NULL,
                    process_instance_id TEXT NOT NULL,
                    process_id INTEGER,
                    fencing_token INTEGER NOT NULL,
                    expires_at_ms INTEGER NOT NULL
                )
                """
            )
        return store

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.database_path,
            timeout=_BUSY_TIMEOUT_MS / 1_000,
            isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def acquire(
        self,
        resource_name: str,
        owner: ResourceOwner,
        *,
        ttl_seconds: int,
    ) -> ResourceLease:
        self._validate_resource_name(resource_name)
        validate_lease_ttl(ttl_seconds)
        now_ms = self._clock()
        expires_at_ms = now_ms + ttl_seconds * 1_000
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM resource_leases WHERE resource_name = ?",
                (resource_name,),
            ).fetchone()
            if row is None:
                fencing_token = 1
                connection.execute(
                    """
                    INSERT INTO resource_leases (
                        resource_name, workspace_identity, process_instance_id,
                        process_id, fencing_token, expires_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resource_name,
                        owner.workspace_identity,
                        owner.process_instance_id,
                        owner.process_id,
                        fencing_token,
                        expires_at_ms,
                    ),
                )
            elif row["expires_at_ms"] > now_ms:
                if not self._row_owned_by(row, owner):
                    raise DomainError(
                        "resource_busy",
                        ErrorCategory.CONFLICT,
                        "Machine resource is held by another process instance",
                    )
                fencing_token = row["fencing_token"]
                connection.execute(
                    """
                    UPDATE resource_leases SET expires_at_ms = ?
                    WHERE resource_name = ?
                    """,
                    (expires_at_ms, resource_name),
                )
            else:
                fencing_token = row["fencing_token"] + 1
                connection.execute(
                    """
                    UPDATE resource_leases
                    SET workspace_identity = ?, process_instance_id = ?,
                        process_id = ?, fencing_token = ?, expires_at_ms = ?
                    WHERE resource_name = ?
                    """,
                    (
                        owner.workspace_identity,
                        owner.process_instance_id,
                        owner.process_id,
                        fencing_token,
                        expires_at_ms,
                        resource_name,
                    ),
                )
        return self._lease(resource_name, owner, fencing_token, expires_at_ms)

    def _heartbeat(
        self,
        resource_name: str,
        owner: ResourceOwner,
        fencing_token: int,
        ttl_seconds: int,
    ) -> ResourceLease:
        validate_lease_ttl(ttl_seconds)
        now_ms = self._clock()
        expires_at_ms = now_ms + ttl_seconds * 1_000
        with self._transaction() as connection:
            updated = connection.execute(
                """
                UPDATE resource_leases SET expires_at_ms = ?
                WHERE resource_name = ? AND workspace_identity = ?
                  AND process_instance_id = ? AND fencing_token = ?
                  AND expires_at_ms > ?
                """,
                (
                    expires_at_ms,
                    resource_name,
                    owner.workspace_identity,
                    owner.process_instance_id,
                    fencing_token,
                    now_ms,
                ),
            )
            if updated.rowcount != 1:
                raise DomainError(
                    "resource_lease_lost",
                    ErrorCategory.CONFLICT,
                    "Machine resource lease is expired or fenced",
                )
        return self._lease(resource_name, owner, fencing_token, expires_at_ms)

    def _release(
        self,
        resource_name: str,
        owner: ResourceOwner,
        fencing_token: int,
    ) -> bool:
        with self._transaction() as connection:
            updated = connection.execute(
                """
                UPDATE resource_leases SET expires_at_ms = 0
                WHERE resource_name = ? AND workspace_identity = ?
                  AND process_instance_id = ? AND fencing_token = ?
                """,
                (
                    resource_name,
                    owner.workspace_identity,
                    owner.process_instance_id,
                    fencing_token,
                ),
            )
            return updated.rowcount == 1

    def _lease(
        self,
        resource_name: str,
        owner: ResourceOwner,
        fencing_token: int,
        expires_at_ms: int,
    ) -> ResourceLease:
        return ResourceLease(
            resource_name=resource_name,
            owner=owner,
            fencing_token=fencing_token,
            expires_at_ms=expires_at_ms,
            _heartbeat_callback=lambda ttl: self._heartbeat(
                resource_name, owner, fencing_token, ttl
            ),
            _release_callback=lambda: self._release(
                resource_name, owner, fencing_token
            ),
        )

    @staticmethod
    def _row_owned_by(row: sqlite3.Row, owner: ResourceOwner) -> bool:
        return (
            row["workspace_identity"] == owner.workspace_identity
            and row["process_instance_id"] == owner.process_instance_id
        )

    @staticmethod
    def _validate_resource_name(resource_name: str) -> None:
        if type(resource_name) is not str or not resource_name.strip():
            raise DomainError(
                "resource_name_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Resource name must be non-empty text",
            )


__all__ = ["MachineResourceLeaseStore"]

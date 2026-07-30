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
_SCHEMA_VERSION = 1
_SCHEMA_STATEMENT = """
CREATE TABLE resource_leases (
    resource_name TEXT PRIMARY KEY CHECK(length(resource_name) > 0),
    workspace_identity TEXT NOT NULL CHECK(length(workspace_identity) > 0),
    process_instance_id TEXT NOT NULL CHECK(length(process_instance_id) > 0),
    process_id INTEGER CHECK(process_id IS NULL OR process_id > 0),
    fencing_token INTEGER NOT NULL CHECK(fencing_token > 0),
    expires_at_ms INTEGER NOT NULL CHECK(expires_at_ms >= 0)
)
"""


def _normalize_schema_sql(sql: str) -> str:
    return " ".join(sql.split())


def _application_schema(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str], str]:
    rows = connection.execute(
        """
        SELECT type, name, sql FROM sqlite_master
        WHERE type IN ('table', 'index', 'trigger', 'view')
          AND name NOT GLOB 'sqlite_*'
        """
    ).fetchall()
    return {(row[0], row[1]): _normalize_schema_sql(row[2]) for row in rows}


def _expected_schema() -> dict[tuple[str, str], str]:
    with sqlite3.connect(":memory:") as connection:
        connection.execute(_SCHEMA_STATEMENT)
        return _application_schema(connection)


def _raise_schema_invalid(error: BaseException | None = None) -> None:
    raise DomainError(
        "machine_lease_schema_invalid",
        ErrorCategory.WORKSPACE_INCOMPATIBLE,
        "Machine resource lease schema does not match version 1",
    ) from error


def _raise_store_invalid(error: BaseException) -> None:
    raise DomainError(
        "machine_lease_store_invalid",
        ErrorCategory.WORKSPACE_INCOMPATIBLE,
        "Machine resource lease store is unavailable",
    ) from error


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
        store._initialize_schema()
        return store

    def _initialize_schema(self) -> None:
        with self._transaction(schema_operation=True) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, _SCHEMA_VERSION):
                raise DomainError(
                    "machine_lease_schema_unsupported",
                    ErrorCategory.WORKSPACE_INCOMPATIBLE,
                    "Machine resource lease schema version is not supported",
                )
            actual_schema = _application_schema(connection)
            if version == 0:
                if actual_schema:
                    _raise_schema_invalid()
                connection.execute(_SCHEMA_STATEMENT)
                connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                actual_schema = _application_schema(connection)
            if actual_schema != _expected_schema():
                _raise_schema_invalid()

    @contextmanager
    def _transaction(
        self,
        *,
        schema_operation: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.database_path,
                timeout=_BUSY_TIMEOUT_MS / 1_000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            if not schema_operation:
                connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except DomainError:
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.DatabaseError:
                    pass
            raise
        except sqlite3.DatabaseError as error:
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.DatabaseError:
                    pass
            if schema_operation:
                _raise_schema_invalid(error)
            _raise_store_invalid(error)
        except BaseException:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
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
                raise DomainError(
                    "resource_busy",
                    ErrorCategory.CONFLICT,
                    "Machine resource is already leased",
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
    def _validate_resource_name(resource_name: str) -> None:
        if type(resource_name) is not str or not resource_name.strip():
            raise DomainError(
                "resource_name_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Resource name must be non-empty text",
            )


__all__ = ["MachineResourceLeaseStore"]

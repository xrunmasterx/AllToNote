from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.cli.main import main
from app.core.errors import DomainError, ErrorCategory
from app.runtime_sqlite_gate import (
    DEFAULT_CONNECTION_COUNTS,
    _SqliteWalGateProfile,
    _run_sqlite_wal_gate,
)


def test_default_gate_matrix_is_exactly_one_four_eight_sixteen() -> None:
    assert DEFAULT_CONNECTION_COUNTS == (1, 4, 8, 16)


def test_spawn_gate_proves_busy_recovery_and_crash_durability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.runtime_info._sqlite_parallel_jobs_supported",
        lambda version: True,
    )
    try:
        report = _run_sqlite_wal_gate(
            tmp_path,
            _SqliteWalGateProfile(
                connection_counts=(1, 4),
                operations_per_worker=2,
                busy_timeout_ms=5_000,
                process_timeout_ms=30_000,
            ),
        )
    except DomainError as error:
        pytest.fail(f"{error.code}: {dict(error.details)!r}")
    data = report.to_mapping()

    assert data["schema_version"] == 1
    assert data["scenarios_passed"] is True
    assert data["sqlite_version_eligible"] is True
    assert data["parallel_job_execution_enabled"] is False
    assert data["connection_counts"] == [1, 4]
    assert [item["connections"] for item in data["normal_write_matrix"]] == [
        1,
        4,
    ]
    assert all(
        item["busy_count"] == 0 and item["error_codes"] == []
        for item in data["normal_write_matrix"]
    )
    assert [
        item["connections"] for item in data["mixed_read_write_matrix"]
    ] == [1, 4]
    assert all(
        item["write_succeeded"] > 0
        and item["read_succeeded"] > 0
        and item["busy_count"] == 0
        and item["error_codes"] == []
        for item in data["mixed_read_write_matrix"]
    )
    assert [item["connections"] for item in data["checkpoint_matrix"]] == [
        1,
        4,
    ]
    assert all(
        item["checkpoint_succeeded"] == 2
        and item["overlap_handshake"] is True
        and item["busy_count"] == 0
        and item["error_codes"] == []
        and item["final_checkpoint_busy"] == 0
        and item["wal_frames_remaining"] == 0
        for item in data["checkpoint_matrix"]
    )
    assert data["forced_busy"][0]["connections"] == 1
    assert data["forced_busy"][0]["contenders"] == 0
    assert data["forced_busy"][1]["connections"] == 4
    assert data["forced_busy"][1]["busy_count"] == 3
    assert data["forced_busy"][1]["retry_succeeded"] == 3
    assert data["portable_commit_writer_lock"]["requested_callback_ms"] == 50
    assert data["portable_commit_writer_lock"]["writer_lock_held_ms"] >= 50
    assert data["portable_commit_writer_lock"]["job_state"] == "succeeded"
    assert data["crash_recovery"] == {
        "uncommitted_exit_code": 23,
        "uncommitted_row_absent": True,
        "acknowledged_exit_code": 24,
        "acknowledged_row_present": True,
        "acknowledged_binding_present": True,
        "acknowledged_event_present": True,
        "post_crash_write_succeeded": True,
    }
    assert data["integrity"] == {
        "integrity_check": "ok",
        "foreign_key_violations": 0,
        "checkpoint_busy": 0,
        "wal_frames_remaining": 0,
        "journal_mode": "wal",
        "user_version": 6,
    }
    serialized = json.dumps(data, allow_nan=False)
    assert str(tmp_path) not in serialized
    assert "jobs.sqlite" not in serialized


def test_gate_rejects_unadmitted_sqlite_before_spawning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.runtime_info._sqlite_parallel_jobs_supported",
        lambda version: False,
    )
    monkeypatch.setattr(
        "app.runtime_sqlite_gate._spawn_context",
        lambda: pytest.fail("unadmitted SQLite must stop before spawn"),
    )

    with pytest.raises(DomainError) as raised:
        _run_sqlite_wal_gate(tmp_path, _SqliteWalGateProfile())

    assert raised.value.code == "sqlite_wal_gate_version_ineligible"
    assert raised.value.details == {
        "sqlite_version": sqlite3.sqlite_version
    }
    assert not tuple(tmp_path.iterdir())


def test_runtime_sqlite_gate_cli_projects_safe_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_root = tmp_path / "private gate root"
    private_root.mkdir()
    report = {
        "schema_version": 1,
        "scenarios_passed": True,
        "connection_counts": [1, 4, 8, 16],
    }
    monkeypatch.setattr(
        "app.runtime_sqlite_gate.run_sqlite_wal_gate",
        lambda root: report if root == private_root else pytest.fail("wrong root"),
    )

    assert (
        main(
            [
                "runtime",
                "sqlite-wal-gate",
                "--root",
                str(private_root),
                "--json",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)

    assert captured.err == ""
    assert captured.out.count("\n") == 1
    assert envelope["command"] == "runtime sqlite-wal-gate"
    assert envelope["data"] == report
    assert str(private_root) not in captured.out


def test_runtime_sqlite_gate_parse_error_preserves_command_identity(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["runtime", "sqlite-wal-gate", "--json"]) == 2
    envelope = json.loads(capsys.readouterr().out)

    assert envelope["command"] == "runtime sqlite-wal-gate"
    assert envelope["error"]["code"] == "cli_usage_invalid"


def test_runtime_sqlite_gate_cli_explains_version_admission_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def reject_version(root: Path) -> dict[str, object]:
        raise DomainError(
            "sqlite_wal_gate_version_ineligible",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
            "The loaded SQLite version is not admitted for the WAL gate",
            {"sqlite_version": "3.50.4"},
        )

    monkeypatch.setattr(
        "app.runtime_sqlite_gate.run_sqlite_wal_gate",
        reject_version,
    )

    assert main(
        [
            "runtime",
            "sqlite-wal-gate",
            "--root",
            str(tmp_path),
            "--json",
        ]
    ) == 10
    envelope = json.loads(capsys.readouterr().out)

    assert envelope["error"]["code"] == "sqlite_wal_gate_version_ineligible"
    assert envelope["error"]["next_actions"] == [
        "Run the Gate from a Runtime with an explicitly admitted SQLite build"
    ]

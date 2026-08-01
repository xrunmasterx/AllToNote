from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
import zipfile
from pathlib import Path

import pytest

from app.adapters.jobs.sqlite_repository import SqliteJobRepository
import tools.runtime_windows_release as release_module

from tools.runtime_windows_release import (
    ReleaseError,
    _archive_members,
    _clean_environment,
    _file_manifest,
    _legacy_jobstore_fixture,
    _load_lock,
    _locked_wheel_install_arguments,
    _parser,
    _remove_direct_url_metadata,
    _remove_pip_console_scripts,
    _run_engine_lifecycle_gate,
    _run_legacy_jobstore_migration,
    _verify_inputs,
)


ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "runtime-windows-x86_64.lock.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path) -> tuple[dict[str, object], Path, Path]:
    inputs = tmp_path / "inputs"
    wheelhouse = tmp_path / "wheelhouse"
    inputs.mkdir()
    wheelhouse.mkdir()
    python = inputs / "python.zip"
    spdx = inputs / "python.spdx.json"
    sqlite = inputs / "sqlite.zip"
    wheel = wheelhouse / "runtime.whl"
    python.write_bytes(b"python")
    spdx.write_bytes(b"spdx")
    sqlite.write_bytes(b"sqlite")
    wheel.write_bytes(b"wheel")
    lock: dict[str, object] = {
        "schema_version": 1,
        "platform": "windows-x86_64",
        "python": {
            "archive": python.name,
            "sha256": _sha256(python),
            "spdx": spdx.name,
            "spdx_sha256": _sha256(spdx),
        },
        "sqlite": {
            "archive": sqlite.name,
            "sha3_256": hashlib.sha3_256(sqlite.read_bytes()).hexdigest(),
        },
        "wheels": [
            {
                "filename": wheel.name,
                "byte_length": wheel.stat().st_size,
                "sha256": _sha256(wheel),
            }
        ],
    }
    return lock, inputs, wheelhouse


def test_engine_lifecycle_gate_reconnects_and_stops_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    state = {"running": False, "generation": 0}
    state_lock = threading.Lock()

    def engine_id() -> str:
        return f"018f0000-0000-7000-8000-{state['generation']:012d}"

    def fake_run_cli(_root, arguments, _environment, **_kwargs):
        command = tuple(arguments)
        calls.append(command)
        with state_lock:
            if command == ("runtime", "info"):
                return {
                    "data": {
                        "engine": {
                            "supported": True,
                            "running": state["running"],
                            "state": "running" if state["running"] else "stopped",
                        }
                    }
                }
            if command == ("engine", "ensure"):
                started = not state["running"]
                if started:
                    state["running"] = True
                    state["generation"] += 1
                return {
                    "data": {
                        "running": True,
                        "state": "running",
                        "started": started,
                        "engine_id": engine_id(),
                    }
                }
            if command == ("engine", "stop"):
                state["running"] = False
                return {
                    "data": {
                        "running": False,
                        "state": "stopped",
                        "stopped": True,
                        "engine_id": None,
                    }
                }
            return {
                "data": {
                    "running": state["running"],
                    "state": "running" if state["running"] else "stopped",
                    "engine_id": engine_id() if state["running"] else None,
                }
            }

    def fake_process_probe(_root, _environment, operation):
        with state_lock:
            if operation == "inspect":
                return {"engine_id": engine_id(), "rss_bytes": 24 * 1024 * 1024}
            terminated_id = engine_id()
            state["running"] = False
            return {"engine_id": terminated_id}

    monkeypatch.setattr(release_module, "_run_cli", fake_run_cli)
    monkeypatch.setattr(
        release_module,
        "_run_engine_process_probe",
        fake_process_probe,
    )
    monkeypatch.setattr(
        release_module,
        "_run_engine_idle_probe",
        lambda _root, _environment: {"descriptor_exists": False, "exit_code": 0},
    )
    monkeypatch.setattr(release_module, "_ENGINE_START_SAMPLES", 2)
    monkeypatch.setattr(release_module, "_ENGINE_ENSURE_CONCURRENCY", 4)

    report = _run_engine_lifecycle_gate(tmp_path, {})

    assert report["supported"] is True
    assert report["parent_exit_reconnect"] is True
    assert len(report["cold_start_samples_milliseconds"]) == 2
    assert report["idle_rss_bytes"] == 24 * 1024 * 1024
    assert report["ensure_concurrency"] == 4
    assert report["forced_death_reensure"] is True
    assert report["idle_exit"] is True
    assert report["stopped_cleanly"] is True
    assert calls.count(("engine", "ensure")) >= 2 + 4 + 1
    assert calls[-1] == ("engine", "stop")


def test_engine_lifecycle_gate_stops_partial_start_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    running = False

    def fake_run_cli(_root, arguments, _environment, **_kwargs):
        nonlocal running
        command = tuple(arguments)
        calls.append(command)
        if command == ("engine", "ensure"):
            running = True
            raise ReleaseError("candidate failed after launch")
        if command == ("engine", "stop"):
            running = False
            return {"data": {"running": False, "state": "stopped"}}
        return {
            "data": {
                "running": running,
                "state": "running" if running else "stopped",
            }
        }

    monkeypatch.setattr(release_module, "_run_cli", fake_run_cli)

    with pytest.raises(ReleaseError, match="failed after launch"):
        _run_engine_lifecycle_gate(tmp_path, {})

    assert running is False
    assert calls[-1] == ("engine", "stop")


def test_checked_in_lock_freezes_the_validated_runtime_inputs() -> None:
    lock = _load_lock(LOCK)

    assert lock["runtime_source_commit"] == "54019ea58a280dea6b508044fc0dbe0558684203"
    assert lock["python"]["version"] == "3.14.6"
    assert lock["sqlite"] == {
        "version": "3.53.4",
        "archive": "sqlite-dll-win-x64-3530400.zip",
        "url": "https://www.sqlite.org/2026/sqlite-dll-win-x64-3530400.zip",
        "sha3_256": "deddee963c810d1eeac3ce5e15c7c41da21a1c54d7a39cf54fbf577d2f50de3a",
        "source_id": "2026-07-24 19:02:57 bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc",
    }
    assert len(lock["wheels"]) == 15


def test_checked_in_lock_binds_the_legacy_jobstore_fixture() -> None:
    lock = _load_lock(LOCK)

    fixture = _legacy_jobstore_fixture(LOCK, lock)

    assert fixture == ROOT / "tools" / "fixtures" / "job-store-v1.sql"
    sql = fixture.read_text(encoding="utf-8")
    assert "PRAGMA user_version = 1" in sql
    assert "job_legacy_release_fixture" in sql
    assert "'succeeded'" in sql
    assert "INSERT INTO attempts" in sql
    assert "INSERT INTO checkpoints" in sql
    assert "INSERT INTO source_identities" in sql
    assert "CREATE TABLE job_execution_bindings" not in sql
    assert b"\r" not in fixture.read_bytes()
    assert fixture.read_bytes().endswith(b"\n")
    assert lock["legacy_jobstore_fixture"]["source_commit"] == (
        "b862f564880974fea12f4b4b3d2b8bcd5b0cc019"
    )


def test_legacy_jobstore_fixture_hash_drift_is_rejected(tmp_path: Path) -> None:
    fixture = tmp_path / "job-store-v1.sql"
    fixture.write_text("PRAGMA user_version = 1;", encoding="utf-8")
    lock = {
        "legacy_jobstore_fixture": {
            "path": fixture.name,
            "sha256": "0" * 64,
            "schema_version": 1,
            "source_commit": "0" * 40,
        }
    }

    with pytest.raises(ReleaseError, match="legacy JobStore fixture hash"):
        _legacy_jobstore_fixture(tmp_path / "lock.json", lock)


@pytest.mark.parametrize("path", ("../outside.sql", "/absolute.sql", "C:/drive.sql"))
def test_legacy_jobstore_fixture_path_must_be_portable(
    tmp_path: Path,
    path: str,
) -> None:
    lock = {
        "legacy_jobstore_fixture": {
            "path": path,
            "sha256": "0" * 64,
            "schema_version": 1,
            "source_commit": "0" * 40,
        }
    }

    with pytest.raises(ReleaseError, match="fixture path"):
        _legacy_jobstore_fixture(tmp_path / "lock.json", lock)


def test_legacy_jobstore_fixture_migrates_and_reopens(tmp_path: Path) -> None:
    lock = _load_lock(LOCK)
    fixture = _legacy_jobstore_fixture(LOCK, lock)
    machine_root = tmp_path / "legacy-jobstore"
    machine_root.mkdir()
    database = machine_root / "jobs.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript(fixture.read_text(encoding="utf-8"))
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1

    migrated = SqliteJobRepository.open(machine_root)
    binding = migrated.get_job_execution_binding("job_legacy_release_fixture")
    job = migrated.get_job("job_legacy_release_fixture")
    result = migrated.get_job_result("job_legacy_release_fixture")
    attempts = migrated.list_attempts("job_legacy_release_fixture")
    events = migrated.list_events("job_legacy_release_fixture")
    checkpoint = migrated.latest_checkpoint("job_legacy_release_fixture", "publish")
    source_identity = migrated.read_source_identity_candidate(
        "fixture", "legacy-release-source"
    )

    assert job.request_hash == (
        "sha256:b6cdcfaca0099ffe78c31f3f33e1b98a00b124eb4d5d0126bb5dfcc5f8691e85"
    )
    assert binding.recipe_id == "alltonote.video-course-note"
    assert binding.recipe_version == 2
    assert binding.executor_id == "alltonote.video"
    assert binding.executor_version == 1
    assert binding.pack_id == "media-basic"
    assert binding.pack_version == "legacy-v1"
    assert job.state.value == "succeeded"
    assert result is not None
    assert result.bundle_id == "bnd_018cc251-f400-7000-8000-000000000005"
    assert result.quality_overall.value == "pass"
    assert [attempt.state.value for attempt in attempts] == ["succeeded"]
    assert [event.event_type for event in events] == [
        "portable.commit.completed.v1",
        "job.state.v1",
    ]
    assert events[-1].payload_json == '{"state":"succeeded"}'
    assert checkpoint is not None
    assert checkpoint.checkpoint_id == "chk_legacy_release_fixture"
    assert source_identity is not None
    assert source_identity.source_id == "src_018cc251-f400-7000-8000-000000000006"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    reopened = SqliteJobRepository.open(machine_root)
    assert reopened.get_job_execution_binding("job_legacy_release_fixture") == binding
    assert reopened.get_job_result("job_legacy_release_fixture") == result
    assert reopened.list_attempts("job_legacy_release_fixture") == attempts
    assert reopened.list_events("job_legacy_release_fixture") == events
    assert reopened.latest_checkpoint("job_legacy_release_fixture", "publish") == (
        checkpoint
    )
    assert reopened.read_source_identity_candidate(
        "fixture", "legacy-release-source"
    ) == source_identity


def test_release_gate_accepts_only_current_jobstore_migration_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "schema_before": 1,
        "schema_after": 5,
        "integrity": "ok",
        "foreign_key_errors": 0,
        "job_id": "job_legacy_release_fixture",
        "job_state": "succeeded",
        "result_bundle_id": "bnd_018cc251-f400-7000-8000-000000000005",
        "result_quality": "pass",
        "attempt_states": ["succeeded"],
        "event_types": ["portable.commit.completed.v1", "job.state.v1"],
        "checkpoint_id": "chk_legacy_release_fixture",
        "source_identity_preserved": True,
        "binding": {
            "recipe_id": "alltonote.video-course-note",
            "recipe_version": 2,
            "executor_id": "alltonote.video",
            "executor_version": 1,
            "pack_id": "media-basic",
            "pack_version": "legacy-v1",
        },
        "reopened_same_binding": True,
        "reopened_data_preserved": True,
    }
    monkeypatch.setattr(
        release_module,
        "_run",
        lambda *_args, **_kwargs: json.dumps(payload),
    )

    result = _run_legacy_jobstore_migration(
        tmp_path,
        tmp_path / "fixture.sql",
        tmp_path / "machine",
        {},
    )

    assert result == payload

    payload["schema_after"] = 2
    with pytest.raises(ReleaseError, match="legacy JobStore Gate"):
        _run_legacy_jobstore_migration(
            tmp_path,
            tmp_path / "fixture.sql",
            tmp_path / "machine",
            {},
        )
def test_cli_lock_option_maps_to_the_assembler_parameter(tmp_path: Path) -> None:
    arguments = _parser().parse_args(
        [
            "--lock",
            str(tmp_path / "lock.json"),
            "--inputs",
            str(tmp_path / "inputs"),
            "--wheelhouse",
            str(tmp_path / "wheels"),
            "--builder-python",
            str(tmp_path / "python.exe"),
            "--output",
            str(tmp_path / "output"),
            "--gate-root",
            str(tmp_path / "gate"),
        ]
    )

    assert arguments.lock_path == tmp_path / "lock.json"


def test_release_input_verifier_requires_the_exact_wheelhouse(tmp_path: Path) -> None:
    lock, inputs, wheelhouse = _fixture(tmp_path)

    _python, _spdx, _sqlite, wheels = _verify_inputs(lock, inputs, wheelhouse)

    assert wheels == tuple(lock["wheels"])

    (wheelhouse / "extra.whl").write_bytes(b"extra")
    with pytest.raises(ReleaseError, match="wheelhouse"):
        _verify_inputs(lock, inputs, wheelhouse)


def test_release_input_verifier_rejects_hash_drift(tmp_path: Path) -> None:
    lock, inputs, wheelhouse = _fixture(tmp_path)
    (inputs / "sqlite.zip").write_bytes(b"tampered")

    with pytest.raises(ReleaseError, match="SQLite archive hash"):
        _verify_inputs(lock, inputs, wheelhouse)


def test_pip_install_uses_only_absolute_locked_wheels(tmp_path: Path) -> None:
    builder = tmp_path / "builder" / "python.exe"
    site_packages = tmp_path / "stage" / "site-packages"
    wheelhouse = tmp_path / "wheelhouse"
    wheels = (
        {"filename": "alltonote_runtime-0.1.0-py3-none-any.whl"},
        {"filename": "llm_iwiki-0.1.3-py3-none-any.whl"},
    )

    arguments = _locked_wheel_install_arguments(
        builder,
        site_packages,
        wheelhouse,
        wheels,
    )

    assert "--isolated" in arguments
    assert "--no-index" in arguments
    assert "--no-deps" in arguments
    assert "--find-links" not in arguments
    assert arguments[-2:] == tuple(
        str((wheelhouse / item["filename"]).resolve(strict=False))
        for item in wheels
    )


def test_pip_environment_discards_external_configuration() -> None:
    environment = _clean_environment(
        {
            "PATH": "trusted-path",
            "PIP_FIND_LINKS": "untrusted-wheelhouse",
            "PIP_CONFIG_FILE": "untrusted-pip.ini",
            "PIP_CONSTRAINT": "untrusted-constraints.txt",
        }
    )

    assert environment["PATH"] == "trusted-path"
    assert environment["PIP_CONFIG_FILE"] == os.devnull
    assert environment["PIP_NO_INDEX"] == "1"
    assert "PIP_FIND_LINKS" not in environment
    assert "PIP_CONSTRAINT" not in environment


def test_direct_wheel_provenance_does_not_leak_the_builder_path(
    tmp_path: Path,
) -> None:
    site_packages = tmp_path / "site-packages"
    dist_info = site_packages / "example-1.0.dist-info"
    dist_info.mkdir(parents=True)
    direct_url = dist_info / "direct_url.json"
    direct_url.write_text(
        '{"url":"file:///private/wheelhouse/example.whl"}',
        encoding="utf-8",
    )
    record = dist_info / "RECORD"
    record.write_text(
        "example/__init__.py,sha256=payload,1\n"
        "example-1.0.dist-info/direct_url.json,sha256=private,50\n"
        "example-1.0.dist-info/RECORD,,\n",
        encoding="utf-8",
        newline="",
    )

    _remove_direct_url_metadata(site_packages, 1)

    assert not direct_url.exists()
    assert record.read_text(encoding="utf-8") == (
        "example/__init__.py,sha256=payload,1\n"
        "example-1.0.dist-info/RECORD,,\n"
    )


def test_pip_console_launchers_do_not_leak_the_builder_path(tmp_path: Path) -> None:
    site_packages = tmp_path / "site-packages"
    scripts = site_packages / "bin"
    scripts.mkdir(parents=True)
    names = ("alltonote.exe", "cffi-gen-src.exe", "iwiki.exe", "keyring.exe")
    for name in names:
        (scripts / name).write_text("C:/private/builder/python.exe", encoding="utf-8")
        dist_info = site_packages / f"{name}-1.0.dist-info"
        dist_info.mkdir()
        (dist_info / "RECORD").write_text(
            f"../../bin/{name},sha256=private,1\n"
            f"{dist_info.name}/RECORD,,\n",
            encoding="utf-8",
            newline="",
        )

    _remove_pip_console_scripts(site_packages)

    assert not scripts.exists()
    for name in names:
        record = site_packages / f"{name}-1.0.dist-info" / "RECORD"
        assert "../../bin/" not in record.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ("../outside", "/absolute", "C:/drive"))
def test_archive_rejects_unsafe_member_paths(tmp_path: Path, name: str) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(name, b"unsafe")

    with pytest.raises(ReleaseError, match="unsafe entry"):
        _archive_members(archive)


def test_archive_rejects_symlink_members(tmp_path: Path) -> None:
    archive = tmp_path / "linked.zip"
    member = zipfile.ZipInfo("linked")
    member.create_system = 3
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(member, b"target")

    with pytest.raises(ReleaseError, match="unsafe entry"):
        _archive_members(archive)


def test_file_manifest_uses_unique_relative_paths_and_excludes_itself(
    tmp_path: Path,
) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "release").mkdir()
    (tmp_path / "a.txt").write_bytes(b"a")
    (tmp_path / "nested" / "b.txt").write_bytes(b"b")
    (tmp_path / "release" / "file-manifest.json").write_text(
        "old", encoding="utf-8"
    )

    manifest = _file_manifest(tmp_path)

    assert manifest["file_count"] == 2
    assert [item["path"] for item in manifest["files"]] == [
        "a.txt",
        "nested/b.txt",
    ]
    assert len(json.dumps(manifest)) > 0

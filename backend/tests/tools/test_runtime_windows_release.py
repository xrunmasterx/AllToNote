from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import sqlite3
import stat
import sys
import threading
import types
import zipfile
from contextlib import redirect_stdout
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
    verify_runtime_candidate,
)


ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "runtime-windows-x86_64.lock.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _runtime_candidate(tmp_path: Path) -> tuple[Path, str]:
    candidate = tmp_path / "Runtime 候选 有空格"
    release = candidate / "release"
    release.mkdir(parents=True)
    (candidate / "alltonote.py").write_text("print('ok')\n", encoding="utf-8")
    controls = {
        "runtime-inputs.json": {
            "schema_version": 1,
            "runtime_source_commit": "1" * 40,
            "runtime_version": "0.1.0",
            "platform": "windows-x86_64",
            "python": {},
            "sqlite": {},
            "legacy_jobstore_fixture": {},
        },
        "wheelhouse-lock.json": {
            "schema_version": 1,
            "python_abi": "cp314-win_amd64",
            "wheel_count": 1,
            "wheels": [{"filename": "runtime.whl"}],
        },
        "acceptance.json": {
            "schema_version": 1,
            "status": "candidate-pass",
            "runtime_source_commit": "1" * 40,
            "identity": {},
            "checks": {
                "version": True,
                "runtime_info": True,
                "runtime_doctor": True,
                "engine_lifecycle": True,
                "unicode_workspace_init": True,
                "sqlite_wal_gate": True,
                "legacy_jobstore_migration": True,
            },
            "wal_gate": {
                "scenarios_passed": True,
                "parallel_job_execution_enabled": False,
            },
        },
    }
    for name, payload in controls.items():
        (release / name).write_bytes(release_module._canonical_json(payload))
    (release / "file-manifest.json").write_bytes(
        release_module._canonical_json(_file_manifest(candidate))
    )
    return candidate, _sha256(release / "file-manifest.json")


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


def _record_hash(content: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
    return f"sha256={encoded.decode('ascii')}"


def _wheel_install_fixture(
    tmp_path: Path,
    *,
    source_contains_installer: bool = False,
) -> tuple[Path, Path, tuple[dict[str, object], ...]]:
    wheelhouse = tmp_path / "wheelhouse"
    site_packages = tmp_path / "site-packages"
    wheelhouse.mkdir()
    site_packages.mkdir()
    dist_info = "example-1.0.dist-info"
    source_files = {
        "example/__init__.py": b"VALUE = 1\n",
        f"{dist_info}/METADATA": b"Name: example\nVersion: 1.0\n",
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\n",
    }
    if source_contains_installer:
        source_files[f"{dist_info}/INSTALLER"] = b"pip\n"
    record_path = f"{dist_info}/RECORD"
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for path, content in source_files.items():
        writer.writerow((path, _record_hash(content), len(content)))
    writer.writerow((record_path, "", ""))
    record_bytes = output.getvalue().encode("utf-8")
    wheel = wheelhouse / "example-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, content in source_files.items():
            archive.writestr(path, content)
        archive.writestr(record_path, record_bytes)

    for path, content in source_files.items():
        target = site_packages.joinpath(*path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    generated = {
        f"{dist_info}/INSTALLER": b"pip\n",
        f"{dist_info}/REQUESTED": b"",
    }
    for path, content in generated.items():
        site_packages.joinpath(*path.split("/")).write_bytes(content)
    installed_record = io.StringIO(newline="")
    installed_writer = csv.writer(installed_record, lineterminator="\n")
    for path, content in {**source_files, **generated}.items():
        installed_writer.writerow((path, _record_hash(content), len(content)))
    installed_writer.writerow((record_path, "", ""))
    site_packages.joinpath(*record_path.split("/")).write_text(
        installed_record.getvalue(),
        encoding="utf-8",
        newline="",
    )
    wheels = (
        {
            "filename": wheel.name,
            "byte_length": wheel.stat().st_size,
            "sha256": _sha256(wheel),
        },
    )
    return site_packages, wheelhouse, wheels


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


@pytest.mark.parametrize(
    "payload",
    (
        {
            "ok": True,
            "data": {
                "healthy": False,
                "checks": [
                    {
                        "code": "runtime.required",
                        "status": "fail",
                        "action": "Repair the Runtime",
                        "dynamic": False,
                    }
                ],
            },
        },
        {
            "ok": True,
            "data": {
                "healthy": True,
                "checks": [
                    {
                        "code": "runtime.required",
                        "status": "fail",
                        "action": "Repair the Runtime",
                        "dynamic": False,
                    }
                ],
            },
        },
    ),
)
def test_runtime_doctor_gate_rejects_unhealthy_or_inconsistent_report(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ReleaseError, match="Runtime doctor Gate failed"):
        release_module._require_healthy_runtime_doctor(payload)


def test_runtime_doctor_gate_accepts_passes_and_warnings() -> None:
    release_module._require_healthy_runtime_doctor(
        {
            "ok": True,
            "data": {
                "healthy": True,
                "checks": [
                    {
                        "code": "runtime.required",
                        "status": "pass",
                        "action": None,
                        "dynamic": False,
                    },
                    {
                        "code": "pack.optional",
                        "status": "warn",
                        "action": "Install the optional Pack",
                        "dynamic": False,
                    },
                ],
            },
        }
    )


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


def test_engine_idle_probe_uses_the_current_host_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = object()
    calls: list[tuple[object, float]] = []
    instance = types.SimpleNamespace(descriptor=tmp_path / "engine.json")

    host_module = types.ModuleType("app.engine.host")

    def fake_run_engine_host(*, paths: object, idle_seconds: float) -> int:
        calls.append((paths, idle_seconds))
        return 0

    host_module.run_engine_host = fake_run_engine_host
    instance_module = types.ModuleType("app.engine.instance")
    instance_module.EngineInstancePaths = types.SimpleNamespace(
        from_runtime_paths=lambda candidate: instance if candidate is paths else None
    )
    runtime_paths_module = types.ModuleType("app.runtime_paths")
    runtime_paths_module.resolve_runtime_paths = lambda: paths
    monkeypatch.setitem(sys.modules, "app.engine.host", host_module)
    monkeypatch.setitem(sys.modules, "app.engine.instance", instance_module)
    monkeypatch.setitem(sys.modules, "app.runtime_paths", runtime_paths_module)

    output = io.StringIO()
    with redirect_stdout(output):
        exec(release_module._ENGINE_IDLE_PROBE, {})

    assert calls == [(paths, 0.25)]
    assert json.loads(output.getvalue()) == {
        "descriptor_exists": False,
        "exit_code": 0,
    }


def test_checked_in_lock_freezes_the_validated_runtime_inputs() -> None:
    lock = _load_lock(LOCK)

    assert lock["runtime_source_commit"] == "6d76f8ccfb480fd7d4711d2986bb4aa7bd1fb4ec"
    assert lock["builder"] == {
        "implementation": "CPython",
        "version": "3.14.0",
        "platform": "win32",
        "machine": "AMD64",
        "pointer_bits": 64,
        "python_executable_sha256": (
            "467014615a5255aca450ae88100dd2caf887da87657f00e3c2171ec44a685aec"
        ),
        "python_runtime_dll_sha256": (
            "f1722bd369d79fecbc85f3ed2790c30c330b9413fd74332f95b086e60dfacc2a"
        ),
        "pip_version": "25.2",
        "pip_tree_sha256": (
            "efe1bd4b245d602d84b97bf591d5d3aee4c91c0349996a9f0c5c73633f3e585b"
        ),
        "builder_tree_sha256": (
            "931b2c04dad774969bb321c788ece6c9991750f7485baccac856a0c2c6cf7200"
        ),
    }
    assert lock["python"]["version"] == "3.14.6"
    assert lock["wheels"][0] == {
        "filename": "alltonote_runtime-0.1.0-py3-none-any.whl",
        "byte_length": 578649,
        "sha256": "0a6a404592f6198b99de0c30b5547de13665cdb6e50685917adc586d1acb4870",
    }
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
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
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
        "schema_after": 6,
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
    assert "-I" in arguments
    assert "-B" in arguments
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


def test_builder_toolchain_gate_rejects_missing_lock_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = tmp_path / "builder" / "python.exe"
    builder.parent.mkdir()
    builder.write_bytes(b"python")
    executed = False

    def unexpected_run(*_args, **_kwargs):
        nonlocal executed
        executed = True
        raise AssertionError("builder must not execute")

    monkeypatch.setattr(release_module, "_run", unexpected_run)

    with pytest.raises(ReleaseError, match="Builder toolchain"):
        release_module._verify_builder_toolchain({}, builder, {})

    assert executed is False


def test_builder_toolchain_attestation_is_locked_and_path_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Private Builder Root"
    builder = root / "python.exe"
    runtime_dll = root / "python314.dll"
    pip_root = root / "Lib" / "site-packages" / "pip"
    pip_dist = root / "Lib" / "site-packages" / "pip-25.2.dist-info"
    pip_root.mkdir(parents=True)
    pip_dist.mkdir()
    builder.write_bytes(b"python")
    runtime_dll.write_bytes(b"runtime")
    (pip_root / "__init__.py").write_text("__version__ = '25.2'\n", encoding="utf-8")
    (pip_dist / "METADATA").write_text("Name: pip\nVersion: 25.2\n", encoding="utf-8")
    pip_tree_sha256 = release_module._builder_pip_tree_sha256(root, "25.2")
    builder_tree_sha256 = release_module._builder_tree_sha256(root)
    lock = {
        "builder": {
            "implementation": "CPython",
            "version": "3.14.0",
            "platform": "win32",
            "machine": "AMD64",
            "pointer_bits": 64,
            "python_executable_sha256": _sha256(builder),
            "python_runtime_dll_sha256": _sha256(runtime_dll),
            "pip_version": "25.2",
            "pip_tree_sha256": pip_tree_sha256,
            "builder_tree_sha256": builder_tree_sha256,
        }
    }
    monkeypatch.setattr(
        release_module,
        "_run",
        lambda *_args, **_kwargs: json.dumps(
            {
                "implementation": "CPython",
                "version": "3.14.0",
                "platform": "win32",
                "machine": "AMD64",
                "pointer_bits": 64,
                "pip_version": "25.2",
            }
        ),
    )

    result = release_module._verify_builder_toolchain(lock, builder, {})

    assert result == {"schema_version": 1, **lock["builder"]}
    assert str(root) not in json.dumps(result)

    runtime_dll.write_bytes(b"tampered")
    with pytest.raises(ReleaseError, match="Builder toolchain"):
        release_module._verify_builder_toolchain(lock, builder, {})


def test_wheel_install_attestation_reconciles_archive_records(
    tmp_path: Path,
) -> None:
    site_packages, wheelhouse, wheels = _wheel_install_fixture(tmp_path)

    result = release_module._verify_wheel_install(
        site_packages,
        wheelhouse,
        wheels,
    )

    assert result["schema_version"] == 1
    assert result["wheel_count"] == 1
    assert result["source_files_verified"] == 3
    assert result["installed_files_verified"] == 6
    assert result["generated_files"] == 2
    assert str(tmp_path) not in json.dumps(result)


@pytest.mark.parametrize(
    "mutation",
    ("missing", "extra", "manifest_named_extra", "tampered"),
)
def test_wheel_install_attestation_rejects_unexplained_installed_files(
    tmp_path: Path,
    mutation: str,
) -> None:
    site_packages, wheelhouse, wheels = _wheel_install_fixture(tmp_path)
    payload = site_packages / "example" / "__init__.py"
    if mutation == "missing":
        payload.unlink()
    elif mutation == "extra":
        (site_packages / "injected.py").write_text("injected\n", encoding="utf-8")
    elif mutation == "manifest_named_extra":
        hidden = site_packages / "release" / "file-manifest.json"
        hidden.parent.mkdir()
        hidden.write_text("injected\n", encoding="utf-8")
    else:
        payload.write_text("TAMPERED = True\n", encoding="utf-8")

    with pytest.raises(ReleaseError, match="wheel installation"):
        release_module._verify_wheel_install(site_packages, wheelhouse, wheels)


def test_wheel_install_attestation_rebinds_final_tree_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site_packages, wheelhouse, wheels = _wheel_install_fixture(tmp_path)
    payload = site_packages / "example" / "__init__.py"
    original_rows = release_module._candidate_file_rows

    def mutate_then_scan(root: Path, **kwargs):
        payload.write_text("TAMPERED_AFTER_RECORD_CHECK = True\n", encoding="utf-8")
        return original_rows(root, **kwargs)

    monkeypatch.setattr(release_module, "_candidate_file_rows", mutate_then_scan)

    with pytest.raises(ReleaseError, match="wheel installation"):
        release_module._verify_wheel_install(site_packages, wheelhouse, wheels)


def test_wheel_install_attestation_rejects_generated_metadata_in_source_wheel(
    tmp_path: Path,
) -> None:
    site_packages, wheelhouse, wheels = _wheel_install_fixture(
        tmp_path,
        source_contains_installer=True,
    )

    with pytest.raises(ReleaseError, match="wheel installation"):
        release_module._verify_wheel_install(site_packages, wheelhouse, wheels)


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


def test_runtime_candidate_verifier_accepts_exact_read_only_tree(
    tmp_path: Path,
) -> None:
    candidate, manifest_sha256 = _runtime_candidate(tmp_path)
    before = {
        path.relative_to(candidate).as_posix(): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
        )
        for path in candidate.rglob("*")
        if path.is_file()
    }

    result = verify_runtime_candidate(
        candidate,
        expected_manifest_sha256=manifest_sha256,
        expected_source_commit="1" * 40,
    )

    after = {
        path.relative_to(candidate).as_posix(): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
        )
        for path in candidate.rglob("*")
        if path.is_file()
    }
    assert result == {
        "manifest_sha256": manifest_sha256,
        "runtime_source_commit": "1" * 40,
        "runtime_version": "0.1.0",
        "platform": "windows-x86_64",
        "status": "candidate-pass",
        "file_count": 4,
        "total_bytes": sum(len(content) for content, _mtime in before.values())
        - len(before["release/file-manifest.json"][0]),
    }
    assert after == before


def test_runtime_candidate_verifier_requires_external_manifest_digest(
    tmp_path: Path,
) -> None:
    candidate, _manifest_sha256 = _runtime_candidate(tmp_path)

    with pytest.raises(ReleaseError, match="expected manifest hash"):
        verify_runtime_candidate(
            candidate,
            expected_manifest_sha256="0" * 64,
        )


@pytest.mark.parametrize("mutation", ("tamper", "extra", "missing"))
def test_runtime_candidate_verifier_rejects_tree_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    candidate, manifest_sha256 = _runtime_candidate(tmp_path)
    payload = candidate / "alltonote.py"
    if mutation == "tamper":
        payload.write_text("print('tampered')\n", encoding="utf-8")
    elif mutation == "extra":
        (candidate / "extra.txt").write_text("extra", encoding="utf-8")
    else:
        payload.unlink()

    with pytest.raises(ReleaseError, match="candidate files"):
        verify_runtime_candidate(
            candidate,
            expected_manifest_sha256=manifest_sha256,
        )


def test_runtime_candidate_verifier_rejects_unsafe_acceptance_claim(
    tmp_path: Path,
) -> None:
    candidate, _manifest_sha256 = _runtime_candidate(tmp_path)
    acceptance = candidate / "release" / "acceptance.json"
    payload = json.loads(acceptance.read_text(encoding="utf-8"))
    payload["wal_gate"]["parallel_job_execution_enabled"] = True
    acceptance.write_bytes(release_module._canonical_json(payload))
    manifest = _file_manifest(candidate)
    (candidate / "release" / "file-manifest.json").write_bytes(
        release_module._canonical_json(manifest)
    )

    with pytest.raises(ReleaseError, match="acceptance metadata"):
        verify_runtime_candidate(
            candidate,
            expected_manifest_sha256=_sha256(
                candidate / "release" / "file-manifest.json"
            ),
        )


def test_file_manifest_rejects_directory_traversal_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed_walk(_root, *, followlinks, onerror=None):
        assert followlinks is False
        if onerror is not None:
            onerror(PermissionError("denied"))
        return []

    monkeypatch.setattr(release_module.os, "walk", failed_walk)

    with pytest.raises(ReleaseError, match="filesystem entries"):
        _file_manifest(tmp_path)


def test_runtime_candidate_verifier_rejects_control_swap_after_tree_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, manifest_sha256 = _runtime_candidate(tmp_path)
    acceptance = candidate / "release" / "acceptance.json"
    original_rows = release_module._candidate_file_rows

    def rows_then_swap(root: Path, **kwargs):
        rows = original_rows(root, **kwargs)
        payload = json.loads(acceptance.read_text(encoding="utf-8"))
        payload["note"] = "swapped after tree hash"
        acceptance.write_bytes(release_module._canonical_json(payload))
        return rows

    monkeypatch.setattr(release_module, "_candidate_file_rows", rows_then_swap)

    with pytest.raises(ReleaseError, match="candidate files"):
        verify_runtime_candidate(
            candidate,
            expected_manifest_sha256=manifest_sha256,
        )


def test_runtime_candidate_verifier_binds_build_provenance_attestations(
    tmp_path: Path,
) -> None:
    candidate, _manifest_sha256 = _runtime_candidate(tmp_path)
    release = candidate / "release"
    builder = release / "builder-toolchain.json"
    wheel_install = release / "wheel-install-attestation.json"
    wheelhouse = release / "wheelhouse-lock.json"
    wheelhouse_payload = json.loads(wheelhouse.read_text(encoding="utf-8"))
    wheelhouse_payload["wheels"] = [
        {
            "filename": "runtime.whl",
            "byte_length": 7,
            "sha256": "1" * 64,
        }
    ]
    wheelhouse.write_bytes(release_module._canonical_json(wheelhouse_payload))
    builder.write_bytes(
        release_module._canonical_json(
            {
                "schema_version": 1,
                "implementation": "CPython",
                "version": "3.14.0",
                "platform": "win32",
                "machine": "AMD64",
                "pointer_bits": 64,
                "python_executable_sha256": "2" * 64,
                "python_runtime_dll_sha256": "3" * 64,
                "pip_version": "25.2",
                "pip_tree_sha256": "4" * 64,
                "builder_tree_sha256": "5" * 64,
            }
        )
    )
    wheel_install.write_bytes(
        release_module._canonical_json(
            {
                "schema_version": 1,
                "wheel_count": 1,
                "source_files_verified": 3,
                "installed_files_verified": 6,
                "generated_files": 2,
                "locked_wheels_sha256": hashlib.sha256(
                    release_module._canonical_json(
                        {"wheels": wheelhouse_payload["wheels"]}
                    )
                ).hexdigest(),
                "installed_tree_sha256": "6" * 64,
                "wheels": [
                    {
                        "filename": "runtime.whl",
                        "archive_sha256": "1" * 64,
                        "source_record_sha256": "7" * 64,
                        "installed_record_sha256": "8" * 64,
                        "source_files_verified": 3,
                        "installed_files_verified": 6,
                    }
                ],
            }
        )
    )
    provenance = {
        "builder_toolchain_sha256": _sha256(builder),
        "wheel_install_attestation_sha256": _sha256(wheel_install),
    }
    for name in ("runtime-inputs.json", "acceptance.json"):
        path = release / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["build_provenance"] = provenance
        if name == "acceptance.json":
            payload["checks"]["builder_toolchain"] = True
            payload["checks"]["wheel_install_attestation"] = True
        path.write_bytes(release_module._canonical_json(payload))
    (release / "file-manifest.json").write_bytes(
        release_module._canonical_json(_file_manifest(candidate))
    )

    result = verify_runtime_candidate(
        candidate,
        expected_manifest_sha256=_sha256(release / "file-manifest.json"),
    )

    assert result["file_count"] == 6

    invalid_builder = json.loads(builder.read_text(encoding="utf-8"))
    invalid_builder["implementation"] = "PyPy"
    builder.write_bytes(release_module._canonical_json(invalid_builder))
    invalid_builder_sha256 = _sha256(builder)
    for name in ("runtime-inputs.json", "acceptance.json"):
        path = release / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["build_provenance"][
            "builder_toolchain_sha256"
        ] = invalid_builder_sha256
        path.write_bytes(release_module._canonical_json(payload))
    (release / "file-manifest.json").write_bytes(
        release_module._canonical_json(_file_manifest(candidate))
    )

    with pytest.raises(ReleaseError, match="build provenance"):
        verify_runtime_candidate(
            candidate,
            expected_manifest_sha256=_sha256(release / "file-manifest.json"),
        )


def test_runtime_candidate_verify_cli_outputs_path_free_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    candidate, manifest_sha256 = _runtime_candidate(tmp_path)

    exit_code = release_module.main(
        [
            "verify",
            "--candidate",
            str(candidate),
            "--expected-manifest-sha256",
            manifest_sha256,
            "--expected-source-commit",
            "1" * 40,
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["data"]["manifest_sha256"] == manifest_sha256
    assert str(candidate) not in captured.out
    assert captured.err == ""

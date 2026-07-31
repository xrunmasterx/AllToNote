from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


class ReleaseError(RuntimeError):
    pass


_MAX_ARCHIVE_ENTRIES = 10_000
_MAX_ARCHIVE_FILE_BYTES = 128 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
_PROCESS_TIMEOUT_SECONDS = 180
_PIP_CONSOLE_SCRIPTS = {
    "alltonote.exe",
    "cffi-gen-src.exe",
    "iwiki.exe",
    "keyring.exe",
}
_SQLITE_PROBE = r"""
import ctypes
import hashlib
import json
import pathlib
import platform
import _sqlite3
import sqlite3
import struct
import sys

def loaded_path(name):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    kernel32.GetModuleFileNameW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_uint,
    ]
    kernel32.GetModuleFileNameW.restype = ctypes.c_uint
    handle = kernel32.GetModuleHandleW(name)
    if not handle:
        raise OSError(ctypes.get_last_error(), name)
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel32.GetModuleFileNameW(handle, buffer, len(buffer))
    if not length:
        raise OSError(ctypes.get_last_error(), name)
    return pathlib.Path(buffer.value).resolve()

connection = sqlite3.connect(":memory:")
sqlite_library = loaded_path("sqlite3.dll")
python_library = loaded_path("python314.dll")
print(json.dumps({
    "python_version": platform.python_version(),
    "implementation": platform.python_implementation(),
    "platform": sys.platform,
    "machine": platform.machine(),
    "pointer_bits": struct.calcsize("P") * 8,
    "python_executable": str(pathlib.Path(sys.executable).resolve()),
    "python_library": str(python_library),
    "sqlite_extension": str(pathlib.Path(_sqlite3.__file__).resolve()),
    "sqlite_library": str(sqlite_library),
    "sqlite_library_sha256": hashlib.sha256(sqlite_library.read_bytes()).hexdigest(),
    "sqlite_version": sqlite3.sqlite_version,
    "sqlite_source_id": connection.execute("select sqlite_source_id()").fetchone()[0],
    "sqlite_threadsafety": sqlite3.threadsafety,
    "sqlite_compile_options": [row[0] for row in connection.execute("pragma compile_options")],
}, sort_keys=True))
"""

_LEGACY_JOB_STORE_PROBE = r"""
import json
import pathlib
import sqlite3
import sys

from app.adapters.jobs.sqlite_repository import SqliteJobRepository

fixture = pathlib.Path(sys.argv[1])
machine_root = pathlib.Path(sys.argv[2])
machine_root.mkdir(parents=True, exist_ok=False)
database = machine_root / "jobs.sqlite"
with sqlite3.connect(database) as connection:
    connection.executescript(fixture.read_text(encoding="utf-8"))
    before = connection.execute("pragma user_version").fetchone()[0]

repository = SqliteJobRepository.open(machine_root)
binding = repository.get_job_execution_binding("job_legacy_release_fixture")
job = repository.get_job("job_legacy_release_fixture")
result = repository.get_job_result("job_legacy_release_fixture")
attempts = repository.list_attempts("job_legacy_release_fixture")
events = repository.list_events("job_legacy_release_fixture")
checkpoint = repository.latest_checkpoint("job_legacy_release_fixture", "publish")
source_identity = repository.read_source_identity_candidate(
    "fixture", "legacy-release-source"
)
with sqlite3.connect(database) as connection:
    after = connection.execute("pragma user_version").fetchone()[0]
    integrity = connection.execute("pragma integrity_check").fetchone()[0]
    foreign_key_errors = len(connection.execute("pragma foreign_key_check").fetchall())

reopened = SqliteJobRepository.open(machine_root)
reopened_binding = reopened.get_job_execution_binding("job_legacy_release_fixture")
reopened_data_preserved = (
    reopened.get_job_result("job_legacy_release_fixture") == result
    and reopened.list_attempts("job_legacy_release_fixture") == attempts
    and reopened.list_events("job_legacy_release_fixture") == events
    and reopened.latest_checkpoint("job_legacy_release_fixture", "publish")
        == checkpoint
    and reopened.read_source_identity_candidate(
        "fixture", "legacy-release-source"
    ) == source_identity
)
print(json.dumps({
    "schema_before": before,
    "schema_after": after,
    "integrity": integrity,
    "foreign_key_errors": foreign_key_errors,
    "job_id": job.job_id,
    "job_state": job.state.value,
    "result_bundle_id": result.bundle_id if result is not None else None,
    "result_quality": (
        result.quality_overall.value if result is not None else None
    ),
    "attempt_states": [attempt.state.value for attempt in attempts],
    "event_types": [event.event_type for event in events],
    "checkpoint_id": checkpoint.checkpoint_id if checkpoint is not None else None,
    "source_identity_preserved": source_identity is not None,
    "binding": {
        "recipe_id": binding.recipe_id,
        "recipe_version": binding.recipe_version,
        "executor_id": binding.executor_id,
        "executor_version": binding.executor_version,
        "pack_id": binding.pack_id,
        "pack_version": binding.pack_version,
    },
    "reopened_same_binding": reopened_binding == binding,
    "reopened_data_preserved": reopened_data_preserved,
}, sort_keys=True))
"""


def _hash(path: Path, algorithm: str = "sha256") -> str:
    digest = hashlib.new(algorithm)
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise ReleaseError(f"cannot read release input: {path.name}") from error
    return digest.hexdigest()


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_bytes(_canonical_json(payload))


def _load_lock(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseError("Runtime release lock is invalid") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ReleaseError("Runtime release lock is incompatible")
    if payload.get("platform") != "windows-x86_64":
        raise ReleaseError("Runtime release lock has the wrong platform")
    return payload


def _legacy_jobstore_fixture(
    lock_path: Path,
    lock: Mapping[str, Any],
) -> Path:
    fixture = lock.get("legacy_jobstore_fixture")
    if (
        not isinstance(fixture, dict)
        or set(fixture) != {"path", "sha256", "schema_version", "source_commit"}
        or not isinstance(fixture.get("path"), str)
        or not isinstance(fixture.get("sha256"), str)
        or type(fixture.get("schema_version")) is not int
        or fixture["schema_version"] != 1
        or not isinstance(fixture.get("source_commit"), str)
        or re.fullmatch(r"[0-9a-f]{40}", fixture["source_commit"]) is None
    ):
        raise ReleaseError("Runtime release lock is missing the legacy JobStore fixture")
    relative = fixture["path"]
    candidate = PurePosixPath(relative)
    if (
        "\\" in relative
        or ":" in relative
        or candidate.is_absolute()
        or any(part in ("", ".", "..") for part in candidate.parts)
    ):
        raise ReleaseError("legacy JobStore fixture path is invalid")
    path = _ordinary_file(
        lock_path.parent.joinpath(*candidate.parts),
        "legacy JobStore fixture",
    )
    if _hash(path) != fixture["sha256"]:
        raise ReleaseError("legacy JobStore fixture hash does not match the release lock")
    return path


def _ordinary_file(path: Path, label: str) -> Path:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ReleaseError(f"{label} is unavailable") from error
    attributes = getattr(metadata, "st_file_attributes", 0)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        or metadata.st_nlink != 1
    ):
        raise ReleaseError(f"{label} must be an ordinary single-link file")
    return path.resolve(strict=True)


def _ordinary_directory(path: Path, label: str) -> Path:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ReleaseError(f"{label} is unavailable") from error
    attributes = getattr(metadata, "st_file_attributes", 0)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ReleaseError(f"{label} must be an ordinary directory")
    return path.resolve(strict=True)


def _archive_members(archive_path: Path) -> tuple[zipfile.ZipInfo, ...]:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = tuple(archive.infolist())
    except (OSError, zipfile.BadZipFile) as error:
        raise ReleaseError(f"archive is invalid: {archive_path.name}") from error
    if not members or len(members) > _MAX_ARCHIVE_ENTRIES:
        raise ReleaseError(f"archive entry count is invalid: {archive_path.name}")
    total = 0
    seen: set[str] = set()
    for member in members:
        name = member.filename.removesuffix("/") if member.is_dir() else member.filename
        pure = PurePosixPath(name)
        folded = name.casefold()
        unix_mode = member.external_attr >> 16
        if (
            not name
            or "\\" in name
            or ":" in name
            or pure.is_absolute()
            or any(part in ("", ".", "..") for part in pure.parts)
            or folded in seen
            or (unix_mode and stat.S_IFMT(unix_mode) == stat.S_IFLNK)
        ):
            raise ReleaseError(f"archive contains an unsafe entry: {member.filename}")
        seen.add(folded)
        if member.file_size > _MAX_ARCHIVE_FILE_BYTES:
            raise ReleaseError(f"archive entry is too large: {member.filename}")
        total += member.file_size
        if total > _MAX_ARCHIVE_BYTES:
            raise ReleaseError(f"archive is too large: {archive_path.name}")
    return members


def _extract_archive(archive_path: Path, destination: Path) -> None:
    members = _archive_members(archive_path)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for member in members:
                target = destination.joinpath(*PurePosixPath(member.filename).parts)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise ReleaseError(f"cannot extract archive: {archive_path.name}") from error


def _extract_sqlite_library(archive_path: Path, destination: Path) -> None:
    members = _archive_members(archive_path)
    candidates = [member for member in members if member.filename == "sqlite3.dll"]
    if len(candidates) != 1:
        raise ReleaseError("SQLite archive must contain exactly one sqlite3.dll")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            with archive.open(candidates[0]) as source, destination.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise ReleaseError("cannot extract sqlite3.dll") from error


def _verify_inputs(
    lock: Mapping[str, Any],
    inputs: Path,
    wheelhouse: Path,
) -> tuple[Path, Path, Path, tuple[dict[str, Any], ...]]:
    python = lock.get("python")
    sqlite = lock.get("sqlite")
    wheels = lock.get("wheels")
    if not isinstance(python, dict) or not isinstance(sqlite, dict) or not isinstance(wheels, list):
        raise ReleaseError("Runtime release lock is incomplete")
    python_archive = _ordinary_file(inputs / str(python.get("archive")), "CPython archive")
    python_spdx = _ordinary_file(inputs / str(python.get("spdx")), "CPython SPDX")
    sqlite_archive = _ordinary_file(inputs / str(sqlite.get("archive")), "SQLite archive")
    if _hash(python_archive) != python.get("sha256"):
        raise ReleaseError("CPython archive hash does not match the release lock")
    if _hash(python_spdx) != python.get("spdx_sha256"):
        raise ReleaseError("CPython SPDX hash does not match the release lock")
    if _hash(sqlite_archive, "sha3_256") != sqlite.get("sha3_256"):
        raise ReleaseError("SQLite archive hash does not match the release lock")
    expected: dict[str, dict[str, Any]] = {}
    for item in wheels:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            raise ReleaseError("Runtime wheel lock is invalid")
        if item["filename"] in expected:
            raise ReleaseError("Runtime wheel lock contains a duplicate filename")
        expected[item["filename"]] = item
    actual = {path.name: path for path in wheelhouse.glob("*.whl")}
    if set(actual) != set(expected):
        raise ReleaseError("Runtime wheelhouse does not match the release lock")
    verified: list[dict[str, Any]] = []
    for name in sorted(expected):
        path = _ordinary_file(actual[name], "Runtime wheel")
        item = expected[name]
        if path.stat().st_size != item.get("byte_length") or _hash(path) != item.get("sha256"):
            raise ReleaseError(f"Runtime wheel hash does not match the release lock: {name}")
        verified.append(dict(item))
    return python_archive, python_spdx, sqlite_archive, tuple(verified)


def _run(arguments: Sequence[str], *, environment: Mapping[str, str], timeout: int) -> str:
    try:
        result = subprocess.run(
            list(arguments),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=dict(environment),
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ReleaseError("Runtime release subprocess failed") from error
    if result.returncode != 0:
        summary = (result.stderr or result.stdout).strip()[-1000:]
        raise ReleaseError(f"Runtime release subprocess failed: {summary}")
    return result.stdout.strip()


def _clean_environment(base: Mapping[str, str]) -> dict[str, str]:
    environment = {
        key: value
        for key, value in base.items()
        if key.upper() not in {"PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"}
        and not key.upper().startswith("PIP_")
    }
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }
    )
    return environment


def _locked_wheel_install_arguments(
    builder_python: Path,
    site_packages: Path,
    wheelhouse: Path,
    wheels: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    return (
        str(builder_python),
        "-m",
        "pip",
        "--isolated",
        "install",
        "--disable-pip-version-check",
        "--no-index",
        "--no-deps",
        "--no-compile",
        "--target",
        str(site_packages),
        *(str(wheelhouse / str(item["filename"])) for item in wheels),
    )


def _remove_direct_url_metadata(site_packages: Path, expected_count: int) -> None:
    metadata_files = sorted(site_packages.glob("*.dist-info/direct_url.json"))
    if len(metadata_files) != expected_count:
        raise ReleaseError("pip direct URL metadata does not match the locked wheels")
    for metadata_file in metadata_files:
        metadata_file = _ordinary_file(metadata_file, "pip direct URL metadata")
        record = _ordinary_file(metadata_file.parent / "RECORD", "wheel RECORD")
        relative = metadata_file.relative_to(site_packages).as_posix()
        try:
            rows = list(csv.reader(record.read_text(encoding="utf-8").splitlines()))
        except (OSError, UnicodeError, csv.Error) as error:
            raise ReleaseError("wheel RECORD is invalid") from error
        if any(len(row) != 3 for row in rows):
            raise ReleaseError("wheel RECORD is invalid")
        retained = [row for row in rows if row[0] != relative]
        if len(retained) != len(rows) - 1:
            raise ReleaseError("wheel RECORD does not bind direct URL metadata")
        output = io.StringIO(newline="")
        csv.writer(output, lineterminator="\n").writerows(retained)
        metadata_file.unlink()
        record.write_text(output.getvalue(), encoding="utf-8", newline="")
    if any(site_packages.rglob("direct_url.json")):
        raise ReleaseError("pip direct URL metadata remains in the Runtime")


def _remove_pip_console_scripts(site_packages: Path) -> None:
    scripts_root = _ordinary_directory(site_packages / "bin", "pip console scripts")
    scripts = {
        path.name: _ordinary_file(path, "pip console script")
        for path in scripts_root.iterdir()
    }
    if set(scripts) != _PIP_CONSOLE_SCRIPTS:
        raise ReleaseError("pip console scripts do not match the Runtime lock")
    record_rows: dict[Path, list[list[str]]] = {}
    matched: dict[str, int] = {name: 0 for name in scripts}
    for record in sorted(site_packages.glob("*.dist-info/RECORD")):
        record = _ordinary_file(record, "wheel RECORD")
        try:
            rows = list(csv.reader(record.read_text(encoding="utf-8").splitlines()))
        except (OSError, UnicodeError, csv.Error) as error:
            raise ReleaseError("wheel RECORD is invalid") from error
        if any(len(row) != 3 for row in rows):
            raise ReleaseError("wheel RECORD is invalid")
        retained: list[list[str]] = []
        for row in rows:
            name = PurePosixPath(row[0]).name
            if row[0].startswith("../../bin/") and name in matched:
                matched[name] += 1
            else:
                retained.append(row)
        record_rows[record] = retained
    if any(count != 1 for count in matched.values()):
        raise ReleaseError("wheel RECORD does not bind the pip console scripts")
    for script in scripts.values():
        script.unlink()
    scripts_root.rmdir()
    for record, rows in record_rows.items():
        output = io.StringIO(newline="")
        csv.writer(output, lineterminator="\n").writerows(rows)
        record.write_text(output.getvalue(), encoding="utf-8", newline="")


def _probe_runtime(root: Path, lock: Mapping[str, Any], environment: Mapping[str, str]) -> dict[str, Any]:
    python_executable = root / "python.exe"
    output = _run(
        (str(python_executable), "-I", "-B", "-c", _SQLITE_PROBE),
        environment=environment,
        timeout=30,
    )
    try:
        probe = json.loads(output)
    except json.JSONDecodeError as error:
        raise ReleaseError("Runtime identity probe returned invalid JSON") from error
    expected_python = lock["python"]["version"]
    expected_sqlite = lock["sqlite"]["version"]
    expected_source_id = lock["sqlite"]["source_id"]
    if (
        probe.get("python_version") != expected_python
        or probe.get("implementation") != "CPython"
        or probe.get("platform") != "win32"
        or probe.get("machine") != "AMD64"
        or probe.get("pointer_bits") != 64
        or probe.get("sqlite_version") != expected_sqlite
        or probe.get("sqlite_source_id") != expected_source_id
        or probe.get("sqlite_threadsafety") != 3
    ):
        raise ReleaseError("Runtime identity does not match the release lock")
    for key in ("python_executable", "python_library", "sqlite_extension", "sqlite_library"):
        candidate = Path(str(probe[key])).resolve(strict=True)
        try:
            probe[key] = candidate.relative_to(root).as_posix()
        except ValueError as error:
            raise ReleaseError(f"Runtime loaded {key} outside the bundle") from error
    if probe["sqlite_library_sha256"] != _hash(root / probe["sqlite_library"]):
        raise ReleaseError("Loaded SQLite library hash does not match the bundle")
    return probe


def _run_cli(
    root: Path,
    arguments: Sequence[str],
    environment: Mapping[str, str],
    *,
    timeout: int = 30,
) -> dict[str, Any]:
    output = _run(
        (
            str(root / "python.exe"),
            "-I",
            "-B",
            str(root / "alltonote.py"),
            *arguments,
            "--json",
        ),
        environment=environment,
        timeout=timeout,
    )
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise ReleaseError("Runtime CLI returned invalid JSON") from error
    if payload.get("ok") is not True:
        raise ReleaseError(f"Runtime CLI Gate failed: {' '.join(arguments)}")
    return payload


def _run_legacy_jobstore_migration(
    root: Path,
    fixture: Path,
    machine_root: Path,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    output = _run(
        (
            str(root / "python.exe"),
            "-I",
            "-B",
            "-c",
            _LEGACY_JOB_STORE_PROBE,
            str(fixture),
            str(machine_root),
        ),
        environment=environment,
        timeout=30,
    )
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise ReleaseError("legacy JobStore Gate returned invalid JSON") from error
    expected_binding = {
        "recipe_id": "alltonote.video-course-note",
        "recipe_version": 2,
        "executor_id": "alltonote.video",
        "executor_version": 1,
        "pack_id": "media-basic",
        "pack_version": "legacy-v1",
    }
    if payload != {
        "schema_before": 1,
        "schema_after": 2,
        "integrity": "ok",
        "foreign_key_errors": 0,
        "job_id": "job_legacy_release_fixture",
        "job_state": "succeeded",
        "result_bundle_id": "bnd_018cc251-f400-7000-8000-000000000005",
        "result_quality": "pass",
        "attempt_states": ["succeeded"],
        "event_types": ["portable.commit.completed.v1"],
        "checkpoint_id": "chk_legacy_release_fixture",
        "source_identity_preserved": True,
        "binding": expected_binding,
        "reopened_same_binding": True,
        "reopened_data_preserved": True,
    }:
        raise ReleaseError("legacy JobStore Gate did not preserve the release contract")
    return payload


def _file_manifest(root: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative == "release/file-manifest.json":
            continue
        rows.append(
            {
                "path": relative,
                "byte_length": path.stat().st_size,
                "sha256": _hash(path),
            }
        )
    return {
        "schema_version": 1,
        "hash_algorithm": "sha256",
        "scope": "all ordinary files except release/file-manifest.json",
        "file_count": len(rows),
        "files": rows,
    }


def assemble_runtime(
    *,
    lock_path: Path,
    inputs: Path,
    wheelhouse: Path,
    builder_python: Path,
    output: Path,
    gate_root: Path,
) -> dict[str, Any]:
    lock_path = _ordinary_file(lock_path, "Runtime release lock")
    lock = _load_lock(lock_path)
    legacy_jobstore_fixture = _legacy_jobstore_fixture(lock_path, lock)
    inputs = _ordinary_directory(inputs, "Runtime input directory")
    wheelhouse = _ordinary_directory(wheelhouse, "Runtime wheelhouse")
    gate_root = _ordinary_directory(gate_root, "Runtime Gate root")
    builder_python = _ordinary_file(builder_python, "Builder Python")
    output = output.resolve(strict=False)
    parent = _ordinary_directory(output.parent, "Runtime output parent")
    if os.path.lexists(output):
        raise ReleaseError("Runtime output already exists")
    for source in (
        inputs,
        wheelhouse,
        gate_root,
        builder_python,
        legacy_jobstore_fixture,
    ):
        try:
            source.relative_to(output)
            overlaps = True
        except ValueError:
            try:
                output.relative_to(source)
                overlaps = True
            except ValueError:
                overlaps = False
        if overlaps:
            raise ReleaseError("Runtime output overlaps a release input")
    python_archive, python_spdx, sqlite_archive, wheels = _verify_inputs(
        lock, inputs, wheelhouse
    )
    stage = parent / f".{output.name}.staging-{uuid.uuid4().hex}"
    stage.mkdir(mode=0o700)
    published = False
    environment = _clean_environment(os.environ)
    try:
        _extract_archive(python_archive, stage)
        sqlite_target = stage / "sqlite3.dll"
        sqlite_target.unlink()
        _extract_sqlite_library(sqlite_archive, sqlite_target)
        site_packages = stage / "site-packages"
        site_packages.mkdir()
        _run(
            _locked_wheel_install_arguments(
                builder_python,
                site_packages,
                wheelhouse,
                wheels,
            ),
            environment=environment,
            timeout=_PROCESS_TIMEOUT_SECONDS,
        )
        _remove_direct_url_metadata(site_packages, len(wheels))
        _remove_pip_console_scripts(site_packages)
        (stage / "python314._pth").write_text(
            "python314.zip\n.\nsite-packages\nimport site\n",
            encoding="ascii",
            newline="\n",
        )
        (stage / "alltonote.py").write_text(
            'from app.cli.main import entrypoint\n\nif __name__ == "__main__":\n    entrypoint()\n',
            encoding="utf-8",
            newline="\n",
        )
        (stage / "alltonote.cmd").write_text(
            '@echo off\n"%~dp0python.exe" -I -B "%~dp0alltonote.py" %*\n',
            encoding="ascii",
            newline="\r\n",
        )
        release = stage / "release"
        release.mkdir()
        shutil.copyfile(python_spdx, release / "python-3.14.6.spdx.json")
        probe = _probe_runtime(stage, lock, environment)
        with tempfile.TemporaryDirectory(prefix="alltonote-runtime-smoke-", dir=gate_root) as temporary:
            smoke = Path(temporary)
            isolated_environment = dict(environment)
            isolated_environment["LOCALAPPDATA"] = str(smoke / "local")
            isolated_environment["APPDATA"] = str(smoke / "roaming")
            _run_cli(stage, ("version",), isolated_environment)
            info = _run_cli(stage, ("runtime", "info"), isolated_environment)
            _run_cli(stage, ("runtime", "doctor"), isolated_environment)
            _run_cli(
                stage,
                (
                    "workspace",
                    "init",
                    str(smoke / "Workspace 中文 有空格"),
                    "--name",
                    "Runtime 候选验收",
                ),
                isolated_environment,
            )
            gate = _run_cli(
                stage,
                ("runtime", "sqlite-wal-gate", "--root", str(smoke)),
                isolated_environment,
                timeout=_PROCESS_TIMEOUT_SECONDS,
            )
            legacy_jobstore = _run_legacy_jobstore_migration(
                stage,
                legacy_jobstore_fixture,
                smoke / "legacy-jobstore",
                isolated_environment,
            )
        storage = info["data"]["storage"]
        gate_data = gate["data"]
        if (
            storage.get("sqlite_version") != lock["sqlite"]["version"]
            or storage.get("sqlite_source_id") != lock["sqlite"]["source_id"]
            or storage.get("parallel_job_execution_supported") is not True
            or gate_data.get("scenarios_passed") is not True
            or gate_data.get("parallel_job_execution_enabled") is not False
        ):
            raise ReleaseError("Runtime Gate report does not match the release contract")
        _write_json(
            release / "runtime-inputs.json",
            {
                "schema_version": 1,
                "runtime_source_commit": lock["runtime_source_commit"],
                "runtime_version": lock["runtime_version"],
                "platform": lock["platform"],
                "python": lock["python"],
                "sqlite": lock["sqlite"],
                "legacy_jobstore_fixture": lock["legacy_jobstore_fixture"],
            },
        )
        _write_json(
            release / "wheelhouse-lock.json",
            {
                "schema_version": 1,
                "python_abi": "cp314-win_amd64",
                "wheel_count": len(wheels),
                "wheels": list(wheels),
            },
        )
        _write_json(
            release / "acceptance.json",
            {
                "schema_version": 1,
                "status": "candidate-pass",
                "runtime_source_commit": lock["runtime_source_commit"],
                "identity": probe,
                "checks": {
                    "version": True,
                    "runtime_info": True,
                    "runtime_doctor": True,
                    "unicode_workspace_init": True,
                    "sqlite_wal_gate": True,
                    "legacy_jobstore_migration": True,
                },
                "wal_gate": {
                    "connection_counts": gate_data["connection_counts"],
                    "scenarios_passed": True,
                    "parallel_job_execution_enabled": False,
                    "integrity": gate_data["integrity"],
                },
                "legacy_jobstore": legacy_jobstore,
                "limits": [
                    "unsigned portable candidate; not a public installer",
                    "clean non-admin VM, Defender, update, rollback, and uninstall remain release gates",
                ],
            },
        )
        _write_json(release / "file-manifest.json", _file_manifest(stage))
        os.replace(stage, output)
        published = True
        return {
            "output": str(output),
            "runtime_source_commit": lock["runtime_source_commit"],
            "python_version": probe["python_version"],
            "sqlite_version": probe["sqlite_version"],
            "sqlite_source_id": probe["sqlite_source_id"],
            "file_count": json.loads(
                (output / "release" / "file-manifest.json").read_text(encoding="utf-8")
            )["file_count"],
            "wheel_count": len(wheels),
            "wal_gate_passed": True,
        }
    finally:
        if not published and stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Assemble the locked Windows Runtime candidate")
    parser.add_argument("--lock", dest="lock_path", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--builder-python", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gate-root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        payload = assemble_runtime(**vars(_parser().parse_args(argv)))
    except ReleaseError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "data": payload}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

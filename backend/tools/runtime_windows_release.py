from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


class ReleaseError(RuntimeError):
    pass


_MAX_ARCHIVE_ENTRIES = 10_000
_MAX_ARCHIVE_FILE_BYTES = 128 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
_PROCESS_TIMEOUT_SECONDS = 180
_ENGINE_START_SAMPLES = 20
_ENGINE_ENSURE_CONCURRENCY = 32
_ENGINE_START_P95_LIMIT_MILLISECONDS = 2000.0
_ENGINE_IDLE_RSS_LIMIT_BYTES = 100 * 1024 * 1024
_MAX_CANDIDATE_CONTROL_BYTES = 16 * 1024 * 1024
_MAX_CANDIDATE_FILES = 100_000
_MAX_CANDIDATE_FILE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_CANDIDATE_BYTES = 8 * 1024 * 1024 * 1024
_FILE_MANIFEST_PATH = "release/file-manifest.json"
_FILE_MANIFEST_SCOPE = "all ordinary files except release/file-manifest.json"
_REQUIRED_ACCEPTANCE_CHECKS = frozenset(
    {
        "version",
        "runtime_info",
        "runtime_doctor",
        "engine_lifecycle",
        "unicode_workspace_init",
        "sqlite_wal_gate",
        "legacy_jobstore_migration",
    }
)
_PIP_CONSOLE_SCRIPTS = {
    "alltonote.exe",
    "cffi-gen-src.exe",
    "iwiki.exe",
    "keyring.exe",
}
_BUILDER_PROBE = r"""
import json
import platform
import struct
import sys

import pip

print(json.dumps({
    "implementation": platform.python_implementation(),
    "version": platform.python_version(),
    "platform": sys.platform,
    "machine": platform.machine(),
    "pointer_bits": struct.calcsize("P") * 8,
    "pip_version": pip.__version__,
}, sort_keys=True))
"""
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

_ENGINE_PROCESS_PROBE = r"""
import ctypes
import json
import os
import signal
import sys

from ctypes import wintypes
from app.engine.instance import EngineInstancePaths, process_start_identity, read_descriptor
from app.runtime_paths import resolve_runtime_paths

instance = EngineInstancePaths.from_runtime_paths(resolve_runtime_paths())
descriptor = read_descriptor(instance.descriptor)
if descriptor is None:
    raise SystemExit("Engine descriptor is absent")
if process_start_identity(descriptor.pid) != descriptor.process_start_identity:
    raise SystemExit("Engine process identity changed")

operation = sys.argv[1]
if operation == "terminate":
    os.kill(descriptor.pid, signal.SIGTERM)
    print(json.dumps({"engine_id": descriptor.engine_id}, sort_keys=True))
elif operation == "inspect":
    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("page_fault_count", wintypes.DWORD),
            ("peak_working_set_size", ctypes.c_size_t),
            ("working_set_size", ctypes.c_size_t),
            ("quota_peak_paged_pool_usage", ctypes.c_size_t),
            ("quota_paged_pool_usage", ctypes.c_size_t),
            ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
            ("quota_non_paged_pool_usage", ctypes.c_size_t),
            ("pagefile_usage", ctypes.c_size_t),
            ("peak_pagefile_usage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    psapi.GetProcessMemoryInfo.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ProcessMemoryCounters),
        wintypes.DWORD,
    )
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x0410, False, descriptor.pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(
            handle,
            ctypes.byref(counters),
            ctypes.sizeof(counters),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)
    print(json.dumps({
        "engine_id": descriptor.engine_id,
        "rss_bytes": counters.working_set_size,
    }, sort_keys=True))
else:
    raise SystemExit("Unknown Engine probe operation")
"""

_ENGINE_IDLE_PROBE = r"""
import json

from app.engine.host import run_engine_host
from app.engine.instance import EngineInstancePaths
from app.runtime_paths import resolve_runtime_paths

paths = resolve_runtime_paths()
instance = EngineInstancePaths.from_runtime_paths(paths)
exit_code = run_engine_host(
    paths=paths,
    idle_seconds=0.25,
)
print(json.dumps({
    "descriptor_exists": instance.descriptor.exists(),
    "exit_code": exit_code,
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


def _run(
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str],
    timeout: int,
    label: str,
) -> str:
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
        raise ReleaseError(f"{label} failed") from error
    if result.returncode != 0:
        summary = (result.stderr or result.stdout).strip()[-1000:]
        raise ReleaseError(f"{label} failed: {summary}")
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


def _builder_pip_tree_sha256(builder_root: Path, pip_version: str) -> str:
    root = _ordinary_directory(builder_root, "Builder root")
    site_packages = _ordinary_directory(
        root / "Lib" / "site-packages",
        "Builder site-packages",
    )
    pip_root = _ordinary_directory(site_packages / "pip", "Builder pip package")
    dist_name = f"pip-{pip_version}.dist-info"
    pip_dist = _ordinary_directory(site_packages / dist_name, "Builder pip metadata")
    try:
        discovered = {
            path.name
            for path in site_packages.iterdir()
            if path.name.casefold().startswith("pip-")
            and path.name.casefold().endswith(".dist-info")
        }
    except OSError as error:
        raise ReleaseError("Builder toolchain is unavailable") from error
    if discovered != {dist_name}:
        raise ReleaseError("Builder toolchain pip metadata is ambiguous")
    rows: list[dict[str, Any]] = []
    for prefix, directory in (("pip", pip_root), (dist_name, pip_dist)):
        for row in _candidate_file_rows(directory):
            rows.append({**row, "path": f"{prefix}/{row['path']}"})
    rows.sort(key=lambda row: row["path"])
    return hashlib.sha256(
        _canonical_json({"schema_version": 1, "files": rows})
    ).hexdigest()


def _builder_tree_sha256(builder_root: Path) -> str:
    root = _ordinary_directory(builder_root, "Builder root")
    rows = _candidate_file_rows(root)
    return hashlib.sha256(
        _canonical_json({"schema_version": 1, "files": rows})
    ).hexdigest()


def _verify_builder_toolchain(
    lock: Mapping[str, Any],
    builder_python: Path,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    builder = lock.get("builder")
    expected_keys = {
        "implementation",
        "version",
        "platform",
        "machine",
        "pointer_bits",
        "python_executable_sha256",
        "python_runtime_dll_sha256",
        "pip_version",
        "pip_tree_sha256",
        "builder_tree_sha256",
    }
    if (
        type(builder) is not dict
        or set(builder) != expected_keys
        or builder.get("implementation") != "CPython"
        or type(builder.get("version")) is not str
        or builder.get("platform") != "win32"
        or builder.get("machine") != "AMD64"
        or type(builder.get("pointer_bits")) is not int
        or builder.get("pointer_bits") != 64
        or type(builder.get("pip_version")) is not str
        or any(
            re.fullmatch(r"[0-9a-f]{64}", builder.get(name, "")) is None
            for name in (
                "python_executable_sha256",
                "python_runtime_dll_sha256",
                "pip_tree_sha256",
                "builder_tree_sha256",
            )
        )
    ):
        raise ReleaseError("Builder toolchain is missing from the Runtime release lock")
    python_executable = _ordinary_file(builder_python, "Builder Python")
    runtime_dll = _ordinary_file(
        python_executable.parent / "python314.dll",
        "Builder Python runtime",
    )
    if (
        _hash(python_executable) != builder["python_executable_sha256"]
        or _hash(runtime_dll) != builder["python_runtime_dll_sha256"]
        or _builder_pip_tree_sha256(
            python_executable.parent,
            builder["pip_version"],
        )
        != builder["pip_tree_sha256"]
        or _builder_tree_sha256(python_executable.parent)
        != builder["builder_tree_sha256"]
    ):
        raise ReleaseError("Builder toolchain does not match the Runtime release lock")
    try:
        output = _run(
            (str(python_executable), "-I", "-B", "-c", _BUILDER_PROBE),
            environment=environment,
            timeout=30,
            label="Builder toolchain probe",
        )
    except ReleaseError as error:
        raise ReleaseError("Builder toolchain probe failed") from error
    try:
        probe = json.loads(
            output,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ReleaseError("Builder toolchain probe returned invalid JSON") from error
    identity_keys = {
        "implementation",
        "version",
        "platform",
        "machine",
        "pointer_bits",
        "pip_version",
    }
    if type(probe) is not dict or set(probe) != identity_keys or any(
        probe.get(name) != builder[name] for name in identity_keys
    ):
        raise ReleaseError("Builder toolchain identity does not match the Runtime release lock")
    return {"schema_version": 1, **builder}


def _locked_wheel_install_arguments(
    builder_python: Path,
    site_packages: Path,
    wheelhouse: Path,
    wheels: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    return (
        str(builder_python),
        "-I",
        "-B",
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


def _record_entries(
    content: bytes,
    record_path: str,
) -> dict[str, tuple[str | None, int | None]]:
    try:
        rows = list(csv.reader(content.decode("utf-8").splitlines()))
    except (UnicodeError, csv.Error) as error:
        raise ReleaseError("Runtime wheel installation RECORD is invalid") from error
    entries: dict[str, tuple[str | None, int | None]] = {}
    for row in rows:
        if len(row) != 3:
            raise ReleaseError("Runtime wheel installation RECORD is invalid")
        relative = _portable_candidate_path(row[0])
        if relative in entries:
            raise ReleaseError("Runtime wheel installation RECORD contains a duplicate path")
        if relative == record_path:
            if row[1:] != ["", ""]:
                raise ReleaseError("Runtime wheel installation RECORD self-entry is invalid")
            entries[relative] = (None, None)
            continue
        if not row[1].startswith("sha256=") or not row[2].isdigit():
            raise ReleaseError("Runtime wheel installation RECORD entry is invalid")
        encoded = row[1].removeprefix("sha256=")
        try:
            digest = base64.b64decode(
                encoded + "=" * (-len(encoded) % 4),
                altchars=b"-_",
                validate=True,
            )
        except (ValueError, base64.binascii.Error) as error:
            raise ReleaseError("Runtime wheel installation RECORD hash is invalid") from error
        if len(digest) != hashlib.sha256().digest_size:
            raise ReleaseError("Runtime wheel installation RECORD hash is invalid")
        size = int(row[2])
        if str(size) != row[2] or size > _MAX_CANDIDATE_FILE_BYTES:
            raise ReleaseError("Runtime wheel installation RECORD size is invalid")
        entries[relative] = (digest.hex(), size)
    return entries


def _verify_wheel_install(
    site_packages: Path,
    wheelhouse: Path,
    wheels: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    installed_root = _ordinary_directory(site_packages, "Runtime site-packages")
    wheelhouse = _ordinary_directory(wheelhouse, "Runtime wheelhouse")
    expected_installed: set[str] = set()
    expected_installed_rows: dict[str, dict[str, Any]] = {}
    collision_keys: set[str] = set()
    wheel_reports: list[dict[str, Any]] = []
    source_files_verified = 0
    generated_files = 0
    try:
        for wheel_lock in wheels:
            filename = wheel_lock.get("filename")
            if type(filename) is not str:
                raise ReleaseError("Runtime wheel installation lock is invalid")
            wheel = _ordinary_file(wheelhouse / filename, "Runtime wheel")
            if (
                type(wheel_lock.get("byte_length")) is not int
                or wheel.stat().st_size != wheel_lock["byte_length"]
                or _hash(wheel) != wheel_lock.get("sha256")
            ):
                raise ReleaseError("Runtime wheel installation source does not match the lock")
            members = _archive_members(wheel)
            file_members = {member.filename: member for member in members if not member.is_dir()}
            record_paths = [
                path
                for path in file_members
                if path.endswith(".dist-info/RECORD")
                and path.count("/") == 1
            ]
            if len(record_paths) != 1 or any(".data/" in path for path in file_members):
                raise ReleaseError("Runtime wheel installation layout is unsupported")
            record_path = record_paths[0]
            dist_info = record_path.removesuffix("/RECORD")
            with zipfile.ZipFile(wheel) as archive:
                source_record_bytes = archive.read(record_path)
                source_record = _record_entries(source_record_bytes, record_path)
                if set(source_record) != set(file_members):
                    raise ReleaseError("Runtime wheel installation RECORD file set is invalid")
                for relative, member in file_members.items():
                    if relative == record_path:
                        continue
                    content = archive.read(member)
                    digest, size = source_record[relative]
                    if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
                        raise ReleaseError("Runtime wheel installation archive bytes are invalid")

            generated = {
                f"{dist_info}/INSTALLER": b"pip\n",
                f"{dist_info}/REQUESTED": b"",
            }
            if set(generated) & set(source_record):
                raise ReleaseError(
                    "Runtime wheel installation source contains pip generated metadata"
                )
            installed_record_path = _ordinary_file(
                installed_root.joinpath(*record_path.split("/")),
                "installed wheel RECORD",
            )
            installed_record_bytes = installed_record_path.read_bytes()
            installed_record = _record_entries(installed_record_bytes, record_path)
            installed_paths = set(source_record) | set(generated)
            if set(installed_record) != installed_paths:
                raise ReleaseError("Runtime wheel installation contains unexplained RECORD entries")
            for relative, expected in source_record.items():
                if relative == record_path:
                    continue
                if installed_record[relative] != expected:
                    raise ReleaseError("Runtime wheel installation changed a source RECORD entry")
                row = _candidate_file_row(
                    installed_root.joinpath(*relative.split("/")),
                    relative,
                )
                digest, size = expected
                if row["sha256"] != digest or row["byte_length"] != size:
                    raise ReleaseError("Runtime wheel installation changed a locked file")
                expected_installed_rows[relative] = row
            for relative, expected_content in generated.items():
                target = _ordinary_file(
                    installed_root.joinpath(*relative.split("/")),
                    "pip generated metadata",
                )
                if target.read_bytes() != expected_content:
                    raise ReleaseError("Runtime wheel installation generated unexpected metadata")
                row = _candidate_file_row(target, relative)
                if installed_record[relative] != (row["sha256"], row["byte_length"]):
                    raise ReleaseError("Runtime wheel installation RECORD does not bind metadata")
                expected_installed_rows[relative] = row
            expected_installed_rows[record_path] = {
                "path": record_path,
                "byte_length": len(installed_record_bytes),
                "sha256": hashlib.sha256(installed_record_bytes).hexdigest(),
            }
            for relative in installed_paths:
                folded = relative.casefold()
                if folded in collision_keys:
                    raise ReleaseError("Runtime wheel installation has colliding files")
                collision_keys.add(folded)
            expected_installed.update(installed_paths)
            source_count = len(source_record) - 1
            source_files_verified += source_count
            generated_files += len(generated)
            wheel_reports.append(
                {
                    "filename": filename,
                    "archive_sha256": wheel_lock["sha256"],
                    "source_record_sha256": hashlib.sha256(source_record_bytes).hexdigest(),
                    "installed_record_sha256": hashlib.sha256(
                        installed_record_bytes
                    ).hexdigest(),
                    "source_files_verified": source_count,
                    "installed_files_verified": len(installed_paths),
                }
            )
        actual_rows = _candidate_file_rows(installed_root)
        if (
            {row["path"] for row in actual_rows} != expected_installed
            or {row["path"]: row for row in actual_rows} != expected_installed_rows
        ):
            raise ReleaseError("Runtime wheel installation contains unexplained files")
    except ReleaseError as error:
        raise ReleaseError("Runtime wheel installation could not be verified") from error
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as error:
        raise ReleaseError("Runtime wheel installation could not be verified") from error
    return {
        "schema_version": 1,
        "wheel_count": len(wheel_reports),
        "source_files_verified": source_files_verified,
        "installed_files_verified": len(expected_installed),
        "generated_files": generated_files,
        "locked_wheels_sha256": hashlib.sha256(
            _canonical_json({"wheels": list(wheels)})
        ).hexdigest(),
        "installed_tree_sha256": hashlib.sha256(
            _canonical_json({"files": actual_rows})
        ).hexdigest(),
        "wheels": wheel_reports,
    }


def _probe_runtime(root: Path, lock: Mapping[str, Any], environment: Mapping[str, str]) -> dict[str, Any]:
    python_executable = root / "python.exe"
    output = _run(
        (str(python_executable), "-I", "-B", "-c", _SQLITE_PROBE),
        environment=environment,
        timeout=30,
        label="Runtime identity probe",
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
        label="Runtime CLI Gate",
    )
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise ReleaseError("Runtime CLI returned invalid JSON") from error
    if payload.get("ok") is not True:
        raise ReleaseError(f"Runtime CLI Gate failed: {' '.join(arguments)}")
    return payload


def _run_engine_process_probe(
    root: Path,
    environment: Mapping[str, str],
    operation: str,
) -> dict[str, Any]:
    output = _run(
        (
            str(root / "python.exe"),
            "-I",
            "-B",
            "-c",
            _ENGINE_PROCESS_PROBE,
            operation,
        ),
        environment=environment,
        timeout=30,
        label=f"Runtime Engine {operation} probe",
    )
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise ReleaseError("Runtime Engine process probe returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise ReleaseError("Runtime Engine process probe returned invalid data")
    return payload


def _run_engine_idle_probe(
    root: Path,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    output = _run(
        (
            str(root / "python.exe"),
            "-I",
            "-B",
            "-c",
            _ENGINE_IDLE_PROBE,
        ),
        environment=environment,
        timeout=30,
        label="Runtime Engine idle probe",
    )
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise ReleaseError("Runtime Engine idle probe returned invalid JSON") from error
    if payload != {"descriptor_exists": False, "exit_code": 0}:
        raise ReleaseError("Runtime Engine idle shutdown Gate failed")
    return payload


def _engine_status(
    root: Path,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    return _run_cli(root, ("engine", "status"), environment)["data"]


def _require_engine_stopped(status: Mapping[str, Any]) -> None:
    if status.get("running") is not False or status.get("state") != "stopped":
        raise ReleaseError("Runtime Engine did not stop cleanly")


def _run_engine_lifecycle_gate(
    root: Path,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    primary_error: Exception | None = None
    try:
        initial = _engine_status(root, environment)
        if initial.get("running") is True:
            _run_cli(root, ("engine", "stop"), environment)
        elif initial.get("state") != "stopped":
            _run_cli(root, ("engine", "ensure"), environment)
            _run_cli(root, ("engine", "stop"), environment)
        _require_engine_stopped(_engine_status(root, environment))

        cold_samples: list[float] = []
        maximum_rss_bytes = 0
        for _sample in range(_ENGINE_START_SAMPLES):
            started_at = time.perf_counter()
            ensured = _run_cli(root, ("engine", "ensure"), environment)["data"]
            cold_samples.append((time.perf_counter() - started_at) * 1000)
            if ensured.get("running") is not True or ensured.get("started") is not True:
                raise ReleaseError("Runtime Engine cold start Gate is inconsistent")
            repeated = _run_cli(root, ("engine", "ensure"), environment)["data"]
            running = _engine_status(root, environment)
            if (
                repeated.get("started") is not False
                or repeated.get("engine_id") != ensured.get("engine_id")
                or running.get("engine_id") != ensured.get("engine_id")
            ):
                raise ReleaseError("Runtime Engine idempotent ensure Gate failed")
            probe = _run_engine_process_probe(root, environment, "inspect")
            rss_bytes = probe.get("rss_bytes")
            if type(rss_bytes) is not int or rss_bytes <= 0:
                raise ReleaseError("Runtime Engine memory probe is invalid")
            maximum_rss_bytes = max(maximum_rss_bytes, rss_bytes)
            _run_cli(root, ("engine", "stop"), environment)
            _require_engine_stopped(_engine_status(root, environment))

        percentile_index = max(0, (95 * len(cold_samples) + 99) // 100 - 1)
        cold_p95 = sorted(cold_samples)[percentile_index]
        if cold_p95 >= _ENGINE_START_P95_LIMIT_MILLISECONDS:
            raise ReleaseError("Runtime Engine cold-start p95 exceeds the release budget")
        if maximum_rss_bytes >= _ENGINE_IDLE_RSS_LIMIT_BYTES:
            raise ReleaseError("Runtime Engine idle RSS exceeds the release budget")

        with ThreadPoolExecutor(
            max_workers=_ENGINE_ENSURE_CONCURRENCY
        ) as executor:
            concurrent = tuple(
                executor.map(
                    lambda _index: _run_cli(
                        root,
                        ("engine", "ensure"),
                        environment,
                    )["data"],
                    range(_ENGINE_ENSURE_CONCURRENCY),
                )
            )
        engine_ids = {item.get("engine_id") for item in concurrent}
        if (
            len(engine_ids) != 1
            or None in engine_ids
            or sum(item.get("started") is True for item in concurrent) != 1
        ):
            raise ReleaseError("Runtime Engine concurrent ensure Gate failed")
        concurrent_engine_id = next(iter(engine_ids))
        info = _run_cli(root, ("runtime", "info"), environment)
        if info["data"].get("engine") != {
            "supported": True,
            "running": True,
            "state": "running",
        }:
            raise ReleaseError("Runtime Engine runtime-info Gate is inconsistent")

        terminated = _run_engine_process_probe(root, environment, "terminate")
        if terminated.get("engine_id") != concurrent_engine_id:
            raise ReleaseError("Runtime Engine termination probe changed identity")
        replacement: dict[str, Any] | None = None
        recovery_deadline = time.monotonic() + 10
        while time.monotonic() < recovery_deadline:
            if _engine_status(root, environment).get("running") is False:
                try:
                    replacement = _run_cli(
                        root,
                        ("engine", "ensure"),
                        environment,
                    )["data"]
                    break
                except ReleaseError:
                    pass
            time.sleep(0.05)
        if (
            replacement is None
            or replacement.get("started") is not True
            or replacement.get("engine_id") in {None, concurrent_engine_id}
        ):
            raise ReleaseError("Runtime Engine forced-death recovery Gate failed")
        _run_cli(root, ("engine", "stop"), environment)
        _require_engine_stopped(_engine_status(root, environment))

        _run_engine_idle_probe(root, environment)
        _require_engine_stopped(_engine_status(root, environment))
        return {
            "supported": True,
            "parent_exit_reconnect": True,
            "cold_start_samples_milliseconds": [
                round(sample, 3) for sample in cold_samples
            ],
            "cold_start_p95_milliseconds": round(cold_p95, 3),
            "idle_rss_bytes": maximum_rss_bytes,
            "ensure_concurrency": _ENGINE_ENSURE_CONCURRENCY,
            "single_instance": True,
            "forced_death_reensure": True,
            "idle_exit": True,
            "stopped_cleanly": True,
        }
    except Exception as error:
        primary_error = error
        raise
    finally:
        try:
            _run_cli(root, ("engine", "stop"), environment)
        except ReleaseError:
            if primary_error is None:
                raise


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
        label="Legacy JobStore migration probe",
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
        "binding": expected_binding,
        "reopened_same_binding": True,
        "reopened_data_preserved": True,
    }:
        raise ReleaseError("legacy JobStore Gate did not preserve the release contract")
    return payload


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _same_open_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
    )


def _portable_candidate_path(relative: str) -> str:
    pure = PurePosixPath(relative)
    if (
        not relative
        or len(relative) > 1024
        or relative != unicodedata.normalize("NFC", relative)
        or relative.startswith("/")
        or "\\" in relative
        or ":" in relative
        or any(
            part in ("", ".", "..") or part.endswith((" ", "."))
            for part in pure.parts
        )
    ):
        raise ReleaseError("Runtime candidate contains an unsafe path")
    return relative


def _candidate_file_row(path: Path, relative: str) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if (
            _is_link_or_reparse(metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > _MAX_CANDIDATE_FILE_BYTES
        ):
            raise OSError("unsafe_file")
        digest = hashlib.sha256()
        byte_count = 0
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not _same_open_file(metadata, opened):
                raise OSError("file_changed_before_read")
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                byte_count += len(chunk)
                digest.update(chunk)
            finished = os.fstat(stream.fileno())
        if byte_count != metadata.st_size or not _same_open_file(opened, finished):
            raise OSError("file_changed_during_read")
    except OSError as error:
        raise ReleaseError("Runtime candidate contains an unsafe file") from error
    return {
        "path": relative,
        "byte_length": byte_count,
        "sha256": digest.hexdigest(),
    }


def _candidate_file_rows(
    root: Path,
    *,
    exclude_file_manifest: bool = False,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    total_bytes = 0
    entry_count = 0

    def traversal_failed(error: OSError) -> None:
        raise error

    try:
        for current, directories, filenames in os.walk(
            root,
            followlinks=False,
            onerror=traversal_failed,
        ):
            current_path = Path(current)
            directories.sort(key=str.casefold)
            filenames.sort(key=str.casefold)
            for name in (*directories, *filenames):
                entry_count += 1
                if entry_count > _MAX_CANDIDATE_FILES:
                    raise OSError("entry_limit")
                target = current_path / name
                metadata = target.lstat()
                if _is_link_or_reparse(metadata):
                    raise OSError("unsafe_entry")
                if name in directories and not stat.S_ISDIR(metadata.st_mode):
                    raise OSError("unsafe_directory")
            for filename in filenames:
                target = current_path / filename
                relative = _portable_candidate_path(
                    target.relative_to(root).as_posix()
                )
                folded = relative.casefold()
                if folded in seen:
                    raise OSError("path_collision")
                seen.add(folded)
                if exclude_file_manifest and relative == _FILE_MANIFEST_PATH:
                    continue
                row = _candidate_file_row(target, relative)
                total_bytes += row["byte_length"]
                if total_bytes > _MAX_CANDIDATE_BYTES:
                    raise OSError("byte_limit")
                rows.append(row)
    except ReleaseError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise ReleaseError("Runtime candidate contains unsafe filesystem entries") from error
    rows.sort(key=lambda row: row["path"])
    return rows


def _file_manifest(root: Path) -> dict[str, Any]:
    rows = _candidate_file_rows(root, exclude_file_manifest=True)
    return {
        "schema_version": 1,
        "hash_algorithm": "sha256",
        "scope": _FILE_MANIFEST_SCOPE,
        "file_count": len(rows),
        "files": rows,
    }


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate JSON member")
        payload[key] = value
    return payload


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _read_candidate_control(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        metadata = path.lstat()
        if (
            _is_link_or_reparse(metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > _MAX_CANDIDATE_CONTROL_BYTES
        ):
            raise OSError("unsafe_control")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not _same_open_file(metadata, opened):
                raise OSError("control_changed_before_read")
            content = stream.read(_MAX_CANDIDATE_CONTROL_BYTES + 1)
            finished = os.fstat(stream.fileno())
        if (
            len(content) > _MAX_CANDIDATE_CONTROL_BYTES
            or len(content) != finished.st_size
            or not _same_open_file(opened, finished)
        ):
            raise OSError("control_changed_during_read")
        payload = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise ReleaseError(f"Runtime candidate {label} is invalid") from error
    if type(payload) is not dict or content != _canonical_json(payload):
        raise ReleaseError(f"Runtime candidate {label} is invalid")
    return payload, content


def _manifest_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    if (
        set(payload) != {
            "schema_version",
            "hash_algorithm",
            "scope",
            "file_count",
            "files",
        }
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != 1
        or payload.get("hash_algorithm") != "sha256"
        or payload.get("scope") != _FILE_MANIFEST_SCOPE
        or type(payload.get("file_count")) is not int
        or type(payload.get("files")) is not list
        or payload["file_count"] != len(payload["files"])
        or payload["file_count"] > _MAX_CANDIDATE_FILES
    ):
        raise ReleaseError("Runtime candidate file manifest is incompatible")
    rows: list[dict[str, Any]] = []
    previous = ""
    seen: set[str] = set()
    for item in payload["files"]:
        if (
            type(item) is not dict
            or set(item) != {"path", "byte_length", "sha256"}
            or type(item.get("path")) is not str
            or type(item.get("byte_length")) is not int
            or item["byte_length"] < 0
            or item["byte_length"] > _MAX_CANDIDATE_FILE_BYTES
            or type(item.get("sha256")) is not str
            or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None
        ):
            raise ReleaseError("Runtime candidate file manifest is incompatible")
        relative = _portable_candidate_path(item["path"])
        folded = relative.casefold()
        if relative == _FILE_MANIFEST_PATH or folded in seen or relative <= previous:
            raise ReleaseError("Runtime candidate file manifest is incompatible")
        seen.add(folded)
        previous = relative
        rows.append(item)
    if sum(item["byte_length"] for item in rows) > _MAX_CANDIDATE_BYTES:
        raise ReleaseError("Runtime candidate file manifest is incompatible")
    return rows


def _valid_sha256(value: object) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validate_candidate_build_attestations(
    builder: Mapping[str, Any],
    wheel_install: Mapping[str, Any],
    wheelhouse: Mapping[str, Any],
) -> None:
    builder_keys = {
        "schema_version",
        "implementation",
        "version",
        "platform",
        "machine",
        "pointer_bits",
        "python_executable_sha256",
        "python_runtime_dll_sha256",
        "pip_version",
        "pip_tree_sha256",
        "builder_tree_sha256",
    }
    if (
        set(builder) != builder_keys
        or type(builder.get("schema_version")) is not int
        or builder.get("schema_version") != 1
        or builder.get("implementation") != "CPython"
        or type(builder.get("version")) is not str
        or not builder["version"]
        or builder.get("platform") != "win32"
        or builder.get("machine") != "AMD64"
        or type(builder.get("pointer_bits")) is not int
        or builder.get("pointer_bits") != 64
        or type(builder.get("pip_version")) is not str
        or not builder["pip_version"]
        or any(
            not _valid_sha256(builder.get(name))
            for name in (
                "python_executable_sha256",
                "python_runtime_dll_sha256",
                "pip_tree_sha256",
                "builder_tree_sha256",
            )
        )
    ):
        raise ReleaseError("Runtime candidate build provenance is invalid")

    locked_wheels = wheelhouse.get("wheels")
    if type(locked_wheels) is not list or any(
        type(item) is not dict
        or set(item) != {"filename", "byte_length", "sha256"}
        or type(item.get("filename")) is not str
        or PurePosixPath(item["filename"]).name != item["filename"]
        or type(item.get("byte_length")) is not int
        or item["byte_length"] < 0
        or not _valid_sha256(item.get("sha256"))
        for item in locked_wheels
    ):
        raise ReleaseError("Runtime candidate build provenance is invalid")
    wheel_reports = wheel_install.get("wheels")
    wheel_keys = {
        "filename",
        "archive_sha256",
        "source_record_sha256",
        "installed_record_sha256",
        "source_files_verified",
        "installed_files_verified",
    }
    if (
        set(wheel_install)
        != {
            "schema_version",
            "wheel_count",
            "source_files_verified",
            "installed_files_verified",
            "generated_files",
            "locked_wheels_sha256",
            "installed_tree_sha256",
            "wheels",
        }
        or type(wheel_install.get("schema_version")) is not int
        or wheel_install.get("schema_version") != 1
        or type(wheel_install.get("wheel_count")) is not int
        or type(wheel_reports) is not list
        or wheel_install["wheel_count"] != len(wheel_reports)
        or wheel_install["wheel_count"] != len(locked_wheels)
        or any(
            type(wheel_install.get(name)) is not int
            or wheel_install[name] < 0
            for name in (
                "source_files_verified",
                "installed_files_verified",
                "generated_files",
            )
        )
        or not _valid_sha256(wheel_install.get("locked_wheels_sha256"))
        or not _valid_sha256(wheel_install.get("installed_tree_sha256"))
        or wheel_install["locked_wheels_sha256"]
        != hashlib.sha256(
            _canonical_json({"wheels": locked_wheels})
        ).hexdigest()
    ):
        raise ReleaseError("Runtime candidate build provenance is invalid")
    for report, locked in zip(wheel_reports, locked_wheels, strict=True):
        if (
            type(report) is not dict
            or set(report) != wheel_keys
            or report.get("filename") != locked["filename"]
            or report.get("archive_sha256") != locked["sha256"]
            or not _valid_sha256(report.get("source_record_sha256"))
            or not _valid_sha256(report.get("installed_record_sha256"))
            or type(report.get("source_files_verified")) is not int
            or type(report.get("installed_files_verified")) is not int
            or report["source_files_verified"] < 0
            or report["installed_files_verified"]
            != report["source_files_verified"] + 3
        ):
            raise ReleaseError("Runtime candidate build provenance is invalid")
    if (
        wheel_install["source_files_verified"]
        != sum(report["source_files_verified"] for report in wheel_reports)
        or wheel_install["installed_files_verified"]
        != sum(report["installed_files_verified"] for report in wheel_reports)
        or wheel_install["generated_files"] != len(wheel_reports) * 2
    ):
        raise ReleaseError("Runtime candidate build provenance is invalid")


def verify_runtime_candidate(
    candidate: Path,
    *,
    expected_manifest_sha256: str,
    expected_source_commit: str | None = None,
) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha256) is None:
        raise ReleaseError("Runtime expected manifest hash is invalid")
    if (
        expected_source_commit is not None
        and re.fullmatch(r"[0-9a-f]{40}", expected_source_commit) is None
    ):
        raise ReleaseError("Runtime expected source commit is invalid")
    root = _ordinary_directory(candidate, "Runtime candidate")
    manifest, manifest_bytes = _read_candidate_control(
        root / "release" / "file-manifest.json",
        "file manifest",
    )
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if manifest_sha256 != expected_manifest_sha256:
        raise ReleaseError("Runtime expected manifest hash does not match the candidate")
    expected_rows = _manifest_rows(manifest)
    actual_rows = _candidate_file_rows(root, exclude_file_manifest=True)
    if actual_rows != expected_rows:
        raise ReleaseError("Runtime candidate files do not match the manifest")

    runtime_inputs, runtime_inputs_bytes = _read_candidate_control(
        root / "release" / "runtime-inputs.json",
        "Runtime inputs metadata",
    )
    wheelhouse, wheelhouse_bytes = _read_candidate_control(
        root / "release" / "wheelhouse-lock.json",
        "wheelhouse metadata",
    )
    acceptance, acceptance_bytes = _read_candidate_control(
        root / "release" / "acceptance.json",
        "acceptance metadata",
    )
    actual_by_path = {item["path"]: item for item in actual_rows}
    for relative, content in (
        ("release/runtime-inputs.json", runtime_inputs_bytes),
        ("release/wheelhouse-lock.json", wheelhouse_bytes),
        ("release/acceptance.json", acceptance_bytes),
    ):
        row = actual_by_path.get(relative)
        if (
            row is None
            or row["byte_length"] != len(content)
            or row["sha256"] != hashlib.sha256(content).hexdigest()
        ):
            raise ReleaseError("Runtime candidate files changed during verification")
    build_provenance = runtime_inputs.get("build_provenance")
    acceptance_provenance = acceptance.get("build_provenance")
    if build_provenance is not None or acceptance_provenance is not None:
        provenance_keys = {
            "builder_toolchain_sha256",
            "wheel_install_attestation_sha256",
        }
        if (
            type(build_provenance) is not dict
            or set(build_provenance) != provenance_keys
            or acceptance_provenance != build_provenance
            or any(
                re.fullmatch(r"[0-9a-f]{64}", build_provenance.get(name, ""))
                is None
                for name in provenance_keys
            )
        ):
            raise ReleaseError("Runtime candidate build provenance is invalid")
        attestation_payloads: dict[str, dict[str, Any]] = {}
        for relative, key, label in (
            (
                "release/builder-toolchain.json",
                "builder_toolchain_sha256",
                "Builder toolchain attestation",
            ),
            (
                "release/wheel-install-attestation.json",
                "wheel_install_attestation_sha256",
                "wheel install attestation",
            ),
        ):
            payload, content = _read_candidate_control(root / relative, label)
            attestation_payloads[key] = payload
            row = actual_by_path.get(relative)
            if (
                row is None
                or row["byte_length"] != len(content)
                or row["sha256"] != hashlib.sha256(content).hexdigest()
                or row["sha256"] != build_provenance[key]
            ):
                raise ReleaseError("Runtime candidate build provenance is invalid")
        _validate_candidate_build_attestations(
            attestation_payloads["builder_toolchain_sha256"],
            attestation_payloads["wheel_install_attestation_sha256"],
            wheelhouse,
        )
    source_commit = runtime_inputs.get("runtime_source_commit")
    checks = acceptance.get("checks")
    wal_gate = acceptance.get("wal_gate")
    wheels = wheelhouse.get("wheels")
    if (
        type(runtime_inputs.get("schema_version")) is not int
        or runtime_inputs.get("schema_version") != 1
        or runtime_inputs.get("platform") != "windows-x86_64"
        or type(runtime_inputs.get("runtime_version")) is not str
        or not runtime_inputs["runtime_version"]
        or type(source_commit) is not str
        or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None
    ):
        raise ReleaseError("Runtime candidate Runtime inputs metadata is invalid")
    if (
        type(wheelhouse.get("schema_version")) is not int
        or wheelhouse.get("schema_version") != 1
        or wheelhouse.get("python_abi") != "cp314-win_amd64"
        or type(wheelhouse.get("wheel_count")) is not int
        or type(wheels) is not list
        or wheelhouse["wheel_count"] != len(wheels)
    ):
        raise ReleaseError("Runtime candidate wheelhouse metadata is invalid")
    if (
        type(acceptance.get("schema_version")) is not int
        or acceptance.get("schema_version") != 1
        or acceptance.get("status") != "candidate-pass"
        or acceptance.get("runtime_source_commit") != source_commit
        or type(checks) is not dict
        or any(checks.get(name) is not True for name in _REQUIRED_ACCEPTANCE_CHECKS)
        or (
            build_provenance is not None
            and (
                checks.get("builder_toolchain") is not True
                or checks.get("wheel_install_attestation") is not True
            )
        )
        or type(wal_gate) is not dict
        or wal_gate.get("scenarios_passed") is not True
        or wal_gate.get("parallel_job_execution_enabled") is not False
    ):
        raise ReleaseError("Runtime candidate acceptance metadata is invalid")
    if expected_source_commit is not None and source_commit != expected_source_commit:
        raise ReleaseError("Runtime expected source commit does not match the candidate")
    return {
        "manifest_sha256": manifest_sha256,
        "runtime_source_commit": source_commit,
        "runtime_version": runtime_inputs["runtime_version"],
        "platform": runtime_inputs["platform"],
        "status": acceptance["status"],
        "file_count": len(actual_rows),
        "total_bytes": sum(item["byte_length"] for item in actual_rows),
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
    environment = _clean_environment(os.environ)
    builder_attestation = _verify_builder_toolchain(
        lock,
        builder_python,
        environment,
    )
    python_archive, python_spdx, sqlite_archive, wheels = _verify_inputs(
        lock, inputs, wheelhouse
    )
    stage = parent / f".{output.name}.staging-{uuid.uuid4().hex}"
    stage.mkdir(mode=0o700)
    published = False
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
            label="Runtime wheel installation",
        )
        _remove_direct_url_metadata(site_packages, len(wheels))
        _remove_pip_console_scripts(site_packages)
        wheel_install_attestation = _verify_wheel_install(
            site_packages,
            wheelhouse,
            wheels,
        )
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
        _write_json(release / "builder-toolchain.json", builder_attestation)
        _write_json(
            release / "wheel-install-attestation.json",
            wheel_install_attestation,
        )
        builder_attestation_sha256 = _hash(release / "builder-toolchain.json")
        wheel_install_attestation_sha256 = _hash(
            release / "wheel-install-attestation.json"
        )
        probe = _probe_runtime(stage, lock, environment)
        with tempfile.TemporaryDirectory(prefix="alltonote-runtime-smoke-", dir=gate_root) as temporary:
            smoke = Path(temporary)
            isolated_environment = dict(environment)
            isolated_environment["ALLTONOTE_MACHINE_STATE_ROOT"] = str(
                smoke / "machine-state"
            )
            _run_cli(stage, ("version",), isolated_environment)
            info = _run_cli(stage, ("runtime", "info"), isolated_environment)
            _run_cli(stage, ("runtime", "doctor"), isolated_environment)
            engine_lifecycle = _run_engine_lifecycle_gate(
                stage,
                isolated_environment,
            )
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
                "build_provenance": {
                    "builder_toolchain_sha256": builder_attestation_sha256,
                    "wheel_install_attestation_sha256": (
                        wheel_install_attestation_sha256
                    ),
                },
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
                    "engine_lifecycle": True,
                    "unicode_workspace_init": True,
                    "sqlite_wal_gate": True,
                    "legacy_jobstore_migration": True,
                    "builder_toolchain": True,
                    "wheel_install_attestation": True,
                },
                "build_provenance": {
                    "builder_toolchain_sha256": builder_attestation_sha256,
                    "wheel_install_attestation_sha256": (
                        wheel_install_attestation_sha256
                    ),
                },
                "wal_gate": {
                    "connection_counts": gate_data["connection_counts"],
                    "scenarios_passed": True,
                    "parallel_job_execution_enabled": False,
                    "integrity": gate_data["integrity"],
                },
                "legacy_jobstore": legacy_jobstore,
                "engine_lifecycle": engine_lifecycle,
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


def _verify_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify a published Windows Runtime candidate")
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--expected-source-commit")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    try:
        if arguments[:1] == ["verify"]:
            payload = verify_runtime_candidate(
                **vars(_verify_parser().parse_args(arguments[1:]))
            )
        else:
            payload = assemble_runtime(**vars(_parser().parse_args(arguments)))
    except ReleaseError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "data": payload}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

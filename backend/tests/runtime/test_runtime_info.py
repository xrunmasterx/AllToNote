from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

import app.runtime_info as runtime_info_module
import app.runtime_lock as runtime_lock_module
from app.adapters.documents.document_basic_pack import PACK_ID, PACK_VERSION
from app.adapters.video_packs.official_video_pack import (
    MEDIA_BASIC,
    TRANSCRIBE_CPU,
    OfficialVideoPackContract,
)
from app.cli.main import main
from app.core.errors import DomainError, ErrorCategory
from app.runtime_capabilities import CapabilityRegistry, CapabilitySpec
from app.runtime_info import (
    RuntimeCheck,
    _sqlite_parallel_jobs_supported,
    build_runtime_info,
    runtime_doctor,
)
from app.runtime_lock import load_runtime_lock
from app.runtime_paths import resolve_runtime_paths


HELPER = Path(__file__).parents[1] / "helpers" / "report_cli_imports.py"


def _static_video_pack(
    paths: object,
    contract: OfficialVideoPackContract,
    digest_character: str,
) -> Path:
    manifest_sha256 = "sha256:" + digest_character * 64
    generation = (
        paths.data_dir
        / "packs"
        / contract.pack_id
        / contract.pack_version
        / "installs"
        / manifest_sha256.removeprefix("sha256:")
    )
    generation.mkdir(parents=True)
    (generation / "manifest.json").write_bytes(b"{}")
    for relative_path in {
        *contract.required_payload_files,
        *contract.entrypoints(
            "windows-x86_64" if os.name == "nt" else "posix-x86_64"
        ).values(),
    }:
        target = generation.joinpath(*relative_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fixture")
    receipt = generation / "receipt.json"
    receipt.write_bytes(
        json.dumps(
            {
                "schema_version": 1,
                "pack_id": contract.pack_id,
                "pack_version": contract.pack_version,
                "manifest_sha256": manifest_sha256,
                "verified": True,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    active = generation.parents[1] / "active.json"
    active.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pack_id": contract.pack_id,
                "pack_version": contract.pack_version,
                "manifest_sha256": manifest_sha256,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return receipt


def test_runtime_info_reports_pinned_versions_without_private_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = resolve_runtime_paths(local_data_parent=tmp_path / "local")
    monkeypatch.setattr(runtime_info_module, "resolve_runtime_paths", lambda: paths)

    assert main(["runtime", "info", "--json"]) == 0
    output = capsys.readouterr()
    envelope = json.loads(output.out)
    data = envelope["data"]

    assert output.err == ""
    assert output.out.count("\n") == 1
    assert data["runtime_version"] == "0.1.0"
    assert data["core_api_version"] == 1
    assert data["cli_api_version"] == 1
    assert data["desktop_api_versions"] == []
    assert data["portable_api_version"] == 1
    assert data["iwiki_contract"] == {
        "package": "llm-iwiki",
        "package_version": "0.1.3",
        "contract_version": 1,
        "contract_id": "iwiki-portable-contract-v1",
        "schema_id": "2026-07-portable-v1",
        "schema_hash": "sha256:f8ded2d23197685dc0046e3949e573097fa4ae13e12cfbba240ff0544ca2c9d9",
    }
    assert data["engine"] == {
        "supported": True,
        "running": False,
        "state": "stopped",
    }
    with closing(runtime_info_module.sqlite3.connect(":memory:")) as connection:
        sqlite_source_id = connection.execute(
            "SELECT sqlite_source_id()"
        ).fetchone()[0]
        sqlite_compile_options = sorted(
            row[0] for row in connection.execute("PRAGMA compile_options")
        )
    assert data["storage"] == {
        "sqlite_version": runtime_info_module.sqlite3.sqlite_version,
        "sqlite_source_id": sqlite_source_id,
        "sqlite_compile_options": sqlite_compile_options,
        "sqlite_threadsafety": runtime_info_module.sqlite3.threadsafety,
        "parallel_job_execution_supported": _sqlite_parallel_jobs_supported(
            runtime_info_module.sqlite3.sqlite_version,
            threadsafety=runtime_info_module.sqlite3.threadsafety,
            compile_options=sqlite_compile_options,
        ),
        "parallel_job_execution_enabled": False,
    }
    assert data["packs"] == [
        {
            "pack_id": PACK_ID,
            "version": PACK_VERSION,
            "installed": False,
            "probe": "static",
        },
        {
            "pack_id": MEDIA_BASIC.pack_id,
            "version": MEDIA_BASIC.pack_version,
            "installed": False,
            "probe": "static",
        },
        {
            "pack_id": TRANSCRIBE_CPU.pack_id,
            "version": TRANSCRIBE_CPU.pack_version,
            "installed": False,
            "probe": "static",
        },
    ]
    assert data["platform"]["os"] in {"windows", "macos", "linux"}
    assert data["platform"]["arch"] in {"x86_64", "arm64"}
    assert [item["key"] for item in envelope["capabilities"]] == sorted(
        item["key"] for item in envelope["capabilities"]
    )
    lowered = output.out.casefold()
    assert "users\\" not in lowered
    assert "/users/" not in lowered
    assert "api_key" not in lowered
    assert "cookie" not in lowered
    assert "prompt" not in lowered


def test_runtime_info_reports_engine_unavailable_without_claiming_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.engine.client as engine_client_module

    class UnavailableClient:
        def __init__(self, _paths) -> None:
            raise DomainError(
                "engine_state_root_unsafe",
                ErrorCategory.POLICY_DENIED,
                "Engine state is unsafe",
            )

    monkeypatch.setattr(engine_client_module, "LocalEngineClient", UnavailableClient)

    info = build_runtime_info(
        paths=resolve_runtime_paths(local_data_parent=tmp_path / "local")
    )

    assert info.engine_supported is True
    assert info.engine_running is False
    assert info.engine_state == "unavailable"


def test_runtime_info_preserves_unknown_engine_probe_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.engine.client as engine_client_module

    class BrokenClient:
        def __init__(self, _paths) -> None:
            raise RuntimeError("unexpected probe failure")

    monkeypatch.setattr(engine_client_module, "LocalEngineClient", BrokenClient)

    info = build_runtime_info(
        paths=resolve_runtime_paths(local_data_parent=tmp_path / "local")
    )

    assert info.engine_supported is True
    assert info.engine_running is False
    assert info.engine_state == "unknown"


def test_loaded_sqlite_identity_closes_its_memory_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_connect = runtime_info_module.sqlite3.connect
    arguments: list[str] = []
    connections: list[sqlite3.Connection] = []

    def tracked_connect(database: str) -> sqlite3.Connection:
        arguments.append(database)
        connection = real_connect(database)
        connections.append(connection)
        return connection

    monkeypatch.setattr(runtime_info_module.sqlite3, "connect", tracked_connect)

    source_id, compile_options = runtime_info_module._loaded_sqlite_identity()

    assert arguments == [":memory:"]
    assert source_id
    assert compile_options == tuple(sorted(compile_options))
    with pytest.raises(runtime_info_module.sqlite3.ProgrammingError):
        connections[0].execute("SELECT 1")


def test_runtime_info_does_not_call_network_or_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: pytest.fail("runtime info attempted network IO"),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("runtime info started a subprocess"),
    )

    info = build_runtime_info(
        paths=resolve_runtime_paths(local_data_parent=tmp_path / "local"),
        environ={},
    )

    assert info.runtime_version == "0.1.0"


def test_runtime_info_and_doctor_report_installed_document_pack(
    tmp_path: Path,
) -> None:
    paths = resolve_runtime_paths(local_data_parent=tmp_path / "local")
    pack_root = paths.data_dir / "packs" / PACK_ID / PACK_VERSION
    python = pack_root / (
        "venv/Scripts/python.exe" if os.name == "nt" else "venv/bin/python"
    )
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    (pack_root / "artifacts").mkdir()

    info = build_runtime_info(paths=paths, environ={})
    checks = runtime_doctor(dynamic=False, paths=paths, environ={})

    assert info.packs[0].installed is True
    assert next(check for check in checks if check.code == "pack.document-basic") == (
        RuntimeCheck("pack.document-basic", "pass", None, False)
    )


def test_runtime_info_and_doctor_report_installed_video_packs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = resolve_runtime_paths(local_data_parent=tmp_path / "local")
    _static_video_pack(paths, MEDIA_BASIC, "a")
    transcribe_receipt = _static_video_pack(paths, TRANSCRIBE_CPU, "b")
    registry = CapabilityRegistry(
        (
            CapabilitySpec(
                "recipe.video.acquire.bilibili", ("missing_bilibili_module",)
            ),
            CapabilitySpec(
                "recipe.video.acquire.youtube", ("missing_youtube_module",)
            ),
            CapabilitySpec(
                "recipe.video.acquire.local", ("missing_local_module",)
            ),
            CapabilitySpec(
                "recipe.video.transcribe.local.cpu", ("missing_whisper_module",)
            ),
        )
    )
    monkeypatch.setattr(runtime_info_module, "CapabilityRegistry", lambda: registry)

    info = build_runtime_info(paths=paths, environ={}, registry=registry)
    checks = runtime_doctor(dynamic=False, paths=paths, environ={})

    assert {pack.pack_id: pack.installed for pack in info.packs} == {
        PACK_ID: False,
        MEDIA_BASIC.pack_id: True,
        TRANSCRIBE_CPU.pack_id: True,
    }
    capabilities = {item.key: item.installed for item in info.capabilities}
    assert capabilities["recipe.video.acquire.bilibili"] is True
    assert capabilities["recipe.video.acquire.local"] is True
    assert capabilities["recipe.video.transcribe.local.cpu"] is True
    assert capabilities["recipe.video.acquire.youtube"] is False
    assert next(check for check in checks if check.code == "pack.media-basic") == (
        RuntimeCheck("pack.media-basic", "pass", None, False)
    )
    assert next(check for check in checks if check.code == "pack.transcribe-cpu") == (
        RuntimeCheck("pack.transcribe-cpu", "pass", None, False)
    )
    for key in (
        "recipe.video.acquire.bilibili",
        "recipe.video.acquire.local",
        "recipe.video.transcribe.local.cpu",
    ):
        assert next(
            check for check in checks if check.code == f"capability.{key}"
        ) == RuntimeCheck(f"capability.{key}", "pass", None, False)
    assert next(
        check
        for check in checks
        if check.code == "capability.recipe.video.acquire.youtube"
    ).status == "warn"

    transcribe_receipt.write_bytes(b"{}")
    corrupted = build_runtime_info(paths=paths, environ={}, registry=registry)
    assert next(
        pack for pack in corrupted.packs if pack.pack_id == TRANSCRIBE_CPU.pack_id
    ).installed is False


def test_runtime_doctor_explains_missing_document_pack(
    tmp_path: Path,
) -> None:
    paths = resolve_runtime_paths(local_data_parent=tmp_path / "local")

    checks = runtime_doctor(dynamic=False, paths=paths, environ={})

    pack = next(check for check in checks if check.code == "pack.document-basic")
    assert pack.status == "warn"
    assert pack.action == "Install or repair the compatible document-basic Pack"


@pytest.mark.parametrize(
    ("version", "expected"),
    (
        ("3.44.5", False),
        ("3.44.6", True),
        ("3.49.9", False),
        ("3.50.4", False),
        ("3.50.7", True),
        ("3.51.2", False),
        ("3.51.3", True),
        ("3.51.4", True),
        ("3.52.0", False),
        ("3.53.3", False),
        ("3.53.4", True),
        ("3.53.5", True),
        ("4.0.0", False),
        ("invalid", False),
    ),
)
def test_sqlite_parallel_job_gate_uses_only_project_validated_release_lines(
    version: str,
    expected: bool,
) -> None:
    assert _sqlite_parallel_jobs_supported(version) is expected


def test_sqlite_parallel_job_support_requires_serialized_wal_build() -> None:
    assert not _sqlite_parallel_jobs_supported(
        "3.53.4",
        threadsafety=0,
        compile_options=("THREADSAFE=0",),
    )
    assert not _sqlite_parallel_jobs_supported(
        "3.53.4",
        threadsafety=3,
        compile_options=("OMIT_WAL", "THREADSAFE=1"),
    )
    assert _sqlite_parallel_jobs_supported(
        "3.53.4",
        threadsafety=3,
        compile_options=("THREADSAFE=1",),
    )


def test_runtime_doctor_warns_when_sqlite_is_unsafe_for_parallel_wal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_info_module.sqlite3, "sqlite_version", "3.50.4")

    check = next(
        item
        for item in runtime_doctor(dynamic=False)
        if item.code == "storage.sqlite.parallel-jobs"
    )

    assert check.status == "warn"
    assert check.action == (
        "Use an AllToNote Runtime validated with SQLite 3.44.6+, 3.50.7+, "
        "3.51.3+, or 3.53.4+ on the same release line before enabling parallel "
        "Job execution"
    )


def test_runtime_doctor_passes_for_patched_sqlite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_info_module.sqlite3, "sqlite_version", "3.51.3")

    check = next(
        item
        for item in runtime_doctor(dynamic=False)
        if item.code == "storage.sqlite.parallel-jobs"
    )

    assert check == RuntimeCheck(
        "storage.sqlite.parallel-jobs",
        "pass",
        None,
        False,
    )


def test_runtime_info_cold_path_does_not_import_heavy_recipe_modules() -> None:
    result = subprocess.run(
        [sys.executable, str(HELPER), "runtime", "info", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(result.stdout)
    envelope = json.loads(report["stdout"])
    imported = set(report["imported_modules"])

    assert report["exit_code"] == 0
    assert envelope["command"] == "runtime info"
    assert not {
        "cryptography",
        "fastapi",
        "torch",
        "faster_whisper",
        "app.runtime",
        "app.adapters.video_packs.official_video_pack_verifier",
        "app.services.note",
        "app.transcriber.whisper",
        "app.downloaders.youtube_downloader",
    } & imported


def test_capability_registry_separates_installed_from_missing_modules() -> None:
    registry = CapabilityRegistry(
        (
            CapabilitySpec("capability.available", ("json",)),
            CapabilitySpec("capability.missing", ("missing_alltonote_module",)),
        )
    )

    snapshot = registry.snapshot()

    assert [item.to_mapping() for item in snapshot] == [
        {
            "key": "capability.available",
            "installed": True,
            "version": "1",
            "probe": "static",
        },
        {
            "key": "capability.missing",
            "installed": False,
            "version": "1",
            "probe": "static",
        },
    ]


def test_runtime_lock_version_mismatch_has_stable_safe_cli_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package_root = tmp_path / "private-user-path" / "app"
    package_root.mkdir(parents=True)
    (package_root / "runtime-lock.json").write_text(
        json.dumps(
            {
                "iwiki_package": "llm-iwiki==999.0.0",
                "portable_api_version": 1,
                "portable_contract_id": "iwiki-portable-contract-v1",
                "schema_set_id": "2026-07-portable-v1",
                "schema_sha256": "sha256:" + "0" * 64,
                "source_commit": "0" * 40,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        runtime_lock_module.resources,
        "files",
        lambda _name: package_root,
    )

    exit_code = main(["runtime", "info", "--json"])
    output = capsys.readouterr()
    envelope = json.loads(output.out)

    assert exit_code == 10
    assert envelope["error"]["code"] == "portable_contract_incompatible"
    assert "private-user-path" not in output.out


def test_runtime_lock_loader_rejects_distribution_version_drift() -> None:
    with pytest.raises(DomainError, match="portable_contract_incompatible"):
        load_runtime_lock(distribution_version=lambda _name: "999.0.0")


def test_runtime_doctor_skips_dynamic_probes_unless_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_info_module,
        "_dynamic_checks",
        lambda: pytest.fail("dynamic checks ran without --dynamic"),
    )

    checks = runtime_doctor(dynamic=False)

    assert checks
    assert all(check.dynamic is False for check in checks)


def test_runtime_doctor_dynamic_results_are_structured(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        runtime_info_module,
        "_dynamic_checks",
        lambda: (
            RuntimeCheck(
                "dynamic.fixture",
                "warn",
                "Configure the fixture",
                True,
            ),
        ),
    )

    assert main(["runtime", "doctor", "--dynamic", "--json"]) == 0
    envelope = json.loads(capsys.readouterr().out)

    assert envelope["data"]["dynamic"] is True
    assert envelope["data"]["healthy"] is True
    assert envelope["data"]["checks"][-1] == {
        "code": "dynamic.fixture",
        "status": "warn",
        "action": "Configure the fixture",
        "dynamic": True,
    }

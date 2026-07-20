from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

import app.runtime_info as runtime_info_module
import app.runtime_lock as runtime_lock_module
from app.cli.main import main
from app.core.errors import DomainError
from app.runtime_capabilities import CapabilityRegistry, CapabilitySpec
from app.runtime_info import RuntimeCheck, build_runtime_info, runtime_doctor
from app.runtime_lock import load_runtime_lock


HELPER = Path(__file__).parents[1] / "helpers" / "report_cli_imports.py"


def test_runtime_info_reports_pinned_versions_without_private_paths(
    capsys: pytest.CaptureFixture[str],
) -> None:
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
        "package_version": "0.1.2",
        "contract_version": 1,
        "contract_id": "iwiki-portable-contract-v1",
        "schema_id": "2026-07-portable-v1",
        "schema_hash": "sha256:f8ded2d23197685dc0046e3949e573097fa4ae13e12cfbba240ff0544ca2c9d9",
    }
    assert data["engine"] == {"supported": False, "running": False}
    assert data["packs"] == []
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


def test_runtime_info_does_not_call_network_or_subprocess(
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

    info = build_runtime_info()

    assert info.runtime_version == "0.1.0"


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
        "fastapi",
        "torch",
        "faster_whisper",
        "app.runtime",
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

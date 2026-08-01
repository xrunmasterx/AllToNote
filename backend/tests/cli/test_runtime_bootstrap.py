from __future__ import annotations

from importlib import resources
import json
from pathlib import Path
import subprocess
import sys


import pytest


@pytest.mark.parametrize(
    "arguments",
    (
        ("recipe", "list", "--json"),
        ("recipe", "describe", "alltonote.video-producer@2", "--json"),
        ("recipe", "describe", "alltonote.document-note@1", "--json"),
    ),
)
def test_recipe_discovery_does_not_import_runtime_or_heavy_modules(
    arguments: tuple[str, ...],
) -> None:
    result = subprocess.run(
        [sys.executable, str(HELPER), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(result.stdout)

    assert report["exit_code"] == 0
    assert not {
        "app.runtime",
        "app.core.application.video_service",
        "app.core.application.document_service",
        "app.core.recipes.video.adapter",
        "app.core.recipes.document.adapter",
        "app.adapters.documents.docling_worker_parser",
        "app.workspace_initializer",
        "app.adapters.sources.legacy_video",
        "app.adapters.transcription.legacy_transcriber",
        "fastapi",
        "torch",
        "faster_whisper",
        "yt_dlp",
        "openai",
    } & set(report["imported_modules"])


HELPER = Path(__file__).resolve().parents[1] / "helpers" / "report_cli_imports.py"

EXPECTED_LOCK = {
    "iwiki_package": "llm-iwiki==0.1.3",
    "portable_api_version": 1,
    "portable_contract_id": "iwiki-portable-contract-v1",
    "schema_set_id": "2026-07-portable-v1",
    "schema_sha256": "sha256:f8ded2d23197685dc0046e3949e573097fa4ae13e12cfbba240ff0544ca2c9d9",
    "source_commit": "1fff39fe54ba0cff16df0a4d31111dbc966dd88b",
}

def test_cli_version_does_not_import_web_or_video_modules():
    result = subprocess.run(
        [sys.executable, str(HELPER), "version", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )

    report = json.loads(result.stdout)
    envelope = json.loads(report["stdout"])

    assert report["exit_code"] == 0
    assert envelope == {
        "alltonote_cli_protocol_version": 1,
        "ok": True,
        "command": "version",
        "correlation_id": envelope["correlation_id"],
        "data": {"runtime_version": "0.1.0"},
        "error": None,
        "warnings": [],
        "job": None,
        "artifacts": [],
        "capabilities": [],
        "versions": {
            "runtime_version": "0.1.0",
            "cli_protocol_version": 1,
        },
    }
    assert envelope["correlation_id"].startswith("corr_")
    assert report["stdout"].count("\n") == 1
    assert not {
        "fastapi",
        "torch",
        "faster_whisper",
        "app.workspace_initializer",
        "app.core.application.review_candidate_service",
        "app.adapters.iwiki.portable_gateway",
        "app.services.note",
    } & set(report["imported_modules"])


def test_static_pack_doctor_does_not_import_verifier_or_heavy_modules() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "pack",
            "doctor",
            "document-basic",
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(result.stdout)
    envelope = json.loads(report["stdout"])

    assert report["exit_code"] == 0
    assert envelope["command"] == "pack doctor"
    assert not {
        "app.runtime",
        "app.adapters.documents.document_basic_pack_installer",
        "app.adapters.documents.document_basic_pack_verifier",
        "app.adapters.documents.docling_worker_parser",
        "cryptography",
        "docling",
        "torch",
    } & set(report["imported_modules"])


def test_engine_status_is_cold_and_does_not_import_execution_modules() -> None:
    result = subprocess.run(
        [sys.executable, str(HELPER), "engine", "status", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(result.stdout)
    envelope = json.loads(report["stdout"])

    assert report["exit_code"] == 0
    assert envelope["command"] == "engine status"
    assert envelope["data"]["running"] is False
    assert not {
        "app.runtime",
        "app.job_runtime",
        "app.engine.host",
        "app.core.application.video_service",
        "app.core.application.document_service",
        "app.adapters.documents.docling_worker_parser",
        "torch",
        "faster_whisper",
        "yt_dlp",
        "openai",
    } & set(report["imported_modules"])


def test_engine_host_import_does_not_load_worker_recipe_or_pack_modules() -> None:
    backend_root = Path(__file__).resolve().parents[2]
    script = (
        "import json,sys;"
        f"sys.path.insert(0,{str(backend_root)!r});"
        "import app.engine.host;"
        "print(json.dumps(sorted(sys.modules)))"
    )

    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    imported = set(json.loads(result.stdout))

    assert not {
        "app.runtime",
        "app.job_runtime",
        "app.engine.job_worker",
        "app.core.application.video_service",
        "app.core.application.document_service",
        "app.core.recipes.video.adapter",
        "app.core.recipes.document.adapter",
        "app.adapters.video_packs.official_pack_process",
        "app.adapters.documents.docling_worker_parser",
        "torch",
        "faster_whisper",
        "yt_dlp",
        "openai",
    } & imported


def test_runtime_lock_is_available_as_a_package_resource():
    lock_resource = resources.files("app").joinpath("runtime-lock.json")

    assert json.loads(lock_resource.read_text(encoding="utf-8")) == EXPECTED_LOCK

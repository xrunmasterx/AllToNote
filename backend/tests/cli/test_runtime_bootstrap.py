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
        "app.core.recipes.video.adapter",
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
    "iwiki_package": "llm-iwiki==0.1.2",
    "portable_api_version": 1,
    "portable_contract_id": "iwiki-portable-contract-v1",
    "schema_set_id": "2026-07-portable-v1",
    "schema_sha256": "sha256:f8ded2d23197685dc0046e3949e573097fa4ae13e12cfbba240ff0544ca2c9d9",
    "source_commit": "8701ace4f65ffd7ee46fbcf3edcc2ce2bcfc47e1",
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
        "app.services.note",
    } & set(report["imported_modules"])


def test_runtime_lock_is_available_as_a_package_resource():
    lock_resource = resources.files("app").joinpath("runtime-lock.json")

    assert json.loads(lock_resource.read_text(encoding="utf-8")) == EXPECTED_LOCK

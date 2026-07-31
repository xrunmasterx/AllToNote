from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from app.adapters.video_packs.official_pack_process import (
    minimal_worker_environment,
    run_json_worker,
)
from app.core.errors import DomainError


def test_process_round_trips_bounded_json(tmp_path: Path) -> None:
    result = run_json_worker(
        (
            sys.executable,
            "-c",
            "import json,sys; value=json.load(sys.stdin); json.dump({'seen':value},sys.stdout)",
        ),
        {"marker": "one"},
        cwd=tmp_path,
        environment=minimal_worker_environment({}),
        timeout_seconds=5,
        maximum_output_bytes=1024,
    )
    assert result == {"seen": {"marker": "one"}}


def test_process_rejects_oversized_or_malformed_output(tmp_path: Path) -> None:
    with pytest.raises(DomainError) as oversized:
        run_json_worker(
            (sys.executable, "-c", "print('x' * 2048)"),
            {},
            cwd=tmp_path,
            environment=minimal_worker_environment({}),
            timeout_seconds=5,
            maximum_output_bytes=128,
        )
    assert oversized.value.code == "pack_worker_result_invalid"

    with pytest.raises(DomainError) as malformed:
        run_json_worker(
            (sys.executable, "-c", "print('not-json')"),
            {},
            cwd=tmp_path,
            environment=minimal_worker_environment({}),
            timeout_seconds=5,
            maximum_output_bytes=128,
        )
    assert malformed.value.code == "pack_worker_result_invalid"


def test_process_terminates_worker_as_soon_as_output_exceeds_limit(
    tmp_path: Path,
) -> None:
    started = time.monotonic()

    with pytest.raises(DomainError) as caught:
        run_json_worker(
            (
                sys.executable,
                "-c",
                (
                    "import os,time;"
                    "os.write(1,b'x'*(2*1024*1024));"
                    "time.sleep(60)"
                ),
            ),
            {},
            cwd=tmp_path,
            environment=minimal_worker_environment({}),
            timeout_seconds=5,
            maximum_output_bytes=128,
        )

    assert caught.value.code == "pack_worker_result_invalid"
    assert time.monotonic() - started < 3


def test_process_timeout_terminates_only_spawned_worker(tmp_path: Path) -> None:
    with pytest.raises(DomainError) as caught:
        run_json_worker(
            (sys.executable, "-c", "import time; time.sleep(60)"),
            {},
            cwd=tmp_path,
            environment=minimal_worker_environment({}),
            timeout_seconds=0.1,
            maximum_output_bytes=128,
        )
    assert caught.value.code == "pack_worker_timeout"


def test_minimal_environment_has_no_unapproved_secret(tmp_path: Path) -> None:
    source = {
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", "C:\\Windows"),
        "PATH": str(tmp_path),
        "ALLTONOTE_API_KEY": "secret",
        "OPENAI_API_KEY": "secret",
        "COOKIE": "secret",
    }
    environment = minimal_worker_environment(
        source,
        overrides={"PYTHONPATH": str(tmp_path / "runtime")},
    )

    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert environment["PYTHONPATH"] == str(tmp_path / "runtime")
    assert not {"ALLTONOTE_API_KEY", "OPENAI_API_KEY", "COOKIE"} & environment.keys()

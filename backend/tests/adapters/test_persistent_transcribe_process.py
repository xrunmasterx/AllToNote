from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import threading
import time

import pytest

from app.adapters.video_packs.persistent_transcribe_process import PersistentTranscribeProcess
from app.adapters.worker_process import minimal_worker_environment
from app.core.errors import DomainError


SCRIPT = """
import json, os, sys, time
for count, line in enumerate(sys.stdin, 1):
    request = json.loads(line)
    time.sleep(request.get('delay', 0))
    print(json.dumps({'result': {'pid': os.getpid(), 'count': count}, 'timings': {}}), flush=True)
"""


def call(worker, tmp_path, **changes):
    options = dict(cwd=tmp_path,
        environment=minimal_worker_environment(overrides={"PYTHONPATH": str(tmp_path)}),
        timeout_seconds=5, maximum_output_bytes=65536)
    delay = changes.pop("delay", 0)
    options.update(changes)
    return worker((sys.executable, "-u", "-c", SCRIPT),
                  {"model_path": "fixed-model", "cpu_threads": 8, "delay": delay}, **options)


def test_parallel_calls_serialize_and_reuse_owned_worker(tmp_path):
    worker = PersistentTranscribeProcess()
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(lambda _: call(worker, tmp_path), range(8)))
        assert len({value["pid"] for value in values}) == 1
        assert sorted(value["count"] for value in values) == list(range(1, 9))
        process = worker._process
    finally:
        worker.close()
    assert process.poll() is not None


def test_timeout_kills_worker_and_next_call_restarts(tmp_path):
    worker = PersistentTranscribeProcess()
    try:
        first = call(worker, tmp_path)
        with pytest.raises(DomainError, match="pack_worker_timeout"):
            call(worker, tmp_path, delay=2, timeout_seconds=0.1)
        value = call(worker, tmp_path)
        assert value["count"] == 1
        assert value["pid"] != first["pid"]
    finally:
        worker.close()


def test_cancelled_waiter_does_not_kill_active_transcription(tmp_path):
    worker = PersistentTranscribeProcess()
    ready = threading.Event()
    def cancel():
        raise RuntimeError("cancelled")
    try:
        call(worker, tmp_path)
        with ThreadPoolExecutor(max_workers=1) as pool:
            def active():
                return call(worker, tmp_path, delay=0.3, check_cancelled=ready.set)
            pending = pool.submit(active)
            assert ready.wait(2)
            with pytest.raises(RuntimeError, match="cancelled"):
                call(worker, tmp_path, check_cancelled=cancel)
            assert pending.result(timeout=3)["count"] == 2
        assert call(worker, tmp_path)["count"] == 3
    finally:
        worker.close()


def test_idle_worker_is_reaped(tmp_path):
    worker = PersistentTranscribeProcess(idle_seconds=0.05)
    try:
        call(worker, tmp_path)
        process = worker._process
        deadline = time.monotonic() + 3
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert process.poll() is not None
    finally:
        worker.close()

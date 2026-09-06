from contextlib import ExitStack
import multiprocessing
from pathlib import Path
import time

import pytest

from app.gpt.model_slots import MODEL_CALL_CAPACITY, model_call_slot


def _try_slot(root, output):
    try:
        with model_call_slot(Path(root), deadline=time.monotonic() + 0.25):
            output.put("acquired")
    except TimeoutError:
        output.put("full")


def _hold_slot_until_killed(root, ready):
    with model_call_slot(Path(root), deadline=time.monotonic() + 5):
        ready.set()
        # Deliberately killed by the parent to exercise OS-owned lock cleanup.
        time.sleep(30)


def test_capacity_is_shared_across_processes_and_reusable(tmp_path):
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    with ExitStack() as stack:
        for _ in range(MODEL_CALL_CAPACITY):
            stack.enter_context(model_call_slot(tmp_path, deadline=time.monotonic() + 1))
        process = context.Process(target=_try_slot, args=(str(tmp_path), output))
        process.start()
        try:
            assert output.get(timeout=10) == "full"
            process.join(timeout=5)
            assert process.exitcode == 0
        finally:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    process = context.Process(target=_try_slot, args=(str(tmp_path), output))
    process.start()
    try:
        assert output.get(timeout=10) == "acquired"
        process.join(timeout=5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        output.close()


def test_process_death_releases_slot_without_ttl_wait(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    with ExitStack() as stack:
        for _ in range(MODEL_CALL_CAPACITY - 1):
            stack.enter_context(model_call_slot(tmp_path, deadline=time.monotonic() + 1))
        process = context.Process(target=_hold_slot_until_killed, args=(str(tmp_path), ready))
        process.start()
        try:
            assert ready.wait(10)
            with pytest.raises(TimeoutError):
                with model_call_slot(tmp_path, deadline=time.monotonic() + 0.1):
                    pytest.fail("cross-process limit bypassed")
        finally:
            process.terminate()
            process.join(timeout=5)
        with model_call_slot(tmp_path, deadline=time.monotonic() + 0.5):
            pass


def test_cancelled_wait_does_not_take_capacity(tmp_path):
    def cancel():
        raise RuntimeError("cancel")
    with pytest.raises(RuntimeError, match="cancel"):
        with model_call_slot(tmp_path, deadline=time.monotonic() + 1, check_cancelled=cancel):
            pytest.fail("cancelled call acquired slot")
    with ExitStack() as stack:
        for _ in range(MODEL_CALL_CAPACITY):
            stack.enter_context(model_call_slot(tmp_path, deadline=time.monotonic() + 1))

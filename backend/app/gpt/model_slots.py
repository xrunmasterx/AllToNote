"""Machine-wide admission for model calls, shared by CLI and Engine workers."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import time

if os.name == "nt":
    import msvcrt
else:
    import fcntl


MODEL_CALL_CAPACITY = 8
VIDEO_MODEL_CONCURRENCY = 4


@contextmanager
def model_call_slot(
    root: Path,
    *,
    deadline: float,
    check_cancelled: Callable[[], None] | None = None,
) -> Iterator[None]:
    with resource_slot(root, resource="model-call", capacity=MODEL_CALL_CAPACITY,
                       deadline=deadline, check_cancelled=check_cancelled):
        yield


@contextmanager
def resource_slot(
    root: Path, *, resource: str, capacity: int, deadline: float,
    check_cancelled: Callable[[], None] | None = None,
) -> Iterator[None]:
    """Hold an OS lock only during a call; process death releases admission.

    Files are stable lock identities, not lease records: never unlink them.
    The fixed slot namespace prevents workers from independently multiplying
    provider concurrency. Waiting does not start a model request.
    """
    if (resource not in {"model-call", "transcribe", "download", "ffmpeg"}
            or type(capacity) is not int or capacity < 1):
        raise ValueError("Invalid resource admission")
    root.mkdir(parents=True, exist_ok=True)
    held: int | None = None
    try:
        while held is None:
            if check_cancelled is not None:
                check_cancelled()
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for machine {resource} capacity")
            for slot in range(capacity):
                descriptor = os.open(root / f"{resource}-{slot}.lock", os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    if os.name == "nt":
                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    os.close(descriptor)
                    if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                else:
                    held = descriptor
                    break
            if held is None:
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        if check_cancelled is not None:
            check_cancelled()
        yield
    finally:
        if held is not None:
            # Closing releases the OS lock, including on exceptions/cancellation.
            os.close(held)

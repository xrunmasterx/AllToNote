from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import time

from app.core.errors import DomainError, ErrorCategory
from app.gpt.model_slots import resource_slot


logger = logging.getLogger(__name__)
_CAPACITY = {"transcribe": 1, "download": 2, "ffmpeg": 2}


@contextmanager
def video_resource_slot(root, resource, *, timeout_seconds, check_cancelled):
    started = time.monotonic()
    admitted = None
    succeeded = False
    try:
        try:
            gate = resource_slot(root, resource=resource, capacity=_CAPACITY[resource],
                                 deadline=started + timeout_seconds, check_cancelled=check_cancelled)
            gate.__enter__()
        except TimeoutError as error:
            raise DomainError("video_resource_wait_timeout", ErrorCategory.RETRYABLE_RUNTIME,
                              "Video resource admission exceeded its time budget") from error
        try:
            admitted = time.monotonic()
            yield
            succeeded = True
        finally:
            gate.__exit__(None, None, None)
    finally:
        finished = time.monotonic()
        logger.info("video_performance %s", json.dumps({
            "stage": resource, "success": succeeded,
            "resource_wait_seconds": (admitted or finished) - started,
            "execution_seconds": finished - admitted if admitted is not None else 0,
        }))


def admitted_pack_worker(command, request, *, resource_root, resource, runner, **kwargs):
    with video_resource_slot(resource_root, resource, timeout_seconds=kwargs["timeout_seconds"],
                             check_cancelled=kwargs.get("check_cancelled")):
        return runner(command, request, **kwargs)

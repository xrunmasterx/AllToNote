from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.application.produce_service import ProduceService
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.model import JobExecutionOwner, JobState
from app.core.recipes.contracts import (
    InputDescriptor, ProduceRequest, ProduceSubmission, RecipeDescriptor, RecipeKey,
)
from app.core.recipes.registry import RecipeRegistry


class Endpoint:
    def __init__(self, submission: object) -> None:
        self.submission = submission
        self.requests: list[ProduceRequest] = []
        self.execution_owners: list[JobExecutionOwner] = []

    def submit(
        self,
        request: ProduceRequest,
        *,
        execution_owner: JobExecutionOwner,
    ) -> object:
        self.requests.append(request)
        self.execution_owners.append(execution_owner)
        return self.submission


def make_request(key: RecipeKey, request_id: str) -> ProduceRequest:
    return ProduceRequest(
        1, key, InputDescriptor("source", request_id), "workspace", ("markdown",),
        client_request_id=request_id,
    )


def make_service(key: RecipeKey, endpoint: object) -> ProduceService:
    descriptor = RecipeDescriptor(key, "Recipe", ("source",), ("markdown",))
    return ProduceService(RecipeRegistry(((descriptor, endpoint),)))  # type: ignore[arg-type]


def assert_error(error: DomainError, expected: tuple[str, ErrorCategory, str]) -> None:
    assert (error.code, error.category, error.message) == expected


def test_submit_routes_exact_requests_without_cross_request_state() -> None:
    key_a, key_b = RecipeKey("recipe.a", 1), RecipeKey("recipe.b", 1)
    submission_a = ProduceSubmission("job-a", key_a, JobState.QUEUED)
    submission_b = ProduceSubmission("job-b", key_b, JobState.QUEUED)
    endpoint_a, endpoint_b = Endpoint(submission_a), Endpoint(submission_b)
    registry = RecipeRegistry((
        (RecipeDescriptor(key_a, "A", ("source",), ("markdown",)), endpoint_a),
        (RecipeDescriptor(key_b, "B", ("source",), ("markdown",)), endpoint_b),
    ))
    service = ProduceService(registry)
    request_a1 = make_request(key_a, "a-1")
    request_b = make_request(key_b, "b-1")
    request_a2 = make_request(key_a, "a-2")

    assert service.submit(request_a1) is submission_a
    assert service.submit(request_b) is submission_b
    assert service.submit(request_a2) is submission_a
    assert endpoint_a.requests == [request_a1, request_a2]
    assert endpoint_a.requests[0] is request_a1
    assert endpoint_a.requests[1] is request_a2
    assert endpoint_b.requests == [request_b]
    assert endpoint_b.requests[0] is request_b


@pytest.mark.parametrize("invalid", [None, {"recipe": "recipe.a@1"}, object()])
def test_submit_rejects_non_request_envelopes(invalid: object) -> None:
    service = make_service(RecipeKey("recipe.a", 1), Endpoint(None))
    with pytest.raises(DomainError) as raised:
        service.submit(invalid)  # type: ignore[arg-type]
    assert_error(raised.value, (
        "produce_request_invalid", ErrorCategory.INVALID_REQUEST,
        "request must be a ProduceRequest",
    ))


def test_submit_preserves_registry_resolution_errors() -> None:
    service = make_service(RecipeKey("known.recipe", 1), Endpoint(None))
    with pytest.raises(DomainError) as raised:
        service.submit(make_request(RecipeKey("missing.recipe", 1), "missing"))
    assert_error(raised.value, (
        "recipe_not_found", ErrorCategory.INVALID_REQUEST, "Recipe is not registered",
    ))


def test_submit_propagates_the_exact_endpoint_error() -> None:
    sentinel = DomainError("endpoint_failed", ErrorCategory.RECIPE_FAILED, "Endpoint failed")

    class FailingEndpoint:
        def submit(
            self,
            request: ProduceRequest,
            *,
            execution_owner: JobExecutionOwner,
        ) -> ProduceSubmission:
            del request, execution_owner
            raise sentinel

    key = RecipeKey("recipe.a", 1)
    with pytest.raises(DomainError) as raised:
        make_service(key, FailingEndpoint()).submit(make_request(key, "request-a"))
    assert raised.value is sentinel


@pytest.mark.parametrize("invalid", [None, {"job_id": "job-a"}, object()])
def test_submit_rejects_invalid_submission_envelopes(invalid: object) -> None:
    key = RecipeKey("recipe.a", 1)
    with pytest.raises(DomainError) as raised:
        make_service(key, Endpoint(invalid)).submit(make_request(key, "request-a"))
    assert_error(raised.value, (
        "produce_submission_invalid", ErrorCategory.INTERNAL,
        "Recipe endpoint returned an invalid submission",
    ))


def test_submit_rejects_a_submission_for_another_recipe() -> None:
    requested_key, returned_key = RecipeKey("recipe.a", 1), RecipeKey("recipe.b", 1)
    endpoint = Endpoint(ProduceSubmission("job-b", returned_key, JobState.QUEUED))
    with pytest.raises(DomainError) as raised:
        make_service(requested_key, endpoint).submit(make_request(requested_key, "request-a"))
    assert_error(raised.value, (
        "produce_submission_recipe_mismatch", ErrorCategory.INTERNAL,
        "Recipe endpoint returned a mismatched Recipe key",
    ))


def test_produce_service_cold_import_avoids_runtime_workers_and_heavy_modules() -> None:
    code = """
import json, sys, threading
before = tuple(threading.enumerate())
import app.core.application.produce_service
exact = {'app.core.domain.video', 'app.runtime', 'app.services.note',
         'app.services.task_serial_executor', 'app.transcriber.whisper',
         'app.downloaders.youtube_downloader', 'app.gpt.gpt_factory'}
prefixes = ('fastapi', 'torch', 'faster_whisper', 'mlx_whisper', 'yt_dlp',
            'openai', 'anthropic', 'httpx', 'sqlalchemy')
blocked = sorted(name for name in sys.modules
                 if name in exact or name.startswith(prefixes))
print(json.dumps({'blocked': blocked, 'threads_unchanged': before == tuple(threading.enumerate())}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=Path(__file__).parents[2], check=True,
        capture_output=True, text=True,
    )
    assert json.loads(completed.stdout) == {"blocked": [], "threads_unchanged": True}

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config.model import RuntimeConfig
from app.runtime_config import effective_runtime_config
from app.core.domain.video import (
    FaithfulLanguagePolicy,
    JobState,
    ScreenshotPolicy,
    VideoDocumentKind,
    VideoProduceRequest,
)
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.model import JobExecutionOwner
from app.core.recipes.contracts import InputDescriptor, ProduceRequest, RecipeKey
from app.core.recipes.video.adapter import (
    VideoRecipeAdapter,
    adapt_video_produce_request,
)
from app.core.recipes.video.descriptor import (
    VIDEO_COURSE_NOTE_V1,
    VIDEO_DESCRIPTORS,
    VIDEO_PRODUCER_V2,
)


class _VideoServiceSpy:
    def __init__(self, state: JobState = JobState.QUEUED) -> None:
        self.requests: list[VideoProduceRequest] = []
        self.execution_owners: list[JobExecutionOwner] = []
        self.state = state

    def submit_video(
        self,
        request: VideoProduceRequest,
        *,
        execution_owner: JobExecutionOwner,
    ) -> object:
        self.requests.append(request)
        self.execution_owners.append(execution_owner)
        return SimpleNamespace(job_id="job_spy", state=self.state)


def _request(
    key: RecipeKey,
    *,
    outputs: tuple[str, ...] = ("knowledge-note",),
    parameters: dict[str, object] | None = None,
    attributes: dict[str, object] | None = None,
) -> ProduceRequest:
    return ProduceRequest(
        1,
        key,
        InputDescriptor("source", "fixture://course", attributes or {}),
        "C:/vault",
        outputs,
        parameters or {},
        "local-user",
        "adapter-request",
    )


def test_video_adapter_maps_v1_and_projects_actual_state() -> None:
    service = _VideoServiceSpy(JobState.RUNNING)
    request = _request(VIDEO_COURSE_NOTE_V1.key)

    submission = VideoRecipeAdapter(service).submit(request)  # type: ignore[arg-type]

    assert submission.job_id == "job_spy"
    assert submission.recipe_key is request.recipe_key
    assert submission.state is JobState.RUNNING
    assert service.requests == [
        VideoProduceRequest(
            request_schema_version=1,
            workspace_root=Path("C:/vault"),
            input_value="fixture://course",
            recipe_id="alltonote.video-course-note",
            recipe_version=1,
            client_request_id="adapter-request",
        )
    ]


def test_video_adapter_maps_v2_parameters_and_lets_domain_normalize_outputs() -> None:
    service = _VideoServiceSpy()
    transcript = {
        "language": "en",
        "segments": [
            {
                "segment_id": "seg_000001",
                "start_ms": 0,
                "end_ms": 1000,
                "text": "Source text",
            }
        ],
    }
    request = _request(
        VIDEO_PRODUCER_V2.key,
        outputs=("faithful-edition", "knowledge-note", "faithful-edition"),
        parameters={
            "provider_profile": "provider-main",
            "model_override": "model-main",
            "transcriber_profile": "transcriber-main",
            "output_language": "zh-CN",
            "quality_preset": "balanced",
            "faithful_language_policy": "translate-to-output",
            "style": "structured",
            "screenshot_policy": "on_demand",
            "provided_transcript": transcript,
        },
    )

    VideoRecipeAdapter(service).submit(request)  # type: ignore[arg-type]

    mapped = service.requests[0]
    assert mapped.request_schema_version == 2
    assert mapped.requested_outputs == (
        VideoDocumentKind.KNOWLEDGE_NOTE,
        VideoDocumentKind.FAITHFUL_EDITION,
    )
    assert tuple(output.recipe_id for output in mapped.resolved_outputs or ()) == (
        "alltonote.video-course-note",
        "alltonote.video-faithful-edition",
    )
    assert mapped.faithful_language_policy is FaithfulLanguagePolicy.TRANSLATE_TO_OUTPUT
    assert mapped.provided_transcript is not None
    assert mapped.provided_transcript.segments[0].text == "Source text"


def test_video_adapter_preserves_explicit_snapshot_and_output_bindings() -> None:
    service = _VideoServiceSpy()
    snapshot = effective_runtime_config(
        RuntimeConfig(default_workspace=Path("C:/vault"))
    ).job_snapshot()
    request = _request(
        VIDEO_PRODUCER_V2.key,
        parameters={
            "resolved_outputs": [
                {
                    "document_kind": "knowledge-note",
                    "recipe_id": "alltonote.video-course-note",
                    "recipe_version": 2,
                    "quality_preset": "balanced",
                }
            ],
            "config_snapshot": {
                "snapshot_version": snapshot.snapshot_version,
                "values": dict(snapshot.values),
                "digest": snapshot.digest,
                "semantic_digest": snapshot.semantic_digest,
            },
        },
    )

    VideoRecipeAdapter(service).submit(request)  # type: ignore[arg-type]

    mapped = service.requests[0]
    assert mapped.config_snapshot == snapshot
    assert mapped.resolved_outputs is not None
    assert mapped.resolved_outputs[0].recipe_id == "alltonote.video-course-note"
    assert mapped.resolved_outputs[0].recipe_version == 2


def test_legacy_v1_request_round_trips_through_generic_adapter() -> None:
    legacy = VideoProduceRequest(
        request_schema_version=1,
        workspace_root=Path("C:/vault"),
        input_value="fixture://course",
        provider_profile="provider-main",
        model_override="model-main",
        transcriber_profile="transcriber-main",
        output_language="en-US",
        quality_preset="strict",
        style="outline",
        screenshot_policy=ScreenshotPolicy.ON_DEMAND,
        principal="agent-a",
        client_request_id="legacy-v1",
    )
    service = _VideoServiceSpy()

    generic = adapt_video_produce_request(legacy)
    VideoRecipeAdapter(service).submit(generic)  # type: ignore[arg-type]

    assert service.requests == [legacy]


def test_legacy_v2_request_round_trips_through_generic_adapter() -> None:
    snapshot = effective_runtime_config(
        RuntimeConfig(default_workspace=Path("C:/vault"))
    ).job_snapshot()
    legacy = VideoProduceRequest(
        request_schema_version=2,
        workspace_root=Path("C:/vault"),
        input_value="fixture://course",
        recipe_id="alltonote.video-producer",
        recipe_version=2,
        provider_profile="provider-main",
        model_override="model-main",
        transcriber_profile="transcriber-main",
        output_language="zh-CN",
        quality_preset="balanced",
        requested_outputs=(
            VideoDocumentKind.FAITHFUL_EDITION,
            VideoDocumentKind.KNOWLEDGE_NOTE,
        ),
        faithful_language_policy=FaithfulLanguagePolicy.TRANSLATE_TO_OUTPUT,
        screenshot_policy=ScreenshotPolicy.ON_DEMAND,
        principal="agent-a",
        client_request_id="legacy-v2",
        config_snapshot=snapshot,
    )
    service = _VideoServiceSpy()

    generic = adapt_video_produce_request(legacy)
    VideoRecipeAdapter(service).submit(generic)  # type: ignore[arg-type]

    assert service.requests == [legacy]


@pytest.mark.parametrize(
    "produce_request",
    [
        _request(RecipeKey("unknown.video", 1)),
        ProduceRequest(
            1,
            VIDEO_COURSE_NOTE_V1.key,
            InputDescriptor("document", "fixture://course"),
            "C:/vault",
            ("knowledge-note",),
        ),
        _request(VIDEO_COURSE_NOTE_V1.key, parameters={"api_key": "secret-canary"}),
        _request(VIDEO_COURSE_NOTE_V1.key, attributes={"source_id": "different"}),
    ],
)
def test_video_adapter_rejects_unsupported_inputs_before_delegate(
    produce_request: ProduceRequest,
) -> None:
    service = _VideoServiceSpy()

    with pytest.raises(DomainError) as raised:
        VideoRecipeAdapter(service).submit(produce_request)  # type: ignore[arg-type]

    assert raised.value.category is ErrorCategory.INVALID_REQUEST
    assert "secret-canary" not in str(raised.value)
    assert service.requests == []


def test_video_adapter_propagates_video_service_error_by_identity() -> None:
    sentinel = DomainError("video_failed", ErrorCategory.RECIPE_FAILED, "Video failed")

    class FailingService:
        def submit_video(
            self,
            request: VideoProduceRequest,
            *,
            execution_owner: JobExecutionOwner,
        ) -> object:
            del request, execution_owner
            raise sentinel

    with pytest.raises(DomainError) as raised:
        VideoRecipeAdapter(FailingService()).submit(  # type: ignore[arg-type]
            _request(VIDEO_COURSE_NOTE_V1.key)
        )
    assert raised.value is sentinel


def test_video_descriptors_are_static_and_cold_importable() -> None:
    assert VIDEO_DESCRIPTORS == (VIDEO_COURSE_NOTE_V1, VIDEO_PRODUCER_V2)
    assert VIDEO_COURSE_NOTE_V1.output_kinds == ("knowledge-note",)
    assert VIDEO_PRODUCER_V2.output_kinds == ("knowledge-note", "faithful-edition")
    code = """
import json, sys
import app.core.recipes.video.descriptor
blocked = sorted(name for name in sys.modules if name in {
    'app.core.domain.video', 'app.core.application.video_service', 'app.runtime'
} or name.startswith(('fastapi', 'torch', 'faster_whisper', 'yt_dlp', 'openai')))
print(json.dumps(blocked))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[2],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == []

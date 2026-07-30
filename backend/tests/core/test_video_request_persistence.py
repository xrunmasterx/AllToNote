from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.core.application.produce_service import ProduceService
from app.core.application.video_service import VideoService
from app.core.config.model import JobConfigSnapshot
from app.core.sdk import AllToNoteSDK
from app.core.domain.ids import sha256_digest
from app.core.domain.video import (
    FaithfulLanguagePolicy,
    TranscriptDocument,
    TranscriptSegment,
    VideoDocumentKind,
    VideoProduceRequest,
)
from app.core.errors import DomainError
from app.core.recipes.contracts import InputDescriptor, ProduceRequest
from app.core.recipes.registry import RecipeRegistry
from app.core.recipes.video.adapter import (
    VideoRecipeAdapter,
    adapt_video_produce_request,
)
from app.core.recipes.video.descriptor import VIDEO_COURSE_NOTE_V1, VIDEO_PRODUCER_V2


def _service(tmp_path: Path) -> tuple[VideoService, SqliteJobRepository]:
    repository = SqliteJobRepository.open(tmp_path / "machine")
    service = VideoService(
        repository,
        attempt_storage=None,  # type: ignore[arg-type]
        portable=None,  # type: ignore[arg-type]
        operations=None,  # type: ignore[arg-type]
        checkpoint_reader=lambda _: b"",
        owner_id="request-persistence-test",
        work_root=tmp_path / "work",
        local_instance_id="request-persistence-test",
    )
    return service, repository


def test_portable_timestamps_are_stably_ordered_after_source_observation() -> None:
    observed = "2030-01-02T03:04:05.678Z"

    started, completed, created = VideoService._timestamps(
        "job_019f6abf-1fec-7fb4-a8b0-6f40f09b072c",
        observed,
    )

    parse = lambda value: datetime.fromisoformat(
        value.removesuffix("Z") + "+00:00"
    )
    assert parse(started) < parse(completed) < parse(created)
    assert parse(completed) == parse(observed)
    assert VideoService._timestamps(
        "job_019f6abf-1fec-7fb4-a8b0-6f40f09b072c",
        observed,
    ) == (started, completed, created)


def test_portable_timestamps_reject_invalid_source_observation() -> None:
    with pytest.raises(DomainError, match="source_metadata_invalid"):
        VideoService._timestamps(
            "job_019f6abf-1fec-7fb4-a8b0-6f40f09b072c",
            "not-a-timestamp",
        )


def test_v1_job_request_json_and_hash_remain_frozen(tmp_path: Path) -> None:
    service, repository = _service(tmp_path)
    request = VideoProduceRequest(
        request_schema_version=1,
        workspace_root=Path("C:/vault"),
        input_value="fixture://course",
        client_request_id="v1-golden",
    )
    expected = {
        "input_value": "fixture://course",
        "model_override": None,
        "output_language": "zh-CN",
        "provided_transcript": None,
        "provider_profile": "default",
        "quality_preset": "balanced",
        "recipe_id": "alltonote.video-course-note",
        "recipe_version": 1,
        "request_schema_version": 1,
        "screenshot_policy": "off",
        "style": "structured",
        "transcriber_profile": "default",
        "workspace_root": str(Path("C:/vault")),
    }
    expected_json = json.dumps(
        expected,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )

    submitted = service.submit_video(request)

    assert repository.get_job_request(submitted.job_id) == expected_json
    assert repository.get_job(submitted.job_id).request_hash == sha256_digest(
        expected_json
    )
    assert VideoService._request_hash(request) == (
        "sha256:efb74f2a44a2706401bc60325655ba61ec3db31429eefac3e6efc1ca50297c85"
    )


def test_sdk_submit_video_is_direct_queued_durable_boundary(tmp_path: Path) -> None:
    service, repository = _service(tmp_path)
    request = VideoProduceRequest(
        request_schema_version=1,
        workspace_root=Path("C:/vault"),
        input_value="fixture://course",
        client_request_id="sdk-submit",
    )

    sdk = AllToNoteSDK(
        ProduceService(RecipeRegistry(((VIDEO_COURSE_NOTE_V1, VideoRecipeAdapter(service)),))),
        service,
        adapt_video_produce_request,
    )
    submitted = sdk.submit_video(request)
    stored = repository.get_job(submitted.job_id)

    assert submitted.state.value == "queued"
    assert stored.state.value == "queued"
    assert repository.get_job_request(submitted.job_id) is not None
    assert stored.request_hash is not None


def test_v2_job_request_round_trips_canonical_outputs_and_bindings(
    tmp_path: Path,
) -> None:
    service, repository = _service(tmp_path)
    transcript = TranscriptDocument(
        "en",
        (TranscriptSegment("seg_000001", 0, 1_000, "Source text"),),
    )
    request = VideoProduceRequest(
        request_schema_version=2,
        workspace_root=Path("C:/vault"),
        input_value="https://example.test/video",
        recipe_id="alltonote.video-producer",
        recipe_version=2,
        provider_profile="openai-main",
        model_override="gpt-test",
        transcriber_profile="local-whisper",
        output_language="zh-CN",
        requested_outputs=(
            VideoDocumentKind.FAITHFUL_EDITION,
            VideoDocumentKind.KNOWLEDGE_NOTE,
            VideoDocumentKind.FAITHFUL_EDITION,
        ),
        faithful_language_policy=FaithfulLanguagePolicy.TRANSLATE_TO_OUTPUT,
        client_request_id="v2-canonical",
        provided_transcript=transcript,
    )
    canonical_request = VideoProduceRequest(
        request_schema_version=2,
        workspace_root=request.workspace_root,
        input_value=request.input_value,
        recipe_id=request.recipe_id,
        recipe_version=request.recipe_version,
        provider_profile=request.provider_profile,
        model_override=request.model_override,
        transcriber_profile=request.transcriber_profile,
        output_language=request.output_language,
        requested_outputs=(
            VideoDocumentKind.KNOWLEDGE_NOTE,
            VideoDocumentKind.FAITHFUL_EDITION,
        ),
        faithful_language_policy=request.faithful_language_policy,
        client_request_id=request.client_request_id,
        provided_transcript=transcript,
    )

    expected_json = (
        '{"faithful_language_policy":"translate-to-output",'
        '"input_value":"https://example.test/video","model_override":"gpt-test",'
        '"output_bindings":[{"document_kind":"knowledge-note",'
        '"quality_preset":"balanced","recipe_id":"alltonote.video-course-note",'
        '"recipe_version":2},{"document_kind":"faithful-edition",'
        '"quality_preset":"balanced",'
        '"recipe_id":"alltonote.video-faithful-edition","recipe_version":1}],'
        '"output_language":"zh-CN","provided_transcript":{"language":"en",'
        '"segments":[{"end_ms":1000,"segment_id":"seg_000001",'
        '"start_ms":0,"text":"Source text"}]},'
        '"provider_profile":"openai-main","quality_preset":"balanced",'
        '"recipe_id":"alltonote.video-producer","recipe_version":2,'
        '"request_schema_version":2,'
        '"requested_outputs":["knowledge-note","faithful-edition"],'
        '"screenshot_policy":"off","style":"structured",'
        '"transcriber_profile":"local-whisper","workspace_root":"C:\\\\vault"}'
    )

    submitted = service.submit_video(request)
    replay = service.submit_video(canonical_request)
    restored = service._load_request(submitted.job_id)

    assert replay.job_id == submitted.job_id
    assert repository.get_job_request(submitted.job_id) == expected_json
    assert repository.get_job(submitted.job_id).request_hash == (
        "sha256:38a48e85e8b1e4866ca972d834310f8828f3849cd15ce6d124b987a9d6f430b9"
    )
    assert restored == canonical_request
    assert VideoService._request_hash(request) == (
        "sha256:fb73f1a581a5fa272e95f71bb5af64b4ae7e96fdac4bea633180a6024fbcb9a8"
    )
    assert VideoService._request_hash(request) == VideoService._request_hash(
        canonical_request
    )


def test_generic_and_legacy_v1_submission_share_durable_identity(tmp_path: Path) -> None:
    service, repository = _service(tmp_path)
    legacy = VideoProduceRequest(
        request_schema_version=1,
        workspace_root=Path("C:/vault"),
        input_value="fixture://course",
        client_request_id="shared-v1",
    )
    generic = ProduceRequest(
        1,
        VIDEO_COURSE_NOTE_V1.key,
        InputDescriptor("source", "fixture://course"),
        "C:/vault",
        ("knowledge-note",),
        client_request_id="shared-v1",
    )

    legacy_submission = service.submit_video(legacy)
    generic_submission = VideoRecipeAdapter(service).submit(generic)

    assert generic_submission.job_id == legacy_submission.job_id
    assert repository.get_job_request(generic_submission.job_id) == repository.get_job_request(
        legacy_submission.job_id
    )
    assert repository.get_job(generic_submission.job_id).request_hash == repository.get_job(
        legacy_submission.job_id
    ).request_hash
    assert VideoService._request_hash(legacy) == VideoService._request_hash(
        service._load_request(generic_submission.job_id)
    )


def test_generic_and_legacy_v2_submission_share_durable_identity(tmp_path: Path) -> None:
    service, repository = _service(tmp_path)
    transcript = TranscriptDocument(
        "en",
        (TranscriptSegment("seg_000001", 0, 1_000, "Source text"),),
    )
    snapshot_digest = sha256_digest("{}")
    config_snapshot = JobConfigSnapshot(
        1,
        {},
        snapshot_digest,
        snapshot_digest,
    )
    legacy = VideoProduceRequest(
        request_schema_version=2,
        workspace_root=Path("C:/vault"),
        input_value="https://example.test/video",
        recipe_id="alltonote.video-producer",
        recipe_version=2,
        provider_profile="openai-main",
        model_override="gpt-test",
        transcriber_profile="local-whisper",
        requested_outputs=(
            VideoDocumentKind.FAITHFUL_EDITION,
            VideoDocumentKind.KNOWLEDGE_NOTE,
        ),
        faithful_language_policy=FaithfulLanguagePolicy.TRANSLATE_TO_OUTPUT,
        client_request_id="shared-v2",
        principal="agent-a",
        provided_transcript=transcript,
        config_snapshot=config_snapshot,
    )
    generic = ProduceRequest(
        1,
        VIDEO_PRODUCER_V2.key,
        InputDescriptor("source", "https://example.test/video"),
        "C:/vault",
        ("faithful-edition", "knowledge-note"),
        {
            "provider_profile": "openai-main",
            "model_override": "gpt-test",
            "transcriber_profile": "local-whisper",
            "faithful_language_policy": "translate-to-output",
            "provided_transcript": {
                "language": "en",
                "segments": [
                    {
                        "segment_id": "seg_000001",
                        "start_ms": 0,
                        "end_ms": 1_000,
                        "text": "Source text",
                    }
                ],
            },
            "config_snapshot": {
                "snapshot_version": config_snapshot.snapshot_version,
                "values": dict(config_snapshot.values),
                "digest": config_snapshot.digest,
                "semantic_digest": config_snapshot.semantic_digest,
            },
        },
        principal="agent-a",
        client_request_id="shared-v2",
    )

    generic_submission = VideoRecipeAdapter(service).submit(generic)
    legacy_submission = service.submit_video(legacy)
    restored = service._load_request(generic_submission.job_id)

    assert generic_submission.job_id == legacy_submission.job_id
    assert restored == legacy
    assert restored.principal == "agent-a"
    assert [
        (event.event_type, json.loads(event.payload_json))
        for event in repository.list_events(generic_submission.job_id)
    ] == [
        (
            "configuration.snapshot.v1",
            {
                "digest": config_snapshot.digest,
                "semantic_digest": config_snapshot.semantic_digest,
                "snapshot_version": config_snapshot.snapshot_version,
                "values": dict(config_snapshot.values),
            },
        )
    ]
    assert repository.get_job(generic_submission.job_id).request_hash == repository.get_job(
        legacy_submission.job_id
    ).request_hash
    assert VideoService._request_hash(restored) == VideoService._request_hash(legacy)


def test_v2_job_request_rejects_modified_recipe_binding(tmp_path: Path) -> None:
    service, repository = _service(tmp_path)
    submitted = service.submit_video(
        VideoProduceRequest(
            request_schema_version=2,
            workspace_root=tmp_path / "vault",
            input_value="fixture://course",
            recipe_id="alltonote.video-producer",
            recipe_version=2,
        )
    )
    stored = json.loads(repository.get_job_request(submitted.job_id) or "null")
    stored["output_bindings"][0]["recipe_version"] = 999
    with repository._transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE jobs SET request_json = ? WHERE job_id = ?",
            (json.dumps(stored, sort_keys=True, separators=(",", ":")), submitted.job_id),
        )

    with pytest.raises(DomainError, match="job_request_invalid"):
        service._load_request(submitted.job_id)

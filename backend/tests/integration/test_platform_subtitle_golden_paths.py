from __future__ import annotations

import json
import multiprocessing
import os
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

from app.adapters.models.legacy_gpt import (
    LegacyKnownRetryableModelFailure,
    LegacyModelBinding,
    LegacyModelCapabilities,
    LegacyModelResponse,
)
from app.adapters.sources.legacy_video import (
    LegacyNoSubtitleError,
    LegacyVideoSourceAdapter,
)
from app.core.application.video_acquisition import transcript_identity
from app.core.application.video_checkpoints import (
    decode_acquired,
    decode_source,
    encode_source,
)
from app.core.application.video_service import VideoService
from app.core.domain.video import (
    JobState,
    QualityOverall,
    TranscriptDocument,
    TranscriptSegment,
    VideoProduceRequest,
)
from app.core.jobs.model import AttemptState
import app.runtime as runtime_module


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures"
WORKSPACE_FIXTURE = FIXTURE_ROOT / "workspace-v2"
PLATFORM_FIXTURES = FIXTURE_ROOT / "video"
PLATFORM_URLS = {
    "bilibili": "https://www.bilibili.com/video/BV1Golden15?p=1",
    "youtube": "https://www.youtube.com/watch?v=GoldenPath1",
}


def test_platform_runtime_composition_is_available() -> None:
    assert callable(getattr(runtime_module, "create_platform_video_runtime", None))


def test_resolved_platform_source_checkpoint_round_trips() -> None:
    source = LegacyVideoSourceAdapter(local_machine_id="fixture-machine").resolve(
        PLATFORM_URLS["youtube"]
    )

    assert decode_source(encode_source(source)) == source


@dataclass
class Calls:
    metadata: int = 0
    subtitle: int = 0
    media_download: int = 0
    ffmpeg: int = 0
    transcriber: int = 0
    model: int = 0


class _FixtureDownloader:
    def __init__(
        self,
        platform: str,
        calls: Calls,
        *,
        subtitle_outcome: str = "available",
    ) -> None:
        self._metadata = _fixture(platform, "metadata.json")
        self._subtitle = _fixture(platform, "subtitle.json")
        self._calls = calls
        self._subtitle_outcome = subtitle_outcome

    def download(self, _url: str, **kwargs: object) -> object:
        if kwargs.get("skip_download") is not True:
            self._calls.media_download += 1
        self._calls.metadata += 1
        return SimpleNamespace(
            title=self._metadata["title"],
            duration=self._metadata["duration_ms"] / 1000,
            cover_url=None,
        )

    def download_subtitles(self, _url: str, **_kwargs: object) -> object:
        self._calls.subtitle += 1
        if self._subtitle_outcome == "unavailable":
            raise LegacyNoSubtitleError
        if self._subtitle_outcome == "malformed":
            return SimpleNamespace(
                language=self._subtitle["language"],
                segments=[{"start": 0.0, "end": 1.0}],
            )
        return SimpleNamespace(
            language=self._subtitle["language"],
            segments=self._subtitle["segments"],
        )


class _Completion:
    def __init__(self, calls: Calls, failure: str | None = None) -> None:
        self._calls = calls
        self._failure = failure

    def complete_once(self, prompt: str) -> LegacyModelResponse:
        assert "seg_000001" in prompt
        self._calls.model += 1
        if self._failure == "known":
            raise LegacyKnownRetryableModelFailure
        if self._failure == "unknown":
            raise TimeoutError("provider response was lost")
        return LegacyModelResponse(
            markdown=(
                "# Video note\n\n"
                "## Verifiable source\n\n"
                "The note preserves evidence from the transcript.[^seg_000001]\n"
            ),
            provider_request_id="fixture-request-15",
            input_tokens=20,
            output_tokens=10,
            actual_model="fixture/model-v1",
        )


class _TerminateBeforeModel:
    def __init__(self, delegate: object) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    def generate_draft(self, *_args: object, **_kwargs: object) -> object:
        os._exit(23)


def _fixture(platform: str, name: str) -> dict[str, object]:
    return json.loads((PLATFORM_FIXTURES / platform / name).read_text("utf-8"))


def _create_runtime(
    machine_root: Path,
    platform: str,
    calls: Calls,
    *,
    failure: str | None = None,
    subtitle_outcome: str = "available",
    owner_id: str | None = None,
    now_ms: int | None = None,
) -> object:
    downloader = _FixtureDownloader(
        platform,
        calls,
        subtitle_outcome=subtitle_outcome,
    )
    source = LegacyVideoSourceAdapter(
        local_machine_id="fixture-machine",
        factories={platform: lambda: downloader},
    )
    model = LegacyModelBinding(
        provider_kind="fixture/provider-v1",
        model_identity="fixture/model-v1",
        bridge=_Completion(calls, failure),
        capabilities=LegacyModelCapabilities(),
    )
    return runtime_module.create_platform_video_runtime(
        machine_root,
        source=source,
        source_metadata={platform: _fixture(platform, "metadata.json")},
        model=model,
        owner_id=owner_id,
        clock=(None if now_ms is None else lambda: now_ms),
    )


def _terminate_job_before_model(machine_root: str, job_id: str) -> None:
    runtime = _create_runtime(
        Path(machine_root),
        "youtube",
        Calls(),
        owner_id="terminated-process",
        now_ms=1_000,
    )
    service = runtime._sdk._video_service
    service._operations = _TerminateBeforeModel(service._operations)
    runtime.wait_job(job_id)
    os._exit(24)


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    shutil.copytree(WORKSPACE_FIXTURE, root)
    shutil.rmtree(root / "raw" / "personal" / ".staging")
    for relative in (
        "raw/common",
        "raw/personal/.staging",
        "wiki/common",
        "wiki/personal",
        ".cache",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def runtime_factory(tmp_path: Path) -> Callable[..., tuple[object, Calls]]:
    created = 0

    def create(
        *,
        platform: str,
        failure: str | None = None,
        subtitle_outcome: str = "available",
        machine_root: Path | None = None,
        owner_id: str | None = None,
        clock: Callable[[], int] | None = None,
    ) -> tuple[object, Calls]:
        nonlocal created
        created += 1
        calls = Calls()
        runtime = _create_runtime(
            machine_root or tmp_path / f"machine-{created}",
            platform,
            calls,
            failure=failure,
            subtitle_outcome=subtitle_outcome,
            owner_id=owner_id,
            now_ms=(clock() if clock is not None else None),
        )
        return runtime, calls

    return create


def request(
    platform: str,
    workspace_root: Path,
    *,
    provided_transcript: TranscriptDocument | None = None,
    client_request_id: str | None = None,
) -> VideoProduceRequest:
    return VideoProduceRequest(
        request_schema_version=1,
        workspace_root=workspace_root,
        input_value=PLATFORM_URLS[platform],
        client_request_id=client_request_id or f"subtitle-{platform}",
        provided_transcript=provided_transcript,
    )


def _decode_acquisition_checkpoint(runtime: object, job_id: str) -> object:
    metadata = runtime.job_repository.latest_checkpoint(job_id, "acquire")
    assert metadata is not None
    payload_path = (
        runtime.job_repository.machine_root.parent
        / "attempts"
        / metadata.relative_path
    )
    return decode_acquired(payload_path.read_bytes())


def _assert_challenge_matches_unknown_operations(runtime: object, job_id: str) -> None:
    _, _, challenge = runtime.job_repository.get_job_details(job_id)
    assert challenge is not None
    prompt = json.loads(challenge.prompt_json)
    with runtime.job_repository._connect() as connection:
        rows = connection.execute(
            """
            SELECT operation_id FROM external_operations
            WHERE job_id = ? AND step_id = 'generate_draft'
              AND outcome = 'external_outcome_unknown'
            ORDER BY operation_id
            """,
            (job_id,),
        ).fetchall()
    operation_ids = [row[0] for row in rows]
    assert operation_ids
    assert prompt == {
        "code": "external_outcome_unknown",
        "operation_ids": operation_ids,
    }


@pytest.mark.parametrize("platform", ["bilibili", "youtube"])
def test_platform_subtitle_path_commits_without_media(
    platform: str,
    runtime_factory: Callable[..., tuple[object, Calls]],
    workspace_root: Path,
) -> None:
    runtime, calls = runtime_factory(platform=platform)

    result = runtime.wait_job(runtime.submit_video(request(platform, workspace_root)).job_id)

    assert result.state is JobState.SUCCEEDED
    assert result.result is not None
    assert result.result.bundle_id.startswith("bnd_")
    assert result.result.quality_overall is QualityOverall.PASS
    assert calls.subtitle == 1
    assert calls.media_download == 0
    assert calls.transcriber == 0
    assert calls.ffmpeg == 0
    assert calls.model == 1
    bundle = workspace_root / result.result.workspace_relative_bundle_path
    draft = (bundle / "drafts" / f"{result.result.primary_draft_artifact_id}.md").read_text(
        "utf-8"
    )
    assert "[^seg_" not in draft
    assert "[^ev_" in draft
    assert "]: Video 00:00.000-00:02.000" in draft
    receipt = json.loads((bundle / "receipt.json").read_text("utf-8"))
    transcriber = next(
        executor
        for executor in receipt["executors"]
        if executor["kind"] == "transcriber"
    )
    assert transcriber["identity"] == f"{platform}/platform-subtitle-v1"


@pytest.mark.parametrize("subtitle_outcome", ["unavailable", "malformed"])
def test_provided_transcript_precedes_lower_priority_platform_subtitle(
    subtitle_outcome: str,
    runtime_factory: Callable[..., tuple[object, Calls]],
    workspace_root: Path,
) -> None:
    provided = TranscriptDocument(
        language="en",
        segments=(TranscriptSegment("seg_000001", 0, 1000, "provided wins"),),
    )
    runtime, calls = runtime_factory(
        platform="youtube",
        subtitle_outcome=subtitle_outcome,
    )
    provided_request = request(
        "youtube",
        workspace_root,
        provided_transcript=provided,
        client_request_id="provided-transcript",
    )
    submitted = runtime.submit_video(provided_request)
    result = runtime.wait_job(submitted.job_id)

    assert result.state is JobState.SUCCEEDED
    assert result.result is not None
    assert calls.metadata == 1
    assert calls.subtitle == 0
    acquisition = _decode_acquisition_checkpoint(runtime, submitted.job_id)
    assert acquisition.transcript == provided
    assert acquisition.transcript_identity == transcript_identity(provided)
    assert acquisition.metadata.subtitle_acquisition == "provided"
    assert acquisition.subtitle_availability.value == "available"
    assert acquisition.transcript_provenance.value == "provided"
    bundle = workspace_root / result.result.workspace_relative_bundle_path
    transcript = (bundle / "evidence" / "transcript.jsonl").read_text("utf-8")
    assert "provided wins" in transcript


@pytest.mark.parametrize(
    ("field_name", "changed_value"),
    [
        ("provider_profile", "provider-two"),
        ("model_override", "model-two"),
        ("transcriber_profile", "transcriber-two"),
        (
            "provided_transcript",
            TranscriptDocument(
                language="en",
                segments=(
                    TranscriptSegment("seg_000001", 0, 1_000, "provided fingerprint"),
                ),
            ),
        ),
    ],
)
def test_request_fingerprint_changes_for_each_execution_input(
    field_name: str,
    changed_value: object,
    workspace_root: Path,
) -> None:
    baseline = request("youtube", workspace_root)

    assert VideoService._request_hash(baseline) != VideoService._request_hash(
        replace(baseline, **{field_name: changed_value})
    )


def test_known_model_failure_is_terminal_without_challenge(
    runtime_factory: Callable[..., tuple[object, Calls]],
    workspace_root: Path,
) -> None:
    runtime, calls = runtime_factory(platform="youtube", failure="known")

    result = runtime.wait_job(runtime.submit_video(request("youtube", workspace_root)).job_id)

    assert result.state is JobState.FAILED
    assert result.error is not None
    assert result.error.code == "model_generation_failed"
    assert result.challenge_id is None
    assert calls.model == 1


def test_unknown_model_outcome_pauses_once_without_resending(
    runtime_factory: Callable[..., tuple[object, Calls]],
    workspace_root: Path,
) -> None:
    runtime, calls = runtime_factory(platform="bilibili", failure="unknown")
    submitted = runtime.submit_video(request("bilibili", workspace_root))

    first = runtime.wait_job(submitted.job_id)
    second = runtime.wait_job(submitted.job_id)

    assert first.state is second.state is JobState.WAITING_FOR_INPUT
    assert first.challenge_id is not None
    assert second.challenge_id == first.challenge_id
    assert calls.model == 1
    _assert_challenge_matches_unknown_operations(runtime, submitted.job_id)
    with runtime.job_repository._connect() as connection:
        attempt_state = connection.execute(
            "SELECT state FROM attempts WHERE job_id = ? AND step_id = 'generate_draft'",
            (submitted.job_id,),
        ).fetchone()[0]
    assert attempt_state == AttemptState.NEEDS_INPUT.value


def test_process_loss_before_model_reuses_acquisition_checkpoints(
    runtime_factory: Callable[..., tuple[object, Calls]],
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    machine_root = tmp_path / "recovery-machine"
    first_runtime, _ = runtime_factory(
        platform="youtube",
        machine_root=machine_root,
        owner_id="submitting-process",
        clock=lambda: 1_000,
    )
    submitted = first_runtime.submit_video(request("youtube", workspace_root))
    process = multiprocessing.get_context("spawn").Process(
        target=_terminate_job_before_model,
        args=(str(machine_root), submitted.job_id),
    )
    process.start()
    process.join(timeout=30)
    assert process.exitcode == 23

    recovered_runtime, recovered_calls = runtime_factory(
        platform="youtube",
        machine_root=machine_root,
        owner_id="recovery-process",
        clock=lambda: 302_001,
    )
    result = recovered_runtime.wait_job(submitted.job_id)

    assert result.state is JobState.SUCCEEDED
    assert recovered_calls.model == 1
    assert recovered_calls.metadata == 0
    assert recovered_calls.subtitle == 0
    assert recovered_calls.media_download == 0
    assert recovered_calls.transcriber == 0
    with recovered_runtime.job_repository._connect() as connection:
        states = connection.execute(
            "SELECT state FROM attempts WHERE job_id = ? AND step_id = 'generate_draft' ORDER BY rowid",
            (submitted.job_id,),
        ).fetchall()
        acquisition_attempts = connection.execute(
            "SELECT state FROM attempts WHERE job_id = ? AND step_id = 'acquire'",
            (submitted.job_id,),
        ).fetchall()
    assert [row[0] for row in states] == [
        AttemptState.INTERRUPTED.value,
        AttemptState.SUCCEEDED.value,
    ]
    assert [row[0] for row in acquisition_attempts] == [AttemptState.SUCCEEDED.value]


def test_reopen_with_started_model_operation_pauses_before_provider_resend(
    runtime_factory: Callable[..., tuple[object, Calls]],
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    machine_root = tmp_path / "unknown-recovery-machine"
    first_runtime, _ = runtime_factory(
        platform="bilibili",
        machine_root=machine_root,
        owner_id="lost-process",
        clock=lambda: 1_000,
    )
    submitted = first_runtime.submit_video(request("bilibili", workspace_root))
    repository = first_runtime.job_repository
    repository.transition_job(submitted.job_id, JobState.RUNNING)
    old_authority = repository.acquire_scheduler_lease(
        "lost-process", ttl_seconds=1
    )
    old_attempt = repository.start_attempt(
        repository.create_attempt(submitted.job_id, "generate_draft").attempt_id,
        old_authority,
    )
    operation = repository.prepare_external_operation(
        job_id=submitted.job_id,
        step_id=old_attempt.step_id,
        attempt_id=old_attempt.attempt_id,
        provider="fixture/provider-v1",
        request_hash="sha256:" + "1" * 64,
        operation_idempotency_key=None,
        summary_json="{}",
        authority=old_authority,
    )
    repository.start_external_operation(operation.operation_id, old_authority)

    recovered_runtime, recovered_calls = runtime_factory(
        platform="bilibili",
        machine_root=machine_root,
        owner_id="recovery-process",
        clock=lambda: 2_001,
    )
    first = recovered_runtime.wait_job(submitted.job_id)
    second = recovered_runtime.wait_job(submitted.job_id)

    assert first.state is second.state is JobState.WAITING_FOR_INPUT
    assert first.challenge_id == second.challenge_id
    assert recovered_calls.model == 0
    assert recovered_runtime.job_repository.get_external_operation(
        operation.operation_id
    ).outcome.value == "external_outcome_unknown"
    _assert_challenge_matches_unknown_operations(
        recovered_runtime, submitted.job_id
    )
    with recovered_runtime.job_repository._connect() as connection:
        states = connection.execute(
            "SELECT state FROM attempts WHERE job_id = ? AND step_id = 'generate_draft' ORDER BY rowid",
            (submitted.job_id,),
        ).fetchall()
    assert [row[0] for row in states] == [
        AttemptState.INTERRUPTED.value,
        AttemptState.NEEDS_INPUT.value,
    ]

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import json
import os
import subprocess
import sys
from types import MappingProxyType

import pytest

from app.adapters.transcription.legacy_transcriber import (
    ExternalOutcomeUnknownError,
    LegacyRemoteCallFailed,
    LegacyTranscriberAdapter,
    RemoteTranscriptResultStore,
    RemoteTranscriptionBinding,
    normalize_legacy_transcript,
    normalize_platform_subtitle,
)
from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.core.domain.ids import sha256_digest
from app.core.domain.video import JobState, TranscriptDocument
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.external_operation import (
    ExternalOperation,
    ExternalOperationGuard,
    ExternalOutcome,
)
from app.core.ports.transcript import MediaInput
from app.downloaders.youtube_subtitle import YouTubeSubtitleFetcher
from app.models.transcriber_model import TranscriptResult, TranscriptSegment


class _Token:
    def __init__(self, cancel_on_check: int | None = None) -> None:
        self.checks = 0
        self.cancel_on_check = cancel_on_check

    def raise_if_cancelled(self) -> None:
        self.checks += 1
        if self.checks == self.cancel_on_check:
            raise DomainError(
                "job_cancelled",
                ErrorCategory.CANCELLED,
                "Job cancellation was requested",
            )


class _Transcriber:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[str] = []

    def transcript(self, file_path: str) -> object:
        self.calls.append(file_path)
        return self.result


class _RaisingTranscriber:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def transcript(self, _file_path: str) -> object:
        self.calls += 1
        raise self.error


class _RecordingGuard:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.current: ExternalOperation | None = None

    def prepare(self, **fields: object) -> ExternalOperation:
        self.events.append("prepare")
        if self.current is not None:
            if self.current.outcome is ExternalOutcome.UNKNOWN:
                raise DomainError(
                    "external_outcome_unknown",
                    ErrorCategory.CONFLICT,
                    "Unknown external outcome requires explicit confirmation",
                )
            if self.current.outcome is ExternalOutcome.SUCCEEDED:
                return self.current
        self.current = ExternalOperation(
            operation_id="op_0190b397-fa00-7000-8000-000000000001",
            job_id=str(fields["job_id"]),
            step_id=str(fields["step_id"]),
            attempt_id=str(fields["attempt_id"]),
            provider=str(fields["provider"]),
            request_hash=str(fields["request_hash"]),
            operation_idempotency_key=None,
            provider_request_id=None,
            outcome=ExternalOutcome.PREPARED,
            summary_json=str(fields["summary_json"]),
            created_at="2026-07-15T00:00:00.000Z",
            updated_at="2026-07-15T00:00:00.000Z",
        )
        return self.current

    def start(self, _operation_id: str) -> ExternalOperation:
        self.events.append("start")
        assert self.current is not None
        self.current = replace(self.current, outcome=ExternalOutcome.STARTED)
        return self.current

    def succeed(
        self,
        _operation_id: str,
        *,
        provider_request_id: str | None,
        summary_json: str,
    ) -> ExternalOperation:
        self.events.append("succeed")
        assert self.current is not None
        self.current = replace(
            self.current,
            outcome=ExternalOutcome.SUCCEEDED,
            provider_request_id=provider_request_id,
            summary_json=summary_json,
        )
        return self.current

    def fail(self, _operation_id: str, *, summary_json: str) -> ExternalOperation:
        self.events.append("fail")
        assert self.current is not None
        self.current = replace(
            self.current,
            outcome=ExternalOutcome.FAILED,
            summary_json=summary_json,
        )
        return self.current

    def unknown(self, _operation_id: str, *, summary_json: str) -> ExternalOperation:
        self.events.append("unknown")
        assert self.current is not None
        self.current = replace(
            self.current,
            outcome=ExternalOutcome.UNKNOWN,
            summary_json=summary_json,
        )
        return self.current


def _remote_binding(
    tmp_path: Path,
    guard: _RecordingGuard,
) -> RemoteTranscriptionBinding:
    return RemoteTranscriptionBinding(
        guard=guard,
        result_store=RemoteTranscriptResultStore(tmp_path / "operation-results"),
        job_id="job_0190b397-fa00-7000-8000-000000000001",
        step_id="normalize-transcript",
        attempt_id="att_0190b397-fa00-7000-8000-000000000001",
        request_hash=sha256_digest(b"same media bytes and transcriber policy"),
    )


def _legacy_result(
    *segments: TranscriptSegment,
    language: str | None = "zh",
) -> TranscriptResult:
    return TranscriptResult(language, "ignored", list(segments), raw={"secret": "raw"})


def test_seconds_convert_outward_without_truncating_evidence() -> None:
    result = normalize_legacy_transcript(
        _legacy_result(TranscriptSegment(1.0019, 2.0091, " text "))
    )

    assert result == TranscriptDocument(
        "zh",
        (
            result.segments[0],
        ),
    )
    assert result.segments[0].start_ms == 1001
    assert result.segments[0].end_ms == 2010
    assert result.segments[0].segment_id == "seg_000001"
    assert result.segments[0].text == "text"


@pytest.mark.parametrize(
    "segment",
    [
        TranscriptSegment(True, 2.0, "text"),
        TranscriptSegment(float("nan"), 2.0, "text"),
        TranscriptSegment(float("-inf"), 2.0, "text"),
        TranscriptSegment(1.0, float("inf"), "text"),
        TranscriptSegment(-0.1, 1.0, "text"),
        TranscriptSegment(1.0, 1.0, "text"),
        TranscriptSegment(2.0, 1.0, "text"),
        TranscriptSegment(1.0, 2.0, "   "),
    ],
)
def test_any_invalid_segment_rejects_the_entire_provider_response(
    segment: TranscriptSegment,
) -> None:
    response = _legacy_result(
        TranscriptSegment(0.0, 0.5, "valid"),
        segment,
    )

    with pytest.raises(DomainError, match="transcript_segment_invalid"):
        normalize_legacy_transcript(response)


def test_empty_provider_response_is_rejected() -> None:
    with pytest.raises(DomainError, match="transcript_empty"):
        normalize_legacy_transcript(_legacy_result())


def test_provider_segment_order_is_not_silently_rewritten() -> None:
    with pytest.raises(DomainError) as caught:
        normalize_legacy_transcript(
            _legacy_result(
                TranscriptSegment(2.0, 3.0, "later"),
                TranscriptSegment(1.0, 1.5, "earlier"),
            )
        )

    assert caught.value.code == "transcript_order_invalid"
    assert caught.value.category is ErrorCategory.RECIPE_FAILED


def test_missing_language_uses_und_without_reading_full_text_or_raw() -> None:
    class ProviderResponse:
        language = None
        segments = [TranscriptSegment(0.0, 1.0, "text")]

        @property
        def full_text(self) -> str:
            raise AssertionError("full_text is a second source of truth")

        @property
        def raw(self) -> dict[str, str]:
            raise AssertionError("provider raw must not enter Core")

    result = normalize_legacy_transcript(ProviderResponse())

    assert result.language == "und"
    assert result.segments[0].text == "text"


def test_platform_subtitle_is_normalized_before_entering_core_media_input() -> None:
    created = 0

    def forbidden_factory() -> object:
        nonlocal created
        created += 1
        raise AssertionError("platform subtitles must not create a transcriber")

    transcript = normalize_platform_subtitle(
        _legacy_result(TranscriptSegment(0.0, 1.0, "subtitle"))
    )
    adapter = LegacyTranscriberAdapter(
        "fast-whisper",
        factories={"fast-whisper": forbidden_factory},
    )
    token = _Token()

    result = adapter.transcribe(
        MediaInput(
            media_path=None,
            provided_transcript=transcript,
        ),
        token,
    )

    assert result.segments[0].text == "subtitle"
    assert created == 0
    assert token.checks >= 2


@pytest.mark.parametrize(
    "provider_id",
    ["fast-whisper", "groq", "bcut", "kuaishou", "mlx-whisper"],
)
def test_selected_legacy_provider_is_lazy_loaded_and_normalized(
    provider_id: str,
    tmp_path: Path,
) -> None:
    media = tmp_path / "audio.wav"
    media.write_bytes(b"fixture")
    transcriber = _Transcriber(
        _legacy_result(TranscriptSegment(0.0, 1.0, provider_id))
    )
    created = 0

    def factory() -> object:
        nonlocal created
        created += 1
        return transcriber

    adapter = LegacyTranscriberAdapter(
        provider_id,
        factories={provider_id: factory},
    )

    if provider_id in {"groq", "bcut", "kuaishou"}:
        with pytest.raises(DomainError, match="external_operation_context_required"):
            adapter.transcribe(MediaInput(media_path=media), _Token())
        assert created == 0
    else:
        result = adapter.transcribe(MediaInput(media_path=media), _Token())
        assert result.segments[0].text == provider_id
        assert created == 1
        assert transcriber.calls == [str(media)]


def test_unknown_provider_is_rejected_instead_of_falling_back() -> None:
    with pytest.raises(DomainError, match="transcriber_unsupported"):
        LegacyTranscriberAdapter("typo-whisper")


def test_importing_adapter_does_not_import_heavy_or_web_runtimes() -> None:
    script = """
import json
import sys
import app.adapters.transcription.legacy_transcriber
names = ["fastapi", "faster_whisper", "mlx_whisper", "openai", "sqlalchemy", "torch"]
print(json.dumps({name: name in sys.modules for name in names}, sort_keys=True))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "fastapi": False,
        "faster_whisper": False,
        "mlx_whisper": False,
        "openai": False,
        "sqlalchemy": False,
        "torch": False,
    }


def test_fast_whisper_module_import_does_not_require_legacy_event_entrypoint(
    tmp_path: Path,
) -> None:
    script = """
import importlib.util
assert importlib.util.find_spec("events") is None
import app.transcriber.whisper
"""
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=tmp_path,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("fast-whisper provider import probe timed out")

    assert completed.returncode == 0, "fast-whisper provider import probe failed"


def test_cancellation_before_blocking_call_does_not_load_provider(
    tmp_path: Path,
) -> None:
    created = 0

    def factory() -> object:
        nonlocal created
        created += 1
        return _Transcriber(_legacy_result(TranscriptSegment(0.0, 1.0, "text")))

    adapter = LegacyTranscriberAdapter(
        "fast-whisper",
        factories={"fast-whisper": factory},
    )

    with pytest.raises(DomainError, match="job_cancelled"):
        adapter.transcribe(
            MediaInput(media_path=tmp_path / "audio.wav"),
            _Token(cancel_on_check=1),
        )

    assert created == 0


def test_local_cancellation_after_blocking_call_discards_unanchored_result(
    tmp_path: Path,
) -> None:
    transcriber = _Transcriber(
        _legacy_result(TranscriptSegment(0.0, 1.0, "text"))
    )
    adapter = LegacyTranscriberAdapter(
        "fast-whisper",
        factories={"fast-whisper": lambda: transcriber},
    )

    with pytest.raises(DomainError, match="job_cancelled"):
        adapter.transcribe(
            MediaInput(media_path=tmp_path / "audio.wav"),
            _Token(cancel_on_check=3),
        )

    assert transcriber.calls == [str(tmp_path / "audio.wav")]


def test_local_cancellation_is_checked_between_normalized_segments(
    tmp_path: Path,
) -> None:
    transcriber = _Transcriber(
        _legacy_result(
            TranscriptSegment(0.0, 1.0, "first"),
            TranscriptSegment(1.0, 2.0, "second"),
        )
    )
    adapter = LegacyTranscriberAdapter(
        "fast-whisper",
        factories={"fast-whisper": lambda: transcriber},
    )

    with pytest.raises(DomainError, match="job_cancelled"):
        adapter.transcribe(
            MediaInput(media_path=tmp_path / "audio.wav"),
            _Token(cancel_on_check=5),
        )

    assert transcriber.calls == [str(tmp_path / "audio.wav")]


@pytest.mark.parametrize("provider_id", ["groq", "bcut", "kuaishou"])
def test_paid_remote_call_is_durably_guarded_and_result_is_anchored(
    provider_id: str,
    tmp_path: Path,
) -> None:
    media = tmp_path / "secret-course-name.wav"
    media.write_bytes(b"media")
    guard = _RecordingGuard()
    transcriber = _Transcriber(
        _legacy_result(TranscriptSegment(0.0, 1.0, "private transcript"))
    )

    class CheckingTranscriber:
        def transcript(self, file_path: str) -> object:
            assert guard.current is not None
            assert guard.current.outcome is ExternalOutcome.STARTED
            return transcriber.transcript(file_path)

    adapter = LegacyTranscriberAdapter(
        provider_id,
        factories={provider_id: CheckingTranscriber},
        remote_binding=_remote_binding(tmp_path, guard),
    )

    result = adapter.transcribe(MediaInput(media_path=media), _Token())

    assert result.segments[0].text == "private transcript"
    assert guard.events == ["prepare", "start", "succeed"]
    assert guard.current is not None
    assert guard.current.outcome is ExternalOutcome.SUCCEEDED
    assert str(media) not in guard.current.summary_json
    assert "private transcript" not in guard.current.summary_json
    summary = json.loads(guard.current.summary_json)
    assert set(summary) == {"operation", "result"}
    assert set(summary["result"]) == {"path", "sha256"}


def test_succeeded_remote_operation_reloads_anchored_transcript_without_reissue(
    tmp_path: Path,
) -> None:
    media = tmp_path / "audio.wav"
    media.write_bytes(b"media")
    guard = _RecordingGuard()
    binding = _remote_binding(tmp_path, guard)
    transcriber = _Transcriber(
        _legacy_result(TranscriptSegment(0.0, 1.0, "replayable"))
    )
    first = LegacyTranscriberAdapter(
        "groq",
        factories={"groq": lambda: transcriber},
        remote_binding=binding,
    )
    expected = first.transcribe(MediaInput(media_path=media), _Token())

    def forbidden_factory() -> object:
        raise AssertionError("a succeeded paid operation must not be reissued")

    replay = LegacyTranscriberAdapter(
        "groq",
        factories={"groq": forbidden_factory},
        remote_binding=binding,
    )

    assert replay.transcribe(MediaInput(media_path=media), _Token()) == expected
    assert transcriber.calls == [str(media)]
    assert guard.events == ["prepare", "start", "succeed", "prepare"]


def test_sqlite_reopen_replays_succeeded_remote_transcript_without_reissue(
    tmp_path: Path,
) -> None:
    machine_root = tmp_path / "machine-root"
    repository = SqliteJobRepository.open(machine_root)
    job = repository.create_job(
        request_hash=sha256_digest(b"job request"),
        principal="local-user",
        client_request_id=None,
    )
    repository.transition_job(job.job_id, JobState.RUNNING)
    authority = repository.claim_job(
        job.job_id,
        "workspace:process-a",
        ttl_seconds=30,
    ).authority
    pending = repository.create_attempt(
        job.job_id,
        "normalize_transcript",
        authority=authority,
    )
    attempt = repository.start_attempt(pending.attempt_id, authority)
    request_hash = sha256_digest(b"remote transcript request")
    result_root = tmp_path / "attempt" / "operation-results"
    first_binding = RemoteTranscriptionBinding(
        guard=ExternalOperationGuard(repository, authority),
        result_store=RemoteTranscriptResultStore(result_root),
        job_id=job.job_id,
        step_id=attempt.step_id,
        attempt_id=attempt.attempt_id,
        request_hash=request_hash,
    )
    media = tmp_path / "audio.wav"
    media.write_bytes(b"media")
    transcriber = _Transcriber(
        _legacy_result(TranscriptSegment(0.0, 1.0, "durable replay"))
    )
    expected = LegacyTranscriberAdapter(
        "groq",
        factories={"groq": lambda: transcriber},
        remote_binding=first_binding,
    ).transcribe(MediaInput(media_path=media), _Token())
    assert repository.release_job_claim(authority)

    reopened = SqliteJobRepository.open(machine_root)
    new_authority = reopened.claim_job(
        job.job_id,
        "workspace:process-b",
        ttl_seconds=30,
    ).authority
    replay_binding = RemoteTranscriptionBinding(
        guard=ExternalOperationGuard(reopened, new_authority),
        result_store=RemoteTranscriptResultStore(result_root),
        job_id=job.job_id,
        step_id=attempt.step_id,
        attempt_id=attempt.attempt_id,
        request_hash=request_hash,
    )

    def forbidden_factory() -> object:
        raise AssertionError("a succeeded paid operation must not be reissued")

    replayed = LegacyTranscriberAdapter(
        "groq",
        factories={"groq": forbidden_factory},
        remote_binding=replay_binding,
    ).transcribe(MediaInput(media_path=media), _Token())

    assert replayed == expected
    assert transcriber.calls == [str(media)]


def test_missing_anchored_result_does_not_reissue_succeeded_remote_operation(
    tmp_path: Path,
) -> None:
    media = tmp_path / "audio.wav"
    media.write_bytes(b"media")
    guard = _RecordingGuard()
    binding = _remote_binding(tmp_path, guard)
    transcriber = _Transcriber(
        _legacy_result(TranscriptSegment(0.0, 1.0, "once"))
    )
    adapter = LegacyTranscriberAdapter(
        "groq",
        factories={"groq": lambda: transcriber},
        remote_binding=binding,
    )
    adapter.transcribe(MediaInput(media_path=media), _Token())
    assert guard.current is not None
    result_path = json.loads(guard.current.summary_json)["result"]["path"]
    (tmp_path / "operation-results" / result_path).unlink()

    with pytest.raises(DomainError, match="external_result_unavailable"):
        adapter.transcribe(MediaInput(media_path=media), _Token())

    assert transcriber.calls == [str(media)]


def test_semantically_corrupt_anchored_result_maps_to_stable_unavailable(
    tmp_path: Path,
) -> None:
    media = tmp_path / "audio.wav"
    media.write_bytes(b"media")
    guard = _RecordingGuard()
    binding = _remote_binding(tmp_path, guard)
    transcriber = _Transcriber(
        _legacy_result(TranscriptSegment(0.0, 1.0, "once"))
    )
    adapter = LegacyTranscriberAdapter(
        "groq",
        factories={"groq": lambda: transcriber},
        remote_binding=binding,
    )
    adapter.transcribe(MediaInput(media_path=media), _Token())
    assert guard.current is not None
    summary = json.loads(guard.current.summary_json)
    result_path = tmp_path / "operation-results" / summary["result"]["path"]
    corrupt = json.loads(result_path.read_bytes())
    corrupt["segments"][0]["end_ms"] = corrupt["segments"][0]["start_ms"]
    payload = (
        json.dumps(
            corrupt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    result_path.write_bytes(payload)
    summary["result"]["sha256"] = sha256_digest(payload)
    guard.current = replace(
        guard.current,
        summary_json=json.dumps(summary, sort_keys=True, separators=(",", ":")),
    )

    with pytest.raises(DomainError, match="external_result_unavailable"):
        adapter.transcribe(MediaInput(media_path=media), _Token())

    assert transcriber.calls == [str(media)]


def _create_directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        completed = subprocess.run(
            [
                os.environ.get("COMSPEC", "cmd.exe"),
                "/d",
                "/c",
                "mklink",
                "/J",
                str(link),
                str(target),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            pytest.skip(
                f"directory junction creation unavailable: {completed.stderr}"
            )
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink creation unavailable: {error}")


def _remove_directory_link(link: Path) -> None:
    if os.name == "nt":
        os.rmdir(link)
    else:
        link.unlink()


@pytest.mark.parametrize("link_position", ["root", "parent"])
def test_remote_result_store_rejects_reparse_root_chain(
    link_position: str,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "result-link"
    _create_directory_link(link, outside)
    root = link if link_position == "root" else link / "operation-results"
    try:
        with pytest.raises(DomainError) as caught:
            RemoteTranscriptResultStore(root)

        assert caught.value.code == "external_result_unavailable"
        assert caught.value.category is ErrorCategory.CONFLICT
        assert not tuple(outside.iterdir())
    finally:
        _remove_directory_link(link)


@pytest.mark.parametrize(
    "error",
    [ExternalOutcomeUnknownError(), TimeoutError(), ConnectionError()],
)
def test_unknown_remote_outcome_is_persisted_and_never_automatically_reissued(
    error: Exception,
    tmp_path: Path,
) -> None:
    media = tmp_path / "audio.wav"
    media.write_bytes(b"media")
    guard = _RecordingGuard()
    transcriber = _RaisingTranscriber(error)
    adapter = LegacyTranscriberAdapter(
        "groq",
        factories={"groq": lambda: transcriber},
        remote_binding=_remote_binding(tmp_path, guard),
    )

    with pytest.raises(DomainError, match="external_outcome_unknown"):
        adapter.transcribe(MediaInput(media_path=media), _Token())
    with pytest.raises(DomainError, match="external_outcome_unknown"):
        adapter.transcribe(MediaInput(media_path=media), _Token())

    assert transcriber.calls == 1
    assert guard.current is not None
    assert guard.current.outcome is ExternalOutcome.UNKNOWN
    assert guard.events == ["prepare", "start", "unknown", "prepare"]


def test_explicit_known_remote_failure_is_recorded_failed(
    tmp_path: Path,
) -> None:
    media = tmp_path / "audio.wav"
    media.write_bytes(b"media")
    guard = _RecordingGuard()
    transcriber = _RaisingTranscriber(LegacyRemoteCallFailed())
    adapter = LegacyTranscriberAdapter(
        "bcut",
        factories={"bcut": lambda: transcriber},
        remote_binding=_remote_binding(tmp_path, guard),
    )

    with pytest.raises(DomainError, match="transcription_failed"):
        adapter.transcribe(MediaInput(media_path=media), _Token())

    assert guard.current is not None
    assert guard.current.outcome is ExternalOutcome.FAILED
    assert guard.events == ["prepare", "start", "fail"]


def test_invalid_paid_response_is_succeeded_but_cannot_be_reissued(
    tmp_path: Path,
) -> None:
    media = tmp_path / "audio.wav"
    media.write_bytes(b"media")
    guard = _RecordingGuard()
    transcriber = _Transcriber(_legacy_result())
    adapter = LegacyTranscriberAdapter(
        "kuaishou",
        factories={"kuaishou": lambda: transcriber},
        remote_binding=_remote_binding(tmp_path, guard),
    )

    with pytest.raises(DomainError, match="transcript_empty"):
        adapter.transcribe(MediaInput(media_path=media), _Token())
    with pytest.raises(DomainError, match="external_result_unavailable"):
        adapter.transcribe(MediaInput(media_path=media), _Token())

    assert transcriber.calls == [str(media)]
    assert guard.current is not None
    assert guard.current.outcome is ExternalOutcome.SUCCEEDED


@pytest.mark.parametrize("cancel_on_check", [3, 5])
def test_cancel_after_remote_provider_return_is_succeeded_and_not_reissued(
    cancel_on_check: int,
    tmp_path: Path,
) -> None:
    media = tmp_path / "audio.wav"
    media.write_bytes(b"media")
    guard = _RecordingGuard()
    binding = _remote_binding(tmp_path, guard)
    transcriber = _Transcriber(
        _legacy_result(
            TranscriptSegment(0.0, 1.0, "first"),
            TranscriptSegment(1.0, 2.0, "second"),
        )
    )
    adapter = LegacyTranscriberAdapter(
        "groq",
        factories={"groq": lambda: transcriber},
        remote_binding=binding,
    )

    with pytest.raises(DomainError, match="job_cancelled"):
        adapter.transcribe(
            MediaInput(media_path=media),
            _Token(cancel_on_check=cancel_on_check),
        )

    assert guard.current is not None
    assert guard.current.outcome is ExternalOutcome.SUCCEEDED
    assert adapter.transcribe(MediaInput(media_path=media), _Token()).segments[0].text == (
        "first"
    )
    assert transcriber.calls == [str(media)]


@dataclass(frozen=True)
class _ObjectSnippet:
    text: str
    start: float
    duration: float


class _FetchedTranscript(list[_ObjectSnippet]):
    pass


class _YouTubeTranscript:
    language_code = "en"
    language = "English"
    is_generated = False

    def fetch(self) -> _FetchedTranscript:
        return _FetchedTranscript([_ObjectSnippet(" object text ", 1.25, 0.5)])


class _YouTubeTranscriptList:
    def __iter__(self):
        return iter((_YouTubeTranscript(),))

    def find_manually_created_transcript(self, _langs: list[str]) -> _YouTubeTranscript:
        return _YouTubeTranscript()


class _YouTubeApi:
    def list(self, _video_id: str) -> _YouTubeTranscriptList:
        return _YouTubeTranscriptList()


def test_youtube_transcript_api_object_snippet_preserves_text_and_timing() -> None:
    fetcher = YouTubeSubtitleFetcher.__new__(YouTubeSubtitleFetcher)
    fetcher._api = _YouTubeApi()

    result = fetcher.fetch_subtitles("video-id", ["en"])

    assert result is not None
    assert result.segments == [TranscriptSegment(1.25, 1.75, "object text")]


class _MappingYouTubeTranscript(_YouTubeTranscript):
    def fetch(self) -> list[MappingProxyType[str, object]]:
        return [
            MappingProxyType(
                {"text": " mapping text ", "start": 2.0, "duration": 0.25}
            )
        ]


class _MappingYouTubeTranscriptList(_YouTubeTranscriptList):
    def __iter__(self):
        return iter((_MappingYouTubeTranscript(),))

    def find_manually_created_transcript(
        self,
        _langs: list[str],
    ) -> _MappingYouTubeTranscript:
        return _MappingYouTubeTranscript()


class _MappingYouTubeApi:
    def list(self, _video_id: str) -> _MappingYouTubeTranscriptList:
        return _MappingYouTubeTranscriptList()


def test_youtube_legacy_mapping_snippet_remains_compatible() -> None:
    fetcher = YouTubeSubtitleFetcher.__new__(YouTubeSubtitleFetcher)
    fetcher._api = _MappingYouTubeApi()

    result = fetcher.fetch_subtitles("video-id", ["en"])

    assert result is not None
    assert result.segments == [TranscriptSegment(2.0, 2.25, "mapping text")]


@pytest.mark.parametrize(
    "snippet",
    [
        type("MissingTimingSnippet", (), {"text": "bad"})(),
        _ObjectSnippet("bad", 1.0, 0.0),
        _ObjectSnippet("bad", 1.0, float("nan")),
    ],
)
def test_youtube_malformed_object_snippet_fails_closed(snippet: object) -> None:
    class MalformedTranscript(_YouTubeTranscript):
        def fetch(self) -> list[object]:
            return [snippet]

    class MalformedTranscriptList(_YouTubeTranscriptList):
        def __iter__(self):
            return iter((MalformedTranscript(),))

        def find_manually_created_transcript(
            self,
            _langs: list[str],
        ) -> MalformedTranscript:
            return MalformedTranscript()

    class MalformedApi:
        def list(self, _video_id: str) -> MalformedTranscriptList:
            return MalformedTranscriptList()

    fetcher = YouTubeSubtitleFetcher.__new__(YouTubeSubtitleFetcher)
    fetcher._api = MalformedApi()

    assert fetcher.fetch_subtitles("video-id", ["en"]) is None

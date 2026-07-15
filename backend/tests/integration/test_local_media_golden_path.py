from __future__ import annotations

import json
import multiprocessing
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

import app.runtime as runtime_module
from app.adapters.models.legacy_gpt import (
    LegacyModelBinding,
    LegacyModelCapabilities,
    LegacyModelResponse,
)
from app.adapters.iwiki.portable_gateway import IWikiPortableGateway
from app.adapters.jobs.file_attempt_storage import FileAttemptStorage
from app.adapters.sources.legacy_video import LegacyVideoSourceAdapter
from app.core.application import video_acquisition
from app.core.application.video_checkpoints import decode_acquired
from app.core.application.video_checkpoints import decode_source
from app.core.domain.ids import sha256_digest
from app.core.domain.video import (
    JobState,
    TranscriptDocument,
    TranscriptSegment,
    VideoProduceRequest,
)
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.external_operation import ExternalOperationGuard
from app.core.jobs.resource_lease import ExecutionAuthority
from app.core.ports.transcript import MediaInput


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures"
WORKSPACE_FIXTURE = FIXTURE_ROOT / "workspace-v2"
LOCAL_METADATA = {
    "title": "Local fixture course",
    "author": "AllToNote",
    "channel": "Local files",
    "duration_ms": 2_000,
    "published_at": None,
    "observed_at": "2026-07-15T00:00:00.000Z",
    "language": "en",
}


@dataclass
class Calls:
    resolve: int = 0
    acquire: int = 0
    transcriber: int = 0
    model: int = 0
    commit: int = 0


def _record(path: Path | None, operation: str) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps({"operation": operation}, separators=(",", ":")))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


class _TranscriptFake:
    def __init__(self, calls: Calls, call_log: Path | None = None) -> None:
        self._calls = calls
        self._call_log = call_log

    def transcribe(self, media: MediaInput, token: object) -> TranscriptDocument:
        assert media.media_path is not None
        assert media.provided_transcript is None
        assert media.media_path.is_file()
        token.raise_if_cancelled()
        self._calls.transcriber += 1
        _record(self._call_log, "transcriber")
        return TranscriptDocument(
            language="en",
            segments=(
                TranscriptSegment(
                    "seg_000001",
                    0,
                    2_000,
                    "AllToNote turns local video into cited knowledge.",
                ),
            ),
        )


class _PathLeakingTranscript:
    def transcribe(self, media: MediaInput, token: object) -> TranscriptDocument:
        del token
        assert media.media_path is not None
        leaked = str(media.media_path.resolve())
        raise DomainError(
            "provider_path_leak",
            ErrorCategory.RECIPE_FAILED,
            f"provider failed for {leaked}",
            {"nested": {"snapshot_path": leaked}},
        )


class _SemanticErrorTranscript:
    def __init__(
        self,
        code: str,
        category: ErrorCategory,
        original_path: Path,
        *,
        owner_id: str | None = None,
    ) -> None:
        self.code = code
        self.category = category
        self.original_path = original_path
        self.owner_id = owner_id
        self.calls = 0
        self.repository: object | None = None

    def bind_repository(self, repository: object) -> None:
        self.repository = repository

    def transcribe(self, media: MediaInput, token: object) -> TranscriptDocument:
        assert media.media_path is not None
        self.calls += 1
        snapshot_path = media.media_path.resolve()
        if self.code == "external_outcome_unknown":
            assert self.repository is not None
            assert self.owner_id is not None
            job_id = token._job_id
            _, attempt, _ = self.repository.get_job_details(job_id)
            assert attempt is not None
            guard = ExternalOperationGuard(
                self.repository,
                ExecutionAuthority(self.owner_id, attempt.fencing_token),
            )
            operation = guard.prepare(
                job_id=job_id,
                step_id=attempt.step_id,
                attempt_id=attempt.attempt_id,
                provider="fixture/transcriber-v1",
                request_hash=sha256_digest(b"local-transcription-request"),
                summary_json="{}",
                max_attempts=1,
            )
            guard.start(operation.operation_id)
            guard.unknown(operation.operation_id, summary_json="{}")
        raise DomainError(
            self.code,
            self.category,
            f"adapter failed for {self.original_path}",
            {
                "nested": {
                    "source_path": str(self.original_path),
                    "snapshot_paths": [str(snapshot_path)],
                }
            },
        )


class _RecordingSource:
    def __init__(
        self,
        delegate: LegacyVideoSourceAdapter,
        calls: Calls,
        call_log: Path | None,
    ) -> None:
        self._delegate = delegate
        self._calls = calls
        self._call_log = call_log

    def resolve(self, input_value: str) -> object:
        self._calls.resolve += 1
        _record(self._call_log, "source_resolve")
        return self._delegate.resolve(input_value)

    def acquire(self, *args: object, **kwargs: object) -> object:
        self._calls.acquire += 1
        _record(self._call_log, "source_acquire")
        return self._delegate.acquire(*args, **kwargs)


class _RecordingPortableGateway:
    def __init__(
        self,
        delegate: IWikiPortableGateway,
        calls: Calls,
        call_log: Path | None,
    ) -> None:
        self._delegate = delegate
        self._calls = calls
        self._call_log = call_log

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    def commit_prepared(self, prepared: object) -> object:
        result = self._delegate.commit_prepared(prepared)
        self._calls.commit += 1
        _record(self._call_log, "portable_commit")
        return result


class _Completion:
    def __init__(self, calls: Calls, call_log: Path | None = None) -> None:
        self._calls = calls
        self._call_log = call_log

    def complete_once(self, prompt: str) -> LegacyModelResponse:
        assert "seg_000001" in prompt
        self._calls.model += 1
        _record(self._call_log, "model")
        return LegacyModelResponse(
            markdown=(
                "# Local video note\n\n"
                "The local source becomes cited knowledge.[^seg_000001]\n"
            ),
            provider_request_id="local-fixture-request",
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


class _CorruptSnapshotBeforeTranscript:
    def __init__(self, delegate: object) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    def acquire(self, *args: object, **kwargs: object) -> object:
        acquired = self._delegate.acquire(*args, **kwargs)
        stored = acquired.stored_media
        assert stored is not None
        target = self._delegate._storage.root / Path(stored.relative_locator)
        target.write_bytes(b"corrupted-after-publication")
        return acquired


def _create_runtime(
    machine_root: Path,
    *,
    calls: Calls | None = None,
    call_log: Path | None = None,
    owner_id: str | None = None,
    now_ms: int | None = None,
    transcriber: object | None = None,
) -> object:
    factory = getattr(runtime_module, "create_local_video_runtime", None)
    assert callable(factory), "Task 16A.1 local runtime composition is missing"
    observed = calls or Calls()
    source = _RecordingSource(
        LegacyVideoSourceAdapter(local_machine_id="fixture-machine"),
        observed,
        call_log,
    )
    model = LegacyModelBinding(
        provider_kind="fixture/provider-v1",
        model_identity="fixture/model-v1",
        bridge=_Completion(observed, call_log),
        capabilities=LegacyModelCapabilities(),
    )
    runtime = factory(
        machine_root,
        source=source,
        source_metadata={"local": LOCAL_METADATA},
        transcriber=transcriber or _TranscriptFake(observed, call_log),
        model=model,
        owner_id=owner_id,
        clock=(None if now_ms is None else lambda: now_ms),
    )
    service = runtime._sdk._video_service
    service._portable = _RecordingPortableGateway(
        service._portable,
        observed,
        call_log,
    )
    return runtime


def _request(
    source: Path,
    workspace: Path,
    request_id: str,
    *,
    provided_transcript: TranscriptDocument | None = None,
) -> VideoProduceRequest:
    return VideoProduceRequest(
        request_schema_version=1,
        workspace_root=workspace,
        input_value=str(source),
        client_request_id=request_id,
        provided_transcript=provided_transcript,
    )


def _checkpoint_payload(runtime: object, job_id: str, step_id: str) -> bytes:
    metadata = runtime.job_repository.latest_checkpoint(job_id, step_id)
    assert metadata is not None
    return (
        runtime.job_repository.machine_root.parent
        / "attempts"
        / metadata.relative_path
    ).read_bytes()


def _terminate_after_transcript(
    machine_root: str,
    job_id: str,
    call_log: str,
) -> None:
    runtime = _create_runtime(
        Path(machine_root),
        call_log=Path(call_log),
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
def local_video(tmp_path: Path) -> Path:
    path = tmp_path / "private course.mp4"
    path.write_bytes(b"deterministic-local-media-fixture")
    return path


def test_local_runtime_and_stored_media_contract_are_available() -> None:
    assert callable(getattr(runtime_module, "create_local_video_runtime", None))
    assert callable(getattr(video_acquisition, "AttemptStoredAsset", None))


def test_local_video_copies_and_transcribes_once_then_commits_private_safe_bundle(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
) -> None:
    calls = Calls()
    runtime = _create_runtime(tmp_path / "machine", calls=calls)
    submitted = runtime.submit_video(_request(local_video, workspace_root, "local-success"))

    result = runtime.wait_job(submitted.job_id)

    assert result.state is JobState.SUCCEEDED
    assert result.result is not None
    assert calls.transcriber == 1
    assert calls.model == 1
    assets = tuple((tmp_path / "machine" / "attempts").rglob("source_media.*"))
    assert len(assets) == 1
    acquisition_payload = _checkpoint_payload(runtime, submitted.job_id, "acquire")
    source_payload = _checkpoint_payload(runtime, submitted.job_id, "resolve_source")
    original_absolute = str(local_video.resolve()).encode()
    snapshot_absolute = str(assets[0].resolve()).encode()
    assert original_absolute not in acquisition_payload
    assert snapshot_absolute not in acquisition_payload
    assert original_absolute not in source_payload
    assert snapshot_absolute not in source_payload
    resolved_checkpoint = decode_source(source_payload)
    assert resolved_checkpoint.local_binding is None
    assert resolved_checkpoint.logical_reference is not None
    acquired = decode_acquired(acquisition_payload)
    stored = acquired.stored_media
    assert stored is not None
    acquisition_metadata = runtime.job_repository.latest_checkpoint(
        submitted.job_id, "acquire"
    )
    assert acquisition_metadata is not None
    assert stored.relative_locator.startswith(
        f"jobs/{submitted.job_id}/attempts/{acquisition_metadata.attempt_id}/assets/"
    )
    assert stored.sha256.startswith("sha256:")
    assert stored.byte_length == local_video.stat().st_size
    assert stored.role.value == "source_media"
    acquisition_json = json.loads(acquisition_payload)
    assert set(acquisition_json["stored_media"]) == {
        "relative_locator",
        "sha256",
        "byte_length",
        "role",
    }
    nested_unknown = json.loads(acquisition_payload)
    nested_unknown["stored_media"]["unexpected"] = "rejected"
    with pytest.raises(DomainError, match="Checkpoint content is invalid"):
        decode_acquired(
            json.dumps(nested_unknown, separators=(",", ":"), sort_keys=True).encode()
        )

    bundle = workspace_root / result.result.workspace_relative_bundle_path
    portable_bytes = b"\n".join(
        path.read_bytes() for path in bundle.rglob("*") if path.is_file()
    )
    assert original_absolute not in portable_bytes
    assert snapshot_absolute not in portable_bytes
    assert b'"kind":"external_local"' in portable_bytes
    assert b"urn:alltonote:local-content:sha256:" in portable_bytes
    assert b'"kind":"reference_only"' not in (
        bundle / "sources" / "video-metadata.json"
    ).read_bytes()
    manifest = json.loads((bundle / "bundle.json").read_text("utf-8"))
    source_id = manifest["sources"][0]["source_id"]
    materialization = manifest["source_revisions"][0]["materialization"]
    assert materialization == {
        "kind": "external_local",
        "external_ref_id": f"ext_{source_id.removeprefix('src_')}",
    }


def test_pre16a_acquisition_json_shape_decodes_to_no_stored_media(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
) -> None:
    runtime = _create_runtime(tmp_path / "legacy-codec-machine")
    submitted = runtime.submit_video(_request(local_video, workspace_root, "legacy-codec"))
    runtime.wait_job(submitted.job_id)
    current = json.loads(_checkpoint_payload(runtime, submitted.job_id, "acquire"))
    current.pop("stored_media")

    decoded = decode_acquired(
        json.dumps(current, separators=(",", ":"), sort_keys=True).encode()
    )

    assert decoded.stored_media is None


def test_provided_transcript_still_snapshots_once_without_media_transcriber_call(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
) -> None:
    calls = Calls()
    runtime = _create_runtime(tmp_path / "provided-machine", calls=calls)
    provided = TranscriptDocument(
        language="en",
        segments=(TranscriptSegment("seg_000001", 0, 2_000, "provided local text"),),
    )
    submitted = runtime.submit_video(
        _request(
            local_video,
            workspace_root,
            "local-provided",
            provided_transcript=provided,
        )
    )

    result = runtime.wait_job(submitted.job_id)

    assert result.state is JobState.SUCCEEDED
    assert calls.resolve == 1
    assert calls.acquire == 1
    assert calls.transcriber == 0
    assert len(tuple((tmp_path / "provided-machine" / "attempts").rglob("source_media.*"))) == 1
    assert decode_acquired(
        _checkpoint_payload(runtime, submitted.job_id, "acquire")
    ).stored_media is not None


def test_corrupted_snapshot_is_rejected_before_transcriber_model_or_public_path_leak(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
) -> None:
    calls = Calls()
    runtime = _create_runtime(tmp_path / "corrupt-machine", calls=calls)
    service = runtime._sdk._video_service
    service._operations = _CorruptSnapshotBeforeTranscript(service._operations)
    submitted = runtime.submit_video(
        _request(local_video, workspace_root, "local-corrupt-snapshot")
    )

    result = runtime.wait_job(submitted.job_id)

    assert result.state is JobState.FAILED
    assert result.error is not None
    assert result.error.code == "attempt_stored_asset_invalid"
    assert calls.transcriber == 0
    assert calls.model == 0
    assert str(local_video.resolve()) not in result.error.message
    events = runtime.job_repository.list_events(submitted.job_id)
    assert all(str(local_video.resolve()) not in event.payload_json for event in events)


def test_path_bearing_transcriber_error_is_sanitized_from_all_public_state(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
) -> None:
    runtime = _create_runtime(
        tmp_path / "leaking-transcriber-machine",
        transcriber=_PathLeakingTranscript(),
    )
    submitted = runtime.submit_video(
        _request(local_video, workspace_root, "path-leaking-transcriber")
    )

    result = runtime.wait_job(submitted.job_id)

    assert result.state is JobState.FAILED
    assert result.error is not None
    assets = tuple(
        (tmp_path / "leaking-transcriber-machine" / "attempts").rglob("source_media.*")
    )
    assert len(assets) == 1
    private_values = (str(local_video.resolve()), str(assets[0].resolve()))
    public_error = repr(
        {
            "code": result.error.code,
            "message": result.error.message,
            "details": result.error.details,
        }
    )
    public_events = repr(
        [vars(event) for event in runtime.job_repository.list_events(submitted.job_id)]
    )
    assert result.error.code == "local_transcription_failed"
    assert all(value not in public_error for value in private_values)
    assert all(value not in public_events for value in private_values)


@pytest.mark.parametrize(
    ("code", "category"),
    [
        ("provider_cancelled", ErrorCategory.CANCELLED),
        ("job_cancelled", ErrorCategory.CANCELLED),
        ("attempt_fenced", ErrorCategory.CONFLICT),
    ],
)
def test_control_flow_transcriber_errors_preserve_semantics_without_path_leak(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
    code: str,
    category: ErrorCategory,
) -> None:
    transcriber = _SemanticErrorTranscript(code, category, local_video)
    runtime = _create_runtime(
        tmp_path / f"{code}-machine",
        transcriber=transcriber,
    )
    submitted = runtime.submit_video(
        _request(local_video, workspace_root, f"semantic-{code}")
    )

    result = runtime.wait_job(submitted.job_id)

    assert result.state is JobState.FAILED
    assert result.error is not None
    assert result.error.code == code
    assert result.error.category is category
    assert result.error.message == "Local media transcription failed"
    assert dict(result.error.details) == {}
    assets = tuple((tmp_path / f"{code}-machine" / "attempts").rglob("source_media.*"))
    assert len(assets) == 1
    private_values = (str(local_video.resolve()), str(assets[0].resolve()))
    public_state = repr(
        {
            "error": result.error,
            "events": runtime.job_repository.list_events(submitted.job_id),
        }
    )
    assert all(value not in public_state for value in private_values)


def test_retryable_transcription_error_remains_retryable_and_path_free(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
) -> None:
    transcriber = _SemanticErrorTranscript(
        "transcription_failed",
        ErrorCategory.RETRYABLE_RUNTIME,
        local_video,
    )
    runtime = _create_runtime(
        tmp_path / "retryable-transcriber-machine",
        transcriber=transcriber,
    )
    submitted = runtime.submit_video(
        _request(local_video, workspace_root, "retryable-transcriber")
    )

    result = runtime.wait_job(submitted.job_id)

    assert result.state is JobState.FAILED
    assert result.error is not None
    assert result.error.code == "transcription_failed"
    assert result.error.category is ErrorCategory.RETRYABLE_RUNTIME
    assert result.error.message == "Local media transcription failed"
    assert dict(result.error.details) == {}
    assets = tuple(
        (tmp_path / "retryable-transcriber-machine" / "attempts").rglob(
            "source_media.*"
        )
    )
    private_values = (str(local_video.resolve()), str(assets[0].resolve()))
    public_state = repr(
        {
            "error": result.error,
            "events": runtime.job_repository.list_events(submitted.job_id),
        }
    )
    assert all(value not in public_state for value in private_values)


def test_unknown_transcription_outcome_pauses_once_without_path_leak_or_resend(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
) -> None:
    owner_id = "unknown-transcriber-owner"
    transcriber = _SemanticErrorTranscript(
        "external_outcome_unknown",
        ErrorCategory.CONFLICT,
        local_video,
        owner_id=owner_id,
    )
    runtime = _create_runtime(
        tmp_path / "unknown-transcriber-machine",
        owner_id=owner_id,
        transcriber=transcriber,
    )
    transcriber.bind_repository(runtime.job_repository)
    submitted = runtime.submit_video(
        _request(local_video, workspace_root, "unknown-transcriber")
    )

    first = runtime.wait_job(submitted.job_id)
    second = runtime.wait_job(submitted.job_id)

    assert first.state is second.state is JobState.WAITING_FOR_INPUT
    assert first.challenge_id is not None
    assert second.challenge_id == first.challenge_id
    assert first.error is second.error is None
    assert transcriber.calls == 1
    _, _, challenge = runtime.job_repository.get_job_details(submitted.job_id)
    assert challenge is not None
    prompt = json.loads(challenge.prompt_json)
    assert prompt["code"] == "external_outcome_unknown"
    assert len(prompt["operation_ids"]) == 1
    assets = tuple(
        (tmp_path / "unknown-transcriber-machine" / "attempts").rglob(
            "source_media.*"
        )
    )
    private_values = (str(local_video.resolve()), str(assets[0].resolve()))
    public_state = repr(
        {
            "prompt": prompt,
            "events": runtime.job_repository.list_events(submitted.job_id),
        }
    )
    assert all(value not in public_state for value in private_values)


def test_source_inspection_os_error_is_terminal_and_private_paths_are_redacted(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine_root = tmp_path / "source-inspection-machine"
    runtime = _create_runtime(machine_root)
    submitted = runtime.submit_video(
        _request(local_video, workspace_root, "source-inspection-error")
    )
    original_is_reparse = FileAttemptStorage._is_reparse_point

    def failing_is_reparse(path: Path) -> bool:
        if path == local_video:
            raise PermissionError(f"cannot inspect {local_video}")
        return original_is_reparse(path)

    monkeypatch.setattr(
        FileAttemptStorage,
        "_is_reparse_point",
        staticmethod(failing_is_reparse),
    )

    result = runtime.wait_job(submitted.job_id)

    assert result.state is JobState.FAILED
    assert result.error is not None
    assert result.error.code == "attempt_storage_io_failed"
    private_values = (
        str(local_video.resolve()),
        str((machine_root / "attempts").resolve()),
    )
    public_state = repr(
        {
            "error": result.error,
            "events": runtime.job_repository.list_events(submitted.job_id),
        }
    )
    assert all(value not in public_state for value in private_values)


def test_asset_replace_failure_is_terminal_and_path_free(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_replace = os.replace

    def fail_asset_replace(source: object, target: object) -> None:
        if Path(target).name.startswith("source_media"):
            raise OSError(f"replace failed for {target}")
        original_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_asset_replace)
    runtime = _create_runtime(tmp_path / "replace-failure-machine")
    submitted = runtime.submit_video(
        _request(local_video, workspace_root, "replace-failure")
    )

    result = runtime.wait_job(submitted.job_id)

    assert result.state is JobState.FAILED
    assert result.error is not None
    assert result.error.code == "attempt_storage_io_failed"
    assert str(local_video.resolve()) not in repr(result.error)


def test_process_loss_after_transcript_reuses_snapshot_without_original(
    tmp_path: Path,
    workspace_root: Path,
    local_video: Path,
) -> None:
    machine_root = tmp_path / "recovery-machine"
    call_log = tmp_path / "calls.jsonl"
    initial = _create_runtime(
        machine_root,
        owner_id="submitting-process",
        now_ms=1_000,
    )
    submitted = initial.submit_video(
        _request(local_video, workspace_root, "local-recovery")
    )
    process = multiprocessing.get_context("spawn").Process(
        target=_terminate_after_transcript,
        args=(str(machine_root), submitted.job_id, str(call_log)),
    )
    process.start()
    process.join(timeout=30)
    assert process.exitcode == 23
    assert runtime_module._read_checkpoint is not None
    assert initial.job_repository.latest_checkpoint(
        submitted.job_id, "normalize_transcript"
    ) is not None
    local_video.unlink()

    recovered_calls = Calls()
    recovered = _create_runtime(
        machine_root,
        calls=recovered_calls,
        call_log=call_log,
        owner_id="recovery-process",
        now_ms=302_001,
    )
    result = recovered.wait_job(submitted.job_id)

    operations = [json.loads(line)["operation"] for line in call_log.read_text().splitlines()]
    assert result.state is JobState.SUCCEEDED
    assert operations.count("transcriber") == 1
    assert operations.count("model") == 1
    assert operations.count("source_resolve") == 1
    assert operations.count("source_acquire") == 1
    assert operations.count("portable_commit") == 1
    assert recovered_calls.transcriber == 0
    assert recovered_calls.model == 1
    assert len(tuple((machine_root / "attempts").rglob("source_media.*"))) == 1
    assert recovered_calls.commit == 1

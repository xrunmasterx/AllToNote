from __future__ import annotations

import importlib
import json
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from iwiki.portable import PortableBundleRef, ValidationLevel, validate_bundle
from iwiki.workspace import open_workspace

from app.core.domain.video import (
    GeneratedVideoDraft,
    JobState,
    QualityOverall,
    ScreenshotPolicy,
    ScreenshotRequest,
    TranscriptDocument,
    TranscriptSegment,
    VideoProduceRequest,
)
from app.core.application.video_service import (
    VideoService,
    VideoPreflightCapabilities,
    _CandidateCheckpoint,
)
from app.core.application.video_checkpoints import decode_draft, encode_draft
from app.core.domain.ids import sha256_digest
from app.core.errors import DomainError


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "workspace-v2"
PREFLIGHT_CAPABILITY_FAILURES = (
    ("video_feature_pack", "video_feature_pack_unavailable"),
    ("source_capability", "source_capability_unavailable"),
    ("transcript_capability", "transcript_capability_unavailable"),
    ("model_capability", "model_capability_unavailable"),
    ("ffmpeg_loadable", "ffmpeg_unavailable"),
    ("model_loadable", "model_unavailable"),
    ("transcriber_loadable", "transcriber_unavailable"),
    ("effective_config_valid", "effective_config_invalid"),
    ("credential_references_resolvable", "credential_reference_unavailable"),
)
CHECKPOINT_STEPS = (
    "preflight",
    "resolve_source",
    "acquire",
    "normalize_transcript",
    "create_source_revision",
    "generate_draft",
    "optional_screenshots",
    "assemble_candidate_bundle",
    "quality_and_portable_validation",
)
_HEARTBEAT_THREAD_PREFIX = "alltonote-scheduler-heartbeat-"


def _background_heartbeat_threads() -> tuple[threading.Thread, ...]:
    return tuple(
        thread
        for thread in threading.enumerate()
        if thread.name.startswith(_HEARTBEAT_THREAD_PREFIX)
    )


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    shutil.copytree(FIXTURE_ROOT, root)
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
def runtime_factory(
    tmp_path: Path,
) -> Callable[..., tuple[object, object]]:
    created = 0

    def create(**options: object) -> tuple[object, object]:
        nonlocal created
        created += 1
        runtime_module = importlib.import_module("app.runtime")
        calls = runtime_module.FakeCallCounts()
        runtime = runtime_module.create_fake_runtime(
            tmp_path / f"machine-{created}",
            calls=calls,
            **options,
        )
        return runtime, calls

    return create


def valid_request(
    workspace_root: Path,
    *,
    client_request_id: str = "fake-video-1",
) -> VideoProduceRequest:
    return VideoProduceRequest(
        request_schema_version=1,
        workspace_root=workspace_root,
        input_value="fixture://course",
        client_request_id=client_request_id,
    )


def _validate_committed_bundle(workspace_root: Path, bundle_id: str) -> None:
    workspace = open_workspace(workspace_root, writable=True)
    report = validate_bundle(
        workspace,
        PortableBundleRef.committed(bundle_id),
        ValidationLevel.SEMANTIC,
    )

    assert report.valid
    assert report.bundle_id == bundle_id
    assert report.issues == ()


def test_fake_recipe_commits_once_and_returns_bundle(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
) -> None:
    runtime, calls = runtime_factory()

    submitted = runtime.submit_video(valid_request(workspace_root))
    snapshot = runtime.wait_job(submitted.job_id)

    assert submitted.state is JobState.QUEUED
    assert snapshot.state is JobState.SUCCEEDED
    assert snapshot.result is not None
    result = snapshot.result
    assert result.bundle_id.startswith("bnd_")
    assert result.workspace_relative_bundle_path.startswith(
        "raw/personal/bundles/"
    )
    assert result.primary_draft_artifact_id.startswith("art_")
    assert result.quality_overall is QualityOverall.PASS
    assert result.publish_eligible is True
    assert calls.download == 1
    assert calls.transcribe == 1
    assert calls.model == 1
    assert calls.ffmpeg == 0
    assert calls.commit == 1
    final = workspace_root / result.workspace_relative_bundle_path
    assert (final / "commit.json").is_file()
    assert [path.name for path in final.parent.iterdir()] == [result.bundle_id]
    _validate_committed_bundle(workspace_root, result.bundle_id)
    for step_id in CHECKPOINT_STEPS:
        assert runtime.job_repository.latest_checkpoint(
            submitted.job_id,
            step_id,
        ) is not None


def test_quality_fail_still_commits_and_returns_success(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
) -> None:
    runtime, calls = runtime_factory(quality_fail=True)

    submitted = runtime.submit_video(valid_request(workspace_root))
    snapshot = runtime.wait_job(submitted.job_id)

    assert snapshot.state is JobState.SUCCEEDED
    assert snapshot.result is not None
    assert snapshot.result.quality_overall is QualityOverall.FAIL
    assert snapshot.result.publish_eligible is False
    assert calls.commit == 1
    _validate_committed_bundle(workspace_root, snapshot.result.bundle_id)


@pytest.mark.parametrize(("field_name", "error_code"), PREFLIGHT_CAPABILITY_FAILURES)
def test_preflight_failure_starts_no_external_work(
    runtime_factory: Callable[..., tuple[object, object]],
    field_name: str,
    error_code: str,
    workspace_root: Path,
) -> None:
    runtime, calls = runtime_factory(
        capabilities=replace(
            VideoPreflightCapabilities(), **{field_name: False}
        )
    )

    submitted = runtime.submit_video(valid_request(workspace_root))
    snapshot = runtime.wait_job(submitted.job_id)

    assert snapshot.state is JobState.FAILED
    assert snapshot.error is not None
    assert snapshot.error.code == error_code
    assert calls.download == 0
    assert calls.transcribe == 0
    assert calls.model == 0
    assert calls.ffmpeg == 0
    assert calls.commit == 0
    staging = workspace_root / "raw" / "personal" / ".staging"
    assert tuple(staging.iterdir()) == ()


def test_screenshot_capability_is_checked_only_when_requested(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
) -> None:
    runtime, calls = runtime_factory(
        capabilities=replace(
            VideoPreflightCapabilities(), screenshot_capability=False
        )
    )
    request = replace(
        valid_request(workspace_root, client_request_id="screenshots-required"),
        screenshot_policy=ScreenshotPolicy.ON_DEMAND,
    )

    snapshot = runtime.wait_job(runtime.submit_video(request).job_id)

    assert snapshot.state is JobState.FAILED
    assert snapshot.error is not None
    assert snapshot.error.code == "screenshot_capability_unavailable"
    assert calls.download == calls.transcribe == calls.model == calls.ffmpeg == 0


def test_empty_on_demand_request_does_not_execute_ffmpeg(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
) -> None:
    runtime, calls = runtime_factory()
    request = replace(
        valid_request(workspace_root, client_request_id="screenshots-empty"),
        screenshot_policy=ScreenshotPolicy.ON_DEMAND,
    )

    snapshot = runtime.wait_job(runtime.submit_video(request).job_id)

    assert snapshot.state is JobState.SUCCEEDED
    assert snapshot.error is None
    assert calls.ffmpeg == 0


@pytest.mark.parametrize(
    ("policy", "screenshot_request", "error_code"),
    (
        (
            ScreenshotPolicy.OFF,
            ScreenshotRequest("seg_000001", 0),
            "screenshot_request_not_allowed",
        ),
        (
            ScreenshotPolicy.ON_DEMAND,
            ScreenshotRequest("seg_000001", 2_000),
            "screenshot_request_invalid",
        ),
    ),
)
def test_invalid_screenshot_work_never_reaches_screenshot_operation(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
    policy: ScreenshotPolicy,
    screenshot_request: ScreenshotRequest,
    error_code: str,
) -> None:
    runtime, calls = runtime_factory()
    service = runtime._sdk._video_service
    delegate = service._operations

    class InvalidDraftOperations:
        screenshot_calls = 0

        def __getattr__(self, name: str) -> object:
            return getattr(delegate, name)

        def generate_draft(self, *args: object, **kwargs: object) -> GeneratedVideoDraft:
            draft = delegate.generate_draft(*args, **kwargs)
            return replace(draft, screenshot_requests=(screenshot_request,))

        def screenshots(self, *args: object, **kwargs: object) -> tuple[object, ...]:
            del args, kwargs
            self.screenshot_calls += 1
            return ()

    operations = InvalidDraftOperations()
    service._operations = operations
    request = replace(
        valid_request(workspace_root, client_request_id=f"invalid-{policy.value}"),
        screenshot_policy=policy,
    )

    snapshot = runtime.wait_job(runtime.submit_video(request).job_id)

    assert snapshot.state is JobState.FAILED
    assert snapshot.error is not None
    assert snapshot.error.code == error_code
    assert operations.screenshot_calls == 0
    assert calls.ffmpeg == 0


@pytest.mark.parametrize(
    ("policy", "expected_state"),
    (
        (ScreenshotPolicy.ON_DEMAND, JobState.FAILED),
        (ScreenshotPolicy.OFF, JobState.SUCCEEDED),
    ),
)
def test_screenshot_model_compatibility_is_typed_and_policy_scoped(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
    policy: ScreenshotPolicy,
    expected_state: JobState,
) -> None:
    runtime, calls = runtime_factory(
        capabilities=replace(
            VideoPreflightCapabilities(), screenshot_model_compatible=False
        )
    )
    request = replace(
        valid_request(workspace_root, client_request_id=f"compat-{policy.value}"),
        screenshot_policy=policy,
    )

    snapshot = runtime.wait_job(runtime.submit_video(request).job_id)

    assert snapshot.state is expected_state
    if policy is ScreenshotPolicy.ON_DEMAND:
        assert snapshot.error is not None
        assert snapshot.error.code == "screenshot_model_incompatible"
        assert calls.download == calls.transcribe == calls.model == calls.ffmpeg == 0
    else:
        assert snapshot.error is None
        assert calls.download == calls.transcribe == calls.model == 1
        assert calls.ffmpeg == 0


def test_crash_after_rename_reconciles_without_new_model_work(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
) -> None:
    runtime, calls = runtime_factory(crash_after_commit_once=True)
    submitted = runtime.submit_video(valid_request(workspace_root))

    with pytest.raises(RuntimeError, match="injected crash after portable rename"):
        runtime.wait_job(submitted.job_id)

    assert runtime.job_repository.get_job(submitted.job_id).state is JobState.RUNNING
    assert calls.model == 1
    assert calls.commit == 1
    committed = tuple(
        (workspace_root / "raw" / "personal" / "bundles").iterdir()
    )
    assert len(committed) == 1
    assert (committed[0] / "commit.json").is_file()

    recovered = runtime.wait_job(submitted.job_id)

    assert recovered.state is JobState.SUCCEEDED
    assert recovered.result is not None
    assert recovered.result.bundle_id == committed[0].name
    assert recovered.result.idempotent is True
    assert calls.model == 1
    assert calls.commit == 1
    _validate_committed_bundle(workspace_root, recovered.result.bundle_id)


def _read_call_log(path: Path) -> tuple[str, ...]:
    return tuple(
        json.loads(line)["operation"]
        for line in path.read_text(encoding="utf-8").splitlines()
    )


def test_crash_after_rename_recovers_after_runtime_reopen_and_new_fence(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    machine_root = tmp_path / "durable-machine"
    call_log = tmp_path / "external-calls.ndjson"
    first = runtime_module.create_fake_runtime(
        machine_root,
        call_log_path=call_log,
        crash_after_commit_once=True,
        owner_id="process-a",
        clock=lambda: 1_000,
    )
    submitted = first.submit_video(
        valid_request(workspace_root, client_request_id="restart-after-rename")
    )

    with pytest.raises(RuntimeError, match="injected crash after portable rename"):
        first.wait_job(submitted.job_id)

    del first
    reopened = runtime_module.create_fake_runtime(
        machine_root,
        call_log_path=call_log,
        owner_id="process-b",
        clock=lambda: 302_000,
    )
    recovered = reopened.wait_job(submitted.job_id)

    assert recovered.state is JobState.SUCCEEDED
    assert recovered.result is not None
    assert recovered.result.idempotent is True
    operations = _read_call_log(call_log)
    assert operations.count("download") == 1
    assert operations.count("transcribe") == 1
    assert operations.count("model") == 1
    assert operations.count("portable_commit") == 1
    with reopened.job_repository._connect() as connection:
        commit_attempts = connection.execute(
            """
            SELECT state FROM attempts
            WHERE job_id = ? AND step_id = 'commit'
            ORDER BY rowid
            """,
            (submitted.job_id,),
        ).fetchall()
        assembly_attempts = connection.execute(
            """
            SELECT COUNT(*) FROM attempts
            WHERE job_id = ? AND step_id = 'assemble_candidate_bundle'
            """,
            (submitted.job_id,),
        ).fetchone()[0]
    assert tuple(row["state"] for row in commit_attempts) == (
        "interrupted",
        "succeeded",
    )
    assert assembly_attempts == 1


def test_restart_after_draft_failure_reuses_transcript_checkpoint(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    machine_root = tmp_path / "transcript-replay-machine"
    call_log = tmp_path / "transcript-replay-calls.ndjson"
    first = runtime_module.create_fake_runtime(
        machine_root,
        call_log_path=call_log,
        crash_operation_once="model",
        owner_id="process-a",
        clock=lambda: 1_000,
    )
    submitted = first.submit_video(
        valid_request(workspace_root, client_request_id="transcript-replay")
    )

    with pytest.raises(RuntimeError, match="injected model crash"):
        first.wait_job(submitted.job_id)

    del first
    reopened = runtime_module.create_fake_runtime(
        machine_root,
        call_log_path=call_log,
        owner_id="process-b",
        clock=lambda: 302_000,
    )
    recovered = reopened.wait_job(submitted.job_id)

    assert recovered.state is JobState.SUCCEEDED
    operations = _read_call_log(call_log)
    assert operations.count("download") == 1
    assert operations.count("transcribe") == 1
    assert operations.count("model") == 2


def test_restart_after_screenshot_failure_reuses_draft_checkpoint(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    machine_root = tmp_path / "draft-replay-machine"
    call_log = tmp_path / "draft-replay-calls.ndjson"
    first = runtime_module.create_fake_runtime(
        machine_root,
        call_log_path=call_log,
        crash_operation_once="screenshots",
        owner_id="process-a",
        clock=lambda: 1_000,
        screenshot_requests=(ScreenshotRequest("seg_000001", 0),),
    )
    request = replace(
        valid_request(workspace_root, client_request_id="draft-replay"),
        screenshot_policy=ScreenshotPolicy.ON_DEMAND,
    )
    submitted = first.submit_video(request)

    with pytest.raises(RuntimeError, match="injected screenshots crash"):
        first.wait_job(submitted.job_id)

    del first
    reopened = runtime_module.create_fake_runtime(
        machine_root,
        call_log_path=call_log,
        owner_id="process-b",
        clock=lambda: 302_000,
        screenshot_requests=(ScreenshotRequest("seg_000001", 0),),
    )
    recovered = reopened.wait_job(submitted.job_id)

    assert recovered.state is JobState.SUCCEEDED
    operations = _read_call_log(call_log)
    assert operations.count("download") == 1
    assert operations.count("transcribe") == 1
    assert operations.count("model") == 1


def test_recovered_legacy_draft_spaces_citations_without_repeating_model_work(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    machine_root = tmp_path / "legacy-citation-spacing-machine"
    call_log = tmp_path / "legacy-citation-spacing-calls.ndjson"
    transcript = TranscriptDocument(
        "zh-CN",
        (
            TranscriptSegment("seg_000001", 0, 1_000, "First claim"),
            TranscriptSegment("seg_000002", 1_000, 2_000, "Second claim"),
        ),
    )
    request = replace(
        valid_request(workspace_root, client_request_id="legacy-citation-spacing"),
        provided_transcript=transcript,
        screenshot_policy=ScreenshotPolicy.ON_DEMAND,
    )
    first = runtime_module.create_fake_runtime(
        machine_root,
        call_log_path=call_log,
        crash_operation_once="screenshots",
        owner_id="legacy-spacing-first",
        clock=lambda: 1_000,
        screenshot_requests=(ScreenshotRequest("seg_000001", 0),),
    )
    service = first._sdk._video_service
    delegate = service._operations

    class AdjacentCitationOperations:
        def __getattr__(self, name: str) -> object:
            return getattr(delegate, name)

        def generate_draft(
            self,
            request_value: VideoProduceRequest,
            transcript_value: TranscriptDocument,
            *,
            execution: object,
        ) -> GeneratedVideoDraft:
            generated = delegate.generate_draft(
                request_value,
                transcript_value,
                execution=execution,
            )
            return replace(
                generated,
                markdown=(
                    "# Video note\n\n## Evidence\n\n"
                    "First and second claims.[^seg_000001][^seg_000002]\n"
                ),
                cited_segment_ids=("seg_000001", "seg_000002"),
            )

    service._operations = AdjacentCitationOperations()
    submitted = first.submit_video(request)

    with pytest.raises(RuntimeError, match="injected screenshots crash"):
        first.wait_job(submitted.job_id)

    draft_metadata = first.job_repository.latest_checkpoint(
        submitted.job_id, "generate_draft"
    )
    assert draft_metadata is not None
    request_hash = VideoService._request_hash(request)
    assert draft_metadata.input_hash == request_hash
    draft_path = (
        Path(first.job_repository.database_path).parent.parent
        / "attempts"
        / draft_metadata.relative_path
    )
    durable_draft = decode_draft(draft_path.read_bytes())
    evidence_ids = tuple(
        VideoService._derived_id(submitted.job_id, "ev", segment_id)
        for segment_id in durable_draft.cited_segment_ids
    )
    adjacent = f"[^{evidence_ids[0]}][^{evidence_ids[1]}]"
    spaced = f"[^{evidence_ids[0]}] [^{evidence_ids[1]}]"
    legacy_draft = replace(
        durable_draft,
        markdown=durable_draft.markdown.replace(spaced, adjacent),
    )
    legacy_payload = encode_draft(legacy_draft)
    assert adjacent in legacy_draft.markdown
    draft_path.write_bytes(legacy_payload)
    with first.job_repository._transaction(immediate=True) as connection:
        connection.execute(
            """
            UPDATE checkpoints SET output_hash = ?, byte_length = ?
            WHERE checkpoint_id = ?
            """,
            (
                sha256_digest(legacy_payload),
                len(legacy_payload),
                draft_metadata.checkpoint_id,
            ),
        )

    old_candidate_hash = sha256_digest(
        json.dumps(
            {"behavior": "linked-screenshot-draft-v1", "request": request_hash},
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    assert VideoService._candidate_assembly_input_hash(request_hash) != old_candidate_hash

    del first
    reopened = runtime_module.create_fake_runtime(
        machine_root,
        call_log_path=call_log,
        owner_id="legacy-spacing-second",
        clock=lambda: 302_000,
    )
    recovered = reopened.wait_job(submitted.job_id)

    assert recovered.state is JobState.SUCCEEDED
    assert recovered.result is not None
    operations = _read_call_log(call_log)
    assert operations.count("model") == 1
    recovered_draft_metadata = reopened.job_repository.latest_checkpoint(
        submitted.job_id, "generate_draft"
    )
    assert recovered_draft_metadata is not None
    assert recovered_draft_metadata.checkpoint_id == draft_metadata.checkpoint_id
    assert recovered_draft_metadata.input_hash == request_hash
    committed = workspace_root / recovered.result.workspace_relative_bundle_path
    final_markdown = (
        committed
        / "drafts"
        / f"{recovered.result.primary_draft_artifact_id}.md"
    ).read_text(encoding="utf-8")
    assert spaced in final_markdown
    assert adjacent not in final_markdown
    assert final_markdown.count(f"[^{evidence_ids[0]}]") == 2
    assert final_markdown.count(f"[^{evidence_ids[1]}]") == 2
    _validate_committed_bundle(workspace_root, recovered.result.bundle_id)


def test_unsupported_recipe_version_fails_preflight_without_external_work(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
) -> None:
    runtime, calls = runtime_factory()
    request = VideoProduceRequest(
        request_schema_version=1,
        workspace_root=workspace_root,
        input_value="fixture://course",
        client_request_id="unsupported-recipe",
        recipe_version=999,
    )

    snapshot = runtime.wait_job(runtime.submit_video(request).job_id)

    assert snapshot.state is JobState.FAILED
    assert snapshot.error is not None
    assert snapshot.error.code == "recipe_version_unsupported"
    assert calls.download == calls.transcribe == calls.model == calls.ffmpeg == 0


def test_second_canonical_source_conflicts_before_new_bundle_commit(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
) -> None:
    runtime, calls = runtime_factory()

    first = runtime.wait_job(
        runtime.submit_video(
            valid_request(workspace_root, client_request_id="same-source-first")
        ).job_id
    )
    second = runtime.wait_job(
        runtime.submit_video(
            valid_request(workspace_root, client_request_id="same-source-second")
        ).job_id
    )

    assert first.state is JobState.SUCCEEDED
    assert second.state is JobState.FAILED
    assert first.result is not None
    assert second.result is None
    assert second.error is not None
    assert second.error.code == "source_identity_conflict"
    committed = tuple((workspace_root / "raw" / "personal" / "bundles").iterdir())
    assert [item.name for item in committed] == [first.result.bundle_id]
    assert calls.commit == 1


def test_long_external_step_renews_scheduler_lease_cooperatively(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    now_ms = [1_000]

    def long_model_step(heartbeat: Callable[[], None]) -> None:
        now_ms[0] += 200_000
        heartbeat()
        now_ms[0] += 200_000

    calls = runtime_module.FakeCallCounts()
    runtime = runtime_module.create_fake_runtime(
        tmp_path / "heartbeat-machine",
        calls=calls,
        clock=lambda: now_ms[0],
        operation_hooks={"model": long_model_step},
    )

    snapshot = runtime.wait_job(
        runtime.submit_video(
            valid_request(workspace_root, client_request_id="long-model")
        ).job_id
    )

    assert snapshot.state is JobState.SUCCEEDED
    assert calls.download == calls.transcribe == calls.model == 1
    assert calls.commit == 1


def test_blocking_checkpoint_action_renews_scheduler_lease_in_background(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    now_ms = [1_000]
    background_heartbeat = threading.Event()

    def block_model_without_cooperative_heartbeat(
        _heartbeat: Callable[[], None],
    ) -> None:
        background_heartbeat.clear()
        now_ms[0] = 200_000
        assert background_heartbeat.wait(timeout=2)
        now_ms[0] = 400_000

    calls = runtime_module.FakeCallCounts()
    runtime = runtime_module.create_fake_runtime(
        tmp_path / "background-heartbeat-machine",
        calls=calls,
        clock=lambda: now_ms[0],
        operation_hooks={"model": block_model_without_cooperative_heartbeat},
    )
    service = runtime._sdk._video_service
    service._heartbeat_interval_seconds = 0.01
    repository = runtime.job_repository
    original_heartbeat = repository.heartbeat_scheduler_lease

    def observe_heartbeat(authority: object, *, ttl_seconds: int) -> object:
        renewed = original_heartbeat(authority, ttl_seconds=ttl_seconds)
        if threading.current_thread() is not threading.main_thread():
            background_heartbeat.set()
        return renewed

    repository.heartbeat_scheduler_lease = observe_heartbeat

    snapshot = runtime.wait_job(
        runtime.submit_video(
            valid_request(workspace_root, client_request_id="background-heartbeat")
        ).job_id
    )

    assert snapshot.state is JobState.SUCCEEDED
    assert calls.download == calls.transcribe == calls.model == 1
    assert calls.commit == 1
    assert _background_heartbeat_threads() == ()


@pytest.mark.parametrize("failure_type", (RuntimeError, KeyboardInterrupt))
def test_checkpoint_heartbeat_worker_stops_when_action_raises(
    tmp_path: Path,
    workspace_root: Path,
    failure_type: type[BaseException],
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    background_heartbeat = threading.Event()

    def fail_model_after_heartbeat(_heartbeat: Callable[[], None]) -> None:
        background_heartbeat.clear()
        assert background_heartbeat.wait(timeout=2)
        raise failure_type("action failed")

    runtime = runtime_module.create_fake_runtime(
        tmp_path / f"heartbeat-{failure_type.__name__}",
        operation_hooks={"model": fail_model_after_heartbeat},
    )
    service = runtime._sdk._video_service
    service._heartbeat_interval_seconds = 0.01
    repository = runtime.job_repository
    original_heartbeat = repository.heartbeat_scheduler_lease

    def observe_heartbeat(authority: object, *, ttl_seconds: int) -> object:
        renewed = original_heartbeat(authority, ttl_seconds=ttl_seconds)
        if threading.current_thread() is not threading.main_thread():
            background_heartbeat.set()
        return renewed

    repository.heartbeat_scheduler_lease = observe_heartbeat
    submitted = runtime.submit_video(
        valid_request(
            workspace_root,
            client_request_id=f"heartbeat-{failure_type.__name__}",
        )
    )

    with pytest.raises(failure_type, match="action failed"):
        runtime.wait_job(submitted.job_id)

    assert _background_heartbeat_threads() == ()


@pytest.mark.parametrize("action_failure", (None, KeyboardInterrupt))
def test_fenced_background_heartbeat_prevents_checkpoint_and_commit(
    tmp_path: Path,
    workspace_root: Path,
    action_failure: type[BaseException] | None,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    now_ms = [1_000]
    heartbeat_failed = threading.Event()

    def fence_during_model(_heartbeat: Callable[[], None]) -> None:
        now_ms[0] = 302_000
        repository.acquire_scheduler_lease("replacement-owner", ttl_seconds=300)
        assert heartbeat_failed.wait(timeout=2)
        if action_failure is not None:
            raise action_failure("control flow interrupted")

    calls = runtime_module.FakeCallCounts()
    runtime = runtime_module.create_fake_runtime(
        tmp_path / "fenced-heartbeat-machine",
        calls=calls,
        clock=lambda: now_ms[0],
        operation_hooks={"model": fence_during_model},
    )
    service = runtime._sdk._video_service
    service._heartbeat_interval_seconds = 0.01
    repository = runtime.job_repository
    original_heartbeat = repository.heartbeat_scheduler_lease

    def observe_heartbeat(authority: object, *, ttl_seconds: int) -> object:
        try:
            return original_heartbeat(authority, ttl_seconds=ttl_seconds)
        except BaseException:
            if threading.current_thread() is not threading.main_thread():
                heartbeat_failed.set()
            raise

    repository.heartbeat_scheduler_lease = observe_heartbeat
    submitted = runtime.submit_video(
        valid_request(workspace_root, client_request_id="fenced-heartbeat")
    )

    if action_failure is None:
        with pytest.raises(DomainError, match="scheduler_lease_lost"):
            runtime.wait_job(submitted.job_id)
    else:
        with pytest.raises(action_failure, match="control flow interrupted"):
            runtime.wait_job(submitted.job_id)

    assert repository.latest_checkpoint(submitted.job_id, "generate_draft") is None
    assert calls.commit == 0
    assert _background_heartbeat_threads() == ()


def test_takeover_of_running_generate_draft_leaves_no_running_replacement(
    tmp_path: Path,
    workspace_root: Path,
) -> None:
    runtime_module = importlib.import_module("app.runtime")
    machine_root = tmp_path / "takeover-machine"
    first = runtime_module.create_fake_runtime(
        machine_root,
        owner_id="process-a",
        clock=lambda: 1_000,
    )
    submitted = first.submit_video(
        valid_request(workspace_root, client_request_id="takeover-model")
    )
    repository = first.job_repository
    repository.transition_job(submitted.job_id, JobState.RUNNING)
    old_authority = repository.acquire_scheduler_lease("process-a", ttl_seconds=300)
    abandoned = repository.start_attempt(
        repository.create_attempt(submitted.job_id, "generate_draft").attempt_id,
        old_authority,
    )

    del first
    calls = runtime_module.FakeCallCounts()
    reopened = runtime_module.create_fake_runtime(
        machine_root,
        calls=calls,
        owner_id="process-b",
        clock=lambda: 302_000,
    )
    recovered = reopened.wait_job(submitted.job_id)

    assert recovered.state is JobState.SUCCEEDED
    with reopened.job_repository._connect() as connection:
        attempts = connection.execute(
            "SELECT attempt_id, state FROM attempts WHERE job_id = ?",
            (submitted.job_id,),
        ).fetchall()
    states = {row["attempt_id"]: row["state"] for row in attempts}
    assert states[abandoned.attempt_id] == "interrupted"
    assert "running" not in states.values()
    assert calls.model == 1


def test_concurrent_waits_on_one_runtime_execute_job_once(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
) -> None:
    model_entered = threading.Event()
    release_model = threading.Event()

    def block_model(heartbeat: Callable[[], None]) -> None:
        heartbeat()
        model_entered.set()
        assert release_model.wait(timeout=5)

    runtime, calls = runtime_factory(operation_hooks={"model": block_model})
    submitted = runtime.submit_video(
        valid_request(workspace_root, client_request_id="concurrent-wait")
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(runtime.wait_job, submitted.job_id)
        assert model_entered.wait(timeout=5)
        second = executor.submit(runtime.wait_job, submitted.job_id)
        release_model.set()
        snapshots = (first.result(timeout=10), second.result(timeout=10))

    assert all(item.state is JobState.SUCCEEDED for item in snapshots)
    assert calls.download == calls.transcribe == calls.model == 1
    assert calls.commit == 1


def test_candidate_checkpoint_decode_rejects_malformed_control_payloads(
    runtime_factory: Callable[..., tuple[object, object]],
    workspace_root: Path,
) -> None:
    runtime, _ = runtime_factory()
    snapshot = runtime.wait_job(
        runtime.submit_video(
            valid_request(workspace_root, client_request_id="strict-candidate")
        ).job_id
    )
    assert snapshot.state is JobState.SUCCEEDED
    metadata = runtime.job_repository.latest_checkpoint(
        snapshot.job_id, "assemble_candidate_bundle"
    )
    assert metadata is not None
    payload_path = (
        Path(runtime.job_repository.database_path).parent.parent
        / "attempts"
        / metadata.relative_path
    )
    original = json.loads(payload_path.read_text(encoding="utf-8"))

    invalid_payloads: list[dict[str, object]] = []
    for key, value in (
        ("extra", "not-allowed"),
        ("publish_eligible", 1),
        ("display_asset_ids", "art_not-a-list"),
        ("warnings", [1]),
        ("usage", {"input_tokens": 1, "cost_micros": 1}),
        ("bundle_id", "bnd_invalid"),
        ("manifest_sha256", "sha256:" + "A" * 64),
        ("staging_relative_path", "../outside"),
    ):
        mutated = dict(original)
        mutated[key] = value
        invalid_payloads.append(mutated)

    for invalid in invalid_payloads:
        with pytest.raises(Exception, match="candidate_checkpoint_invalid"):
            _CandidateCheckpoint.decode(json.dumps(invalid).encode("utf-8"))

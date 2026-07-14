from __future__ import annotations

import importlib
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from iwiki.portable import PortableBundleRef, ValidationLevel, validate_bundle
from iwiki.workspace import open_workspace

from app.core.domain.video import (
    JobState,
    QualityOverall,
    VideoProduceRequest,
)


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "workspace-v2"
PREFLIGHT_FAILURE_CASES = (
    "request_schema",
    "workspace_contract",
    "recipe_version",
    "runtime_version",
    "video_feature_pack",
    "job_store",
    "work_directory",
    "disk_space",
    "source_capability",
    "transcript_capability",
    "model_capability",
    "screenshot_capability",
    "screenshot_model_incompatibility",
    "ffmpeg_loadability",
    "model_loadability",
    "transcriber_loadability",
    "effective_config",
    "credential_reference",
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


@pytest.mark.parametrize("failure", PREFLIGHT_FAILURE_CASES)
def test_preflight_failure_starts_no_external_work(
    runtime_factory: Callable[..., tuple[object, object]],
    failure: str,
    workspace_root: Path,
) -> None:
    runtime, calls = runtime_factory(preflight_failure=failure)

    submitted = runtime.submit_video(valid_request(workspace_root))
    snapshot = runtime.wait_job(submitted.job_id)

    assert snapshot.state is JobState.FAILED
    assert snapshot.error is not None
    assert snapshot.error.code == f"preflight_{failure}_failed"
    assert calls.download == 0
    assert calls.transcribe == 0
    assert calls.model == 0
    assert calls.ffmpeg == 0
    assert calls.commit == 0
    staging = workspace_root / "raw" / "personal" / ".staging"
    assert tuple(staging.iterdir()) == ()


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

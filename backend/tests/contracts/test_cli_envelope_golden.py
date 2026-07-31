from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.cli.main import main
from app.core.domain.video import (
    JobSnapshot,
    JobState,
    QualityOverall,
    VideoDocumentKind,
    VideoProducedDocument,
    VideoProduceRequest,
    VideoProduceResult,
)
from app.core.errors import DomainError, ErrorCategory


FIXTURE_ROOT = Path(__file__).parent / "fixtures"
CORRELATION_ID = "corr_018cc251-f400-7000-8000-000000000000"
JOB_ID = "job_018cc251-f400-7000-8000-000000000001"
DRAFT_ID = "art_018cc251-f400-7000-8000-000000000002"
QUALITY_ID = "art_018cc251-f400-7000-8000-000000000003"


def _golden(name: str) -> object:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def _submitted_snapshot() -> JobSnapshot:
    return JobSnapshot(
        job_id=JOB_ID,
        state=JobState.QUEUED,
        cancellation_requested=False,
        active_attempt_id=None,
        challenge_id=None,
        retry_of_job_id=None,
        result=None,
        error=None,
    )


def _completed_snapshot() -> JobSnapshot:
    document = VideoProducedDocument(
        document_kind=VideoDocumentKind.KNOWLEDGE_NOTE,
        draft_artifact_id=DRAFT_ID,
        quality_report_artifact_id=QUALITY_ID,
        quality_overall=QualityOverall.PASS_WITH_WARNINGS,
        publish_eligible=True,
    )
    result = VideoProduceResult(
        job_id=JOB_ID,
        run_id="run_018cc251-f400-7000-8000-000000000004",
        bundle_id="bnd_018cc251-f400-7000-8000-000000000005",
        manifest_sha256="sha256:" + "1" * 64,
        commit_sha256="sha256:" + "2" * 64,
        workspace_relative_bundle_path=(
            "raw/personal/bundles/bnd_018cc251-f400-7000-8000-000000000005"
        ),
        source_id="src_018cc251-f400-7000-8000-000000000006",
        source_revision_id="rev_018cc251-f400-7000-8000-000000000007",
        primary_draft_artifact_id=DRAFT_ID,
        transcript_artifact_id="art_018cc251-f400-7000-8000-000000000008",
        evidence_set_artifact_id="art_018cc251-f400-7000-8000-000000000009",
        quality_report_artifact_id=QUALITY_ID,
        display_asset_ids=(),
        quality_overall=QualityOverall.PASS_WITH_WARNINGS,
        publish_eligible=True,
        usage={"model_calls": 2},
        warnings=("transcript-language-detected",),
        idempotent=False,
        documents=(document,),
    )
    return JobSnapshot(
        job_id=JOB_ID,
        state=JobState.SUCCEEDED,
        cancellation_requested=False,
        active_attempt_id=None,
        challenge_id=None,
        retry_of_job_id=None,
        result=result,
        error=None,
    )


class _GoldenRuntime:
    def __init__(self) -> None:
        self.submitted = _submitted_snapshot()
        self.completed = _completed_snapshot()
        self.wait_calls: list[str] = []

    def submit_video(self, request: VideoProduceRequest) -> JobSnapshot:
        del request
        return self.submitted

    def wait_job(
        self, job_id: str, event_sink: object | None = None
    ) -> JobSnapshot:
        del event_sink
        assert job_id == JOB_ID
        self.wait_calls.append(job_id)
        return self.completed


class _QueuedGoldenRuntime(_GoldenRuntime):
    def wait_job(
        self, job_id: str, event_sink: object | None = None
    ) -> JobSnapshot:
        del event_sink
        assert job_id == JOB_ID
        self.wait_calls.append(job_id)
        return self.submitted


class _CapabilityFailureRuntime:
    def submit_video(self, request: VideoProduceRequest) -> JobSnapshot:
        del request
        raise DomainError(
            "video_feature_pack_unavailable",
            ErrorCategory.WORKSPACE_INCOMPATIBLE,
            "Video capability is unavailable",
            {"capability": "recipe.video"},
        )


def _run_produce(
    workspace_root: Path,
    runtime: object,
    *,
    wait: bool,
) -> int:
    arguments = [
        "produce",
        "video",
        "--input",
        "fixture://course",
        "--workspace",
        str(workspace_root),
        "--json",
    ]
    if wait:
        arguments.append("--wait")
    return main(arguments, runtime=runtime)


def test_version_json_matches_published_golden(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("app.cli.main.new_typed_id", lambda prefix: CORRELATION_ID)
    exit_code = main(["version", "--json"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert json.loads(captured.out) == _golden("version-success.v1.json")
    assert captured.err == ""
    assert captured.out.count("\n") == 1


def test_queued_wait_result_projection_matches_published_golden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace_root = tmp_path / "Workspace"
    workspace_root.mkdir()
    monkeypatch.setattr("app.cli.main.new_typed_id", lambda prefix: CORRELATION_ID)

    runtime = _QueuedGoldenRuntime()
    exit_code = _run_produce(workspace_root, runtime, wait=False)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert runtime.wait_calls == [JOB_ID]
    assert json.loads(captured.out) == _golden("job-submitted.v1.json")
    assert captured.err == ""
    assert captured.out.count("\n") == 1


def test_committed_portable_result_matches_published_golden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace_root = tmp_path / "Workspace"
    workspace_root.mkdir()
    monkeypatch.setattr("app.cli.main.new_typed_id", lambda prefix: CORRELATION_ID)

    exit_code = _run_produce(workspace_root, _GoldenRuntime(), wait=True)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert json.loads(captured.out) == _golden(
        "produce-video-portable-result.v1.json"
    )
    assert captured.err == ""
    assert captured.out.count("\n") == 1


def test_capability_failure_matches_published_golden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace_root = tmp_path / "Workspace"
    workspace_root.mkdir()
    monkeypatch.setattr("app.cli.main.new_typed_id", lambda prefix: CORRELATION_ID)

    exit_code = _run_produce(
        workspace_root, _CapabilityFailureRuntime(), wait=True
    )
    captured = capsys.readouterr()

    assert exit_code == 10
    assert json.loads(captured.out) == _golden("produce-video-error.v1.json")
    assert captured.err == ""
    assert captured.out.count("\n") == 1

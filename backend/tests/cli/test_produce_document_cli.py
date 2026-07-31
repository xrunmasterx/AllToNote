from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.cli.commands.jobs import _result_refs
from app.cli.main import main
from app.core.domain.production import RecipeProduceResult
from app.core.domain.video import JobSnapshot, JobState
from app.core.recipes.contracts import ProduceRequest, ProduceSubmission
from app.core.domain.ids import new_typed_id


def _document_result(job_id: str) -> RecipeProduceResult:
    bundle_id = new_typed_id("bnd")
    return RecipeProduceResult(
        result_schema_version=1,
        result_kind="document-note",
        job_id=job_id,
        run_id=new_typed_id("run"),
        bundle_id=bundle_id,
        manifest_sha256="sha256:" + "a" * 64,
        commit_sha256="sha256:" + "b" * 64,
        workspace_relative_bundle_path=f"raw/personal/bundles/{bundle_id}",
        source_id=new_typed_id("src"),
        source_revision_id=new_typed_id("rev"),
        artifacts={
            "evidence_set": new_typed_id("art"),
            "normalized_content": new_typed_id("art"),
            "primary_draft": new_typed_id("art"),
            "quality_report": new_typed_id("art"),
            "source_metadata": new_typed_id("art"),
        },
        quality_overall="pass",
        publish_eligible=False,
        usage={"pages": 4, "blocks": 82},
        warnings=(),
        idempotent=False,
    )


class _DocumentRuntime:
    def __init__(self) -> None:
        self.job_id = new_typed_id("job")
        self.requests: list[ProduceRequest] = []
        self.wait_calls: list[str] = []
        self.completed = JobSnapshot(
            job_id=self.job_id,
            state=JobState.SUCCEEDED,
            cancellation_requested=False,
            active_attempt_id=None,
            challenge_id=None,
            retry_of_job_id=None,
            result=_document_result(self.job_id),
            error=None,
        )
        self.queued = JobSnapshot(
            job_id=self.job_id,
            state=JobState.QUEUED,
            cancellation_requested=False,
            active_attempt_id=None,
            challenge_id=None,
            retry_of_job_id=None,
            result=None,
            error=None,
        )

    def submit(self, request: ProduceRequest) -> ProduceSubmission:
        self.requests.append(request)
        return ProduceSubmission(self.job_id, request.recipe_key, JobState.QUEUED)

    def get_job(self, job_id: str) -> JobSnapshot:
        assert job_id == self.job_id
        return self.queued

    def wait_job(self, job_id: str, event_sink: object | None = None) -> JobSnapshot:
        del event_sink
        assert job_id == self.job_id
        self.wait_calls.append(job_id)
        return self.completed

    def cancel_job(self, job_id: str) -> JobSnapshot:
        assert job_id == self.job_id
        return self.queued


def test_generic_document_produce_uses_file_contract_and_projects_result(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    runtime = _DocumentRuntime()

    exit_code = main(
        [
            "produce",
            str(source),
            "--recipe",
            "alltonote.document-note@1",
            "--workspace",
            str(workspace),
            "--json",
        ],
        runtime=runtime,
    )
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert runtime.wait_calls == [runtime.job_id]
    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request.input.kind == "file"
    assert request.input.value == str(source)
    assert request.input.attributes == {}
    assert request.requested_outputs == ("knowledge-note",)
    assert request.parameters == {}
    assert envelope["ok"] is True
    assert envelope["command"] == "produce"
    assert envelope["data"]["result_kind"] == "document-note"
    assert envelope["data"]["quality"] == {
        "overall": "pass",
        "publish_eligible": False,
    }
    assert envelope["data"]["usage"] == {"blocks": 82, "pages": 4}
    assert envelope["data"]["primary_draft_artifact_id"] == (
        runtime.completed.result.artifacts["primary_draft"]
    )
    assert envelope["data"]["evidence_set_artifact_id"] == (
        runtime.completed.result.artifacts["evidence_set"]
    )
    assert envelope["data"]["source_id"] == runtime.completed.result.source_id
    assert envelope["data"]["source_revision_id"] == (
        runtime.completed.result.source_revision_id
    )
    assert {item["role"] for item in envelope["artifacts"]} == set(
        runtime.completed.result.artifacts
    )


def test_generic_document_produce_hands_off_to_clean_draft(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    runtime = _DocumentRuntime()

    assert main(
        [
            "produce",
            str(source),
            "--recipe",
            "alltonote.document-note@1",
            "--workspace",
            str(workspace),
        ],
        runtime=runtime,
    ) == 0
    captured = capsys.readouterr()

    draft_id = runtime.completed.result.artifacts["primary_draft"]
    assert f"Read: alltonote draft show {draft_id}\n" in captured.out
    assert "Document page" not in captured.out
    assert "[^ev_" not in captured.out
    assert captured.err == ""


def test_generic_document_produce_selects_document_runtime_by_recipe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    runtime = _DocumentRuntime()
    selected: list[Path] = []

    def create(workspace_root: Path) -> _DocumentRuntime:
        selected.append(workspace_root)
        return runtime

    monkeypatch.setattr(
        "app.runtime.create_document_runtime_for_workspace",
        create,
        raising=False,
    )

    assert main(
        [
            "produce",
            str(source),
            "--recipe",
            "alltonote.document-note@1",
            "--workspace",
            str(workspace),
            "--wait",
            "--json",
        ]
    ) == 0
    capsys.readouterr()

    assert selected == [workspace.resolve()]


def test_generic_document_request_file_preserves_document_contract(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "contract_version": 1,
                "recipe_key": {
                    "recipe_id": "alltonote.document-note",
                    "recipe_version": 1,
                },
                "input": {"kind": "file", "value": str(source)},
                "workspace_ref": str(workspace),
                "requested_outputs": ["knowledge-note"],
                "parameters": {},
            }
        ),
        encoding="utf-8",
    )
    runtime = _DocumentRuntime()

    assert main(
        ["produce", "--request", str(request_path), "--json"],
        runtime=runtime,
    ) == 0
    capsys.readouterr()

    assert len(runtime.requests) == 1
    assert runtime.requests[0].input.kind == "file"
    assert runtime.requests[0].parameters == {}


def test_generic_job_result_refs_support_document_result() -> None:
    result = _document_result(new_typed_id("job"))

    refs = _result_refs(result)

    assert refs == {
        "result_kind": "document-note",
        "run_id": result.run_id,
        "bundle_id": result.bundle_id,
        "manifest_sha256": result.manifest_sha256,
        "commit_sha256": result.commit_sha256,
        "source_id": result.source_id,
        "source_revision_id": result.source_revision_id,
        "artifacts": dict(result.artifacts),
        "primary_draft_artifact_id": result.artifacts["primary_draft"],
        "quality_overall": "pass",
        "publish_eligible": False,
    }

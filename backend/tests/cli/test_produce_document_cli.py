from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.cli.commands.jobs import _result_refs
from app.cli.main import main
from app.core.domain.production import RecipeProduceResult
from app.core.domain.video import JobSnapshot, JobState
from app.core.jobs.model import JobExecutionOwner
from app.core.recipes.contracts import ProduceRequest, ProduceSubmission
from app.core.domain.ids import new_typed_id
from app.engine.contracts import EngineJobReference


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
        self.workspace_instance_id = "d" * 32
        self.job_id = new_typed_id("job")
        self.requests: list[ProduceRequest] = []
        self.execution_owners: list[JobExecutionOwner] = []
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

    def submit(
        self,
        request: ProduceRequest,
        *,
        execution_owner: JobExecutionOwner = JobExecutionOwner.FOREGROUND,
    ) -> ProduceSubmission:
        self.requests.append(request)
        self.execution_owners.append(execution_owner)
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


class _EngineNotifySpy:
    def __init__(self) -> None:
        self.references: list[EngineJobReference] = []

    def notify_job(self, reference: EngineJobReference) -> dict[str, object]:
        self.references.append(reference)
        return {
            "engine_id": "engine_test",
            "workspace_instance_id": reference.workspace_instance_id,
            "job_id": reference.job_id,
            "state": "queued",
            "scheduled": True,
        }


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
    assert request.parameters == {
        "model_override": None,
        "output_language": "zh-CN",
        "provider_profile": "default",
    }
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


def test_generic_document_detach_persists_engine_owned_job_and_notifies_once(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    runtime = _DocumentRuntime()
    client = _EngineNotifySpy()

    exit_code = main(
        [
            "produce",
            str(source),
            "--recipe",
            "alltonote.document-note@1",
            "--workspace",
            str(workspace),
            "--detach",
            "--json",
        ],
        runtime=runtime,
        engine_client=client,
    )
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)

    assert exit_code == 0
    assert envelope["ok"] is True
    assert envelope["command"] == "produce"
    assert envelope["data"] == {
        "job_id": runtime.job_id,
        "state": "queued",
    }
    assert runtime.execution_owners == [JobExecutionOwner.ENGINE]
    assert runtime.wait_calls == []
    assert client.references == [
        EngineJobReference(runtime.workspace_instance_id, runtime.job_id)
    ]
    assert captured.err == ""
    assert captured.out.count("\n") == 1


def test_document_request_file_detach_uses_the_same_engine_submission_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    request_path = tmp_path / "document request.json"
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
                "parameters": {
                    "provider_profile": "composer",
                    "model_override": "fixture/composer-v1",
                    "output_language": "zh-CN",
                },
                "client_request_id": "document-detach-request",
            }
        ),
        encoding="utf-8",
    )
    runtime = _DocumentRuntime()
    client = _EngineNotifySpy()

    exit_code = main(
        [
            "produce",
            "--request",
            str(request_path),
            "--detach",
            "--json",
        ],
        runtime=runtime,
        engine_client=client,
    )
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert envelope["data"] == {
        "job_id": runtime.job_id,
        "state": "queued",
    }
    assert runtime.execution_owners == [JobExecutionOwner.ENGINE]
    assert runtime.wait_calls == []
    assert client.references == [
        EngineJobReference(runtime.workspace_instance_id, runtime.job_id)
    ]


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

    def create(
        workspace_root: Path,
        *,
        runtime_paths=None,
        current_config_snapshot=None,
        requested_model_identity=None,
        requested_provider_profile=None,
        requested_verifier_model_identity=None,
        requested_verifier_provider_profile=None,
    ) -> _DocumentRuntime:
        assert runtime_paths is None
        assert current_config_snapshot is not None
        assert requested_model_identity is None
        assert requested_provider_profile == "default"
        assert requested_verifier_model_identity is None
        assert requested_verifier_provider_profile is None
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


def test_generic_document_produce_freezes_independent_verifier_profile(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.core.config.model import ProviderProfileConfig, RuntimeConfig
    from app.runtime_config import effective_runtime_config

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")

    class ConfigService:
        def effective(self, *, profile: object, cli_overrides: object) -> object:
            del profile, cli_overrides
            return effective_runtime_config(
                RuntimeConfig(
                    default_workspace=workspace,
                    default_provider_profile="composer",
                    default_verifier_provider_profile="verifier",
                    providers={
                        "composer": ProviderProfileConfig(
                            "codex-app-server",
                            default_model="fixture/composer-v1",
                        ),
                        "verifier": ProviderProfileConfig(
                            "codex-app-server",
                            default_model="fixture/verifier-v1",
                        ),
                    },
                )
            )

    runtime = _DocumentRuntime()
    assert main(
        [
            "produce",
            str(source),
            "--recipe",
            "alltonote.document-note@1",
            "--json",
        ],
        runtime=runtime,
        config_service=ConfigService(),  # type: ignore[arg-type]
    ) == 0
    capsys.readouterr()

    assert runtime.requests[0].parameters == {
        "model_override": "fixture/composer-v1",
        "output_language": "zh-CN",
        "provider_profile": "composer",
        "verifier_model_override": "fixture/verifier-v1",
        "verifier_provider_profile": "verifier",
    }


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


def test_document_request_file_builds_runtime_from_frozen_v3_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
                "parameters": {
                    "provider_profile": "composer",
                    "model_override": "fixture/composer-v1",
                    "output_language": "en",
                    "verifier_provider_profile": "reviewer",
                    "verifier_model_override": "fixture/reviewer-v1",
                },
            }
        ),
        encoding="utf-8",
    )
    runtime = _DocumentRuntime()
    selected: list[tuple[object, ...]] = []

    def create(workspace_root: Path, **options: object) -> _DocumentRuntime:
        selected.append((workspace_root, options))
        return runtime

    monkeypatch.setattr(
        "app.runtime.create_document_runtime_for_workspace",
        create,
        raising=False,
    )

    assert main(["produce", "--request", str(request_path), "--json"]) == 0
    capsys.readouterr()

    assert selected == [
        (
            workspace.resolve(),
                {
                    "runtime_paths": None,
                    "current_config_snapshot": None,
                "requested_model_identity": "fixture/composer-v1",
                "requested_provider_profile": "composer",
                "requested_verifier_model_identity": "fixture/reviewer-v1",
                "requested_verifier_provider_profile": "reviewer",
            },
        )
    ]
    assert runtime.requests[0].parameters["verifier_model_override"] == (
        "fixture/reviewer-v1"
    )


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

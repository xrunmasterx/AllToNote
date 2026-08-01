from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from iwiki.workspace import open_workspace

from app.adapters.iwiki.portable_gateway import IWikiPortableGateway
from app.cli.main import main
from app.core.application import review_candidate_service as review_candidates
from app.core.application.review_candidate_service import ReviewCandidateService
from app.core.domain.ids import new_typed_id
from app.core.errors import DomainError
from app.core.portable.bundle_assembler import BundleAssembler
from app.core.portable.document_bundle_assembler import DocumentBundleAssembler
from tests.integration.test_document_bundle_assembly import (
    _compiled_note,
    _document_with_blocks,
    _verification,
)
from tests.integration.test_video_bundle_assembly import (
    BUNDLE_ID,
    DRAFT_ARTIFACT_ID,
    EVIDENCE_ID,
    FIXTURE_ROOT,
    QUALITY_ARTIFACT_ID,
    _bundle_input,
)


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "审阅 工作区"
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


def _commit_video(workspace_root: Path) -> None:
    candidate = BundleAssembler().assemble(_bundle_input(workspace_root))
    gateway = IWikiPortableGateway()
    prepared = gateway.prepare_candidate(
        workspace_root,
        candidate.staging_relative_path,
        expected_bundle_id=candidate.bundle_id,
        expected_manifest_sha256=candidate.manifest_sha256,
    )
    gateway.commit_prepared(prepared)


def _commit_document(workspace_root: Path) -> tuple[str, str]:
    parsed = _document_with_blocks(
        ("section_header", "Source title"),
        ("paragraph", "The source states the problem and method."),
    )
    compiled = _compiled_note()
    gateway = IWikiPortableGateway()
    candidate = DocumentBundleAssembler().assemble(
        parsed,
        compiled=compiled,
        verification=_verification(compiled, parsed=parsed),
        job_id=new_typed_id("job"),
        created_at="2026-08-01T00:00:00.000Z",
        source_canonical_identity="sha256:" + "f" * 64,
        location=gateway.candidate_location(
            workspace_root,
            local_instance_id="review-document",
            nonce="fixture",
        ),
    )
    prepared = gateway.prepare_candidate(
        workspace_root,
        candidate.candidate.staging_relative_path,
        expected_bundle_id=candidate.candidate.bundle_id,
        expected_manifest_sha256=candidate.candidate.manifest_sha256,
    )
    gateway.commit_prepared(prepared)
    return candidate.primary_draft_artifact_id, candidate.candidate.bundle_id


def _commit_unverified_document(
    workspace_root: Path,
    *,
    source_name: str = "untrusted.pdf",
) -> str:
    parsed = _document_with_blocks(
        ("section_header", "Source title"),
        ("paragraph", "The source states the problem and method."),
    )
    parsed = replace(parsed, source_name=source_name)
    gateway = IWikiPortableGateway()
    candidate = DocumentBundleAssembler().assemble(
        parsed,
        compiled=_compiled_note(),
        job_id=new_typed_id("job"),
        created_at="2026-08-01T00:00:00.000Z",
        source_canonical_identity="sha256:" + "e" * 64,
        location=gateway.candidate_location(
            workspace_root,
            local_instance_id="review-document-unverified",
            nonce="fixture",
        ),
    )
    prepared = gateway.prepare_candidate(
        workspace_root,
        candidate.candidate.staging_relative_path,
        expected_bundle_id=candidate.candidate.bundle_id,
        expected_manifest_sha256=candidate.candidate.manifest_sha256,
    )
    gateway.commit_prepared(prepared)
    return candidate.primary_draft_artifact_id


def _file_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def test_review_show_returns_bounded_video_candidate_and_exact_evidence(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle_input = _bundle_input(workspace_root)
    expected_excerpt = bundle_input.transcript.segments[0].text
    _commit_video(workspace_root)
    before = _file_snapshot(workspace_root)

    code = main(
        [
            "review",
            "show",
            DRAFT_ARTIFACT_ID,
            "--evidence-id",
            EVIDENCE_ID,
            "--workspace",
            str(workspace_root),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)

    assert code == 0
    assert envelope["command"] == "review show"
    assert envelope["alltonote_cli_protocol_version"] == 1
    data = envelope["data"]
    assert data["candidate"] == {
        "draft_id": DRAFT_ARTIFACT_ID,
        "draft_sha256": data["candidate"]["draft_sha256"],
        "document_kind": "knowledge-note",
        "bundle_id": BUNDLE_ID,
    }
    assert data["candidate"]["draft_sha256"].startswith("sha256:")
    assert data["source"] == {
        "kind": "video",
        "title": "Portable Bundle Contract",
        "author": "AllToNote",
        "channel": "AllToNote Engineering",
        "duration_ms": 1500,
        "language": "zh-CN",
        "link": "https://www.youtube.com/watch?v=portable101",
    }
    assert data["quality"]["overall"] == "pass"
    assert data["quality"]["publish_eligible"] is True
    assert data["quality"]["admission"] == {
        "status": "pass",
        "reason": "quality-profile-supported",
    }
    assert data["quality"]["reports"] == [
        {
            "artifact_id": QUALITY_ARTIFACT_ID,
            "profile": {"id": "alltonote.video-course-note", "version": 1},
            "overall": "pass",
            "method": {"kind": "deterministic"},
            "checks": data["quality"]["reports"][0]["checks"],
            "messages": [],
        }
    ]
    assert data["quality"]["reports"][0]["checks"]
    assert data["focus"] == {
        "kind": "evidence",
        "evidence_id": EVIDENCE_ID,
        "locator": {
            "scheme": "video-time-range.v1",
            "start_ms": 0,
            "end_ms": 1500,
        },
        "excerpt": expected_excerpt,
    }
    assert "body" not in data
    assert str(workspace_root) not in captured.out
    assert "openai/gpt" not in captured.out
    assert "prompt" not in captured.out.casefold()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    assert _file_snapshot(workspace_root) == before


def test_review_show_resolves_document_note_item_to_verified_source_blocks(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    draft_id, bundle_id = _commit_document(workspace_root)

    code = main(
        [
            "review",
            "show",
            draft_id,
            "--note-item-id",
            "title-0001",
            "--workspace",
            str(workspace_root),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    data = json.loads(captured.out)["data"]

    assert code == 0
    assert data["candidate"]["bundle_id"] == bundle_id
    assert data["candidate"]["document_kind"] == "knowledge-note"
    assert data["source"] == {
        "kind": "document",
        "name": "untrusted.pdf",
        "page_count": 1,
    }
    assert data["quality"]["overall"] == "pass"
    assert data["quality"]["publish_eligible"] is True
    assert data["quality"]["admission"] == {
        "status": "pass",
        "reason": "quality-profile-supported",
    }
    assert data["quality"]["reports"][0]["checks"]
    assert data["focus"]["kind"] == "note_item"
    assert data["focus"]["note_item_id"] == "title-0001"
    assert data["focus"]["verification"] == {
        "status": "supported",
        "model_identity": "fixture/verifier-v1",
    }
    assert data["focus"]["source_blocks"] == [
        {
            "block_id": "blk_1",
            "page": 1,
            "kind": "paragraph",
            "text": "The source states the problem and method.",
            "bbox": {
                "left": 50,
                "top": 100,
                "right": 500,
                "bottom": 130,
                "origin": "top-left",
            },
            "basis": "native",
        }
    ]
    assert captured.err == ""


def test_review_show_admits_verified_document_without_exposing_model_identities(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    draft_id, _ = _commit_document(workspace_root)
    before = _file_snapshot(workspace_root)

    code = main(
        [
            "review",
            "show",
            draft_id,
            "--workspace",
            str(workspace_root),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    data = json.loads(captured.out)["data"]

    assert code == 0
    assert data["quality"]["publish_eligible"] is True
    assert data["quality"]["admission"] == {
        "status": "pass",
        "reason": "quality-profile-supported",
    }
    assert data["quality"]["reports"][0]["method"] == {"kind": "model"}
    assert "focus" not in data
    assert "fixture/model-v1" not in captured.out
    assert "fixture/verifier-v1" not in captured.out
    assert captured.err == ""
    assert _file_snapshot(workspace_root) == before


def test_review_show_human_output_is_readable_and_omits_internal_ids(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _commit_video(workspace_root)

    code = main(
        [
            "review",
            "show",
            DRAFT_ARTIFACT_ID,
            "--evidence-id",
            EVIDENCE_ID,
            "--workspace",
            str(workspace_root),
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert "Portable Bundle Contract" in captured.out
    assert "Quality: pass" in captured.out
    assert "00:00.000–00:01.500" in captured.out
    assert BUNDLE_ID not in captured.out
    assert QUALITY_ARTIFACT_ID not in captured.out
    assert str(workspace_root) not in captured.out
    assert captured.err == ""


def test_review_show_exposes_complete_failure_reasons_without_loading_focus(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    draft_id = _commit_unverified_document(workspace_root)

    code = main(
        [
            "review",
            "show",
            draft_id,
            "--workspace",
            str(workspace_root),
            "--json",
        ]
    )
    data = json.loads(capsys.readouterr().out)["data"]

    assert code == 0
    assert data["quality"]["overall"] == "fail"
    assert data["quality"]["publish_eligible"] is False
    assert data["quality"]["admission"] == {
        "status": "blocked",
        "reason": "quality-report-not-publishable",
    }
    report = data["quality"]["reports"][0]
    assert {
        "id": "knowledge-note-quality",
        "status": "skipped",
        "reason": "semantic-not-evaluated",
    } in report["checks"]
    assert "knowledge-note-semantic-quality-not-evaluated" in report["messages"]
    assert "focus" not in data


@pytest.mark.parametrize(
    ("profile_id", "reason"),
    (
        (
            "alltonote.document-note",
            "legacy-document-quality-profile-not-publishable",
        ),
        (
            "alltonote.document-native-extraction",
            "document-native-extraction-not-publishable",
        ),
    ),
)
def test_legacy_document_quality_flag_cannot_admit_publication(
    profile_id: str,
    reason: str,
) -> None:
    admission = review_candidates._publication_admission(
        stored_publish_eligible=True,
        reports=(
            {
                "profile": {
                    "id": profile_id,
                    "version": 1,
                },
                "overall": "pass",
            },
        ),
    )

    assert admission == {
        "status": "blocked",
        "reason": reason,
    }


def test_historical_document_quality_profile_requires_independent_proof() -> None:
    admission = review_candidates._publication_admission(
        stored_publish_eligible=True,
        reports=(
            {
                "profile": {
                    "id": "alltonote.document-knowledge-note",
                    "version": 1,
                },
                "overall": "pass",
                "method": {"kind": "deterministic"},
                "checks": (
                    {"id": "knowledge-note-quality", "status": "pass"},
                    {"id": "source-coverage", "status": "pass"},
                ),
            },
        ),
        document_independently_verified=False,
    )

    assert admission == {
        "status": "blocked",
        "reason": "document-independent-verification-not-proven",
    }


@pytest.mark.parametrize(
    "profile_id",
    (
        "alltonote.video-course-note",
        "alltonote.video-faithful-edition",
    ),
)
def test_supported_video_quality_with_warnings_remains_publishable(
    profile_id: str,
) -> None:
    admission = review_candidates._publication_admission(
        stored_publish_eligible=True,
        reports=(
            {
                "profile": {"id": profile_id, "version": 1},
                "overall": "pass_with_warnings",
                "method": {"kind": "deterministic"},
                "checks": (),
            },
        ),
        document_independently_verified=None,
    )

    assert admission == {
        "status": "pass",
        "reason": "quality-profile-supported",
    }


def test_unknown_quality_profile_is_not_admitted() -> None:
    admission = review_candidates._publication_admission(
        stored_publish_eligible=True,
        reports=(
            {
                "profile": {"id": "untrusted.profile", "version": 1},
                "overall": "pass",
                "method": {"kind": "deterministic"},
                "checks": (),
            },
        ),
    )

    assert admission == {
        "status": "blocked",
        "reason": "quality-profile-unsupported",
    }


def test_review_show_reports_missing_focus_without_disclosing_bundle_details(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _commit_video(workspace_root)
    missing = "ev_018f0000-0000-7000-8000-0000000001ff"

    code = main(
        [
            "review",
            "show",
            DRAFT_ARTIFACT_ID,
            "--evidence-id",
            missing,
            "--workspace",
            str(workspace_root),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)

    assert code == 2
    assert envelope["error"]["code"] == "review_focus_not_found"
    assert BUNDLE_ID not in captured.out
    assert str(workspace_root) not in captured.out


def test_review_show_rejects_document_metadata_that_contains_a_private_path(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    draft_id = _commit_unverified_document(
        workspace_root,
        source_name=r"C:\Users\private-user\untrusted.pdf",
    )

    code = main(
        [
            "review",
            "show",
            draft_id,
            "--workspace",
            str(workspace_root),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)

    assert code == 10
    assert envelope["error"]["code"] == "review_candidate_invalid"
    assert "private-user" not in captured.out


@pytest.mark.parametrize(
    "broken_kind",
    (
        "source.metadata.v1",
        "selected-draft",
        "document.knowledge-map.v1",
    ),
)
def test_review_service_rejects_artifacts_not_bound_to_the_selected_draft(
    broken_kind: str,
    workspace_root: Path,
) -> None:
    draft_id, _bundle_id = _commit_document(workspace_root)
    delegate = IWikiPortableGateway()

    class _BrokenLineagePort:
        def inspect_committed(
            self,
            root: Path,
            target_id: str,
            *,
            payload_limit: int | None = None,
        ):
            inspected = delegate.inspect_committed(
                root,
                target_id,
                payload_limit=payload_limit,
            )
            if target_id != draft_id or payload_limit is not None:
                return inspected
            knowledge_id = next(
                artifact.artifact_id
                for artifact in inspected.artifacts
                if artifact.kind == "document.knowledge-map.v1"
            )
            artifacts = []
            for artifact in inspected.artifacts:
                if artifact.kind == broken_kind == "source.metadata.v1":
                    artifact = replace(
                        artifact,
                        source_revision_ids=(
                            "rev_018f0000-0000-7000-8000-0000000001ff",
                        ),
                    )
                elif artifact.kind == broken_kind == "document.knowledge-map.v1":
                    artifact = replace(artifact, parent_artifact_ids=())
                elif artifact.artifact_id == draft_id and broken_kind == "selected-draft":
                    artifact = replace(
                        artifact,
                        parent_artifact_ids=tuple(
                            parent_id
                            for parent_id in artifact.parent_artifact_ids
                            if parent_id != knowledge_id
                        ),
                    )
                artifacts.append(artifact)
            target = next(
                artifact
                for artifact in artifacts
                if artifact.artifact_id == draft_id
            )
            return replace(
                inspected,
                artifacts=tuple(artifacts),
                target_artifact=target,
            )

    service = ReviewCandidateService(_BrokenLineagePort())

    with pytest.raises(DomainError) as captured:
        service.show(
            workspace_root,
            draft_id,
            note_item_id=(
                "title-0001"
                if broken_kind in {"selected-draft", "document.knowledge-map.v1"}
                else None
            ),
        )

    assert captured.value.code == "review_candidate_invalid"


def test_review_show_rejects_multiple_focuses_with_nested_command_identity(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        [
            "review",
            "show",
            DRAFT_ARTIFACT_ID,
            "--evidence-id",
            EVIDENCE_ID,
            "--note-item-id",
            "title-0001",
            "--json",
        ]
    )
    envelope = json.loads(capsys.readouterr().out)

    assert code == 2
    assert envelope["command"] == "review show"
    assert envelope["error"]["code"] == "cli_usage_invalid"


def test_review_show_fails_closed_when_committed_draft_is_tampered(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _commit_video(workspace_root)
    workspace = open_workspace(workspace_root, writable=True)
    bundle_root = workspace.resolve_contract_path("raw_personal") / "bundles" / BUNDLE_ID
    (bundle_root / "drafts" / f"{DRAFT_ARTIFACT_ID}.md").write_text(
        "# tampered\n",
        encoding="utf-8",
    )

    code = main(
        [
            "review",
            "show",
            DRAFT_ARTIFACT_ID,
            "--workspace",
            str(workspace_root),
            "--json",
        ]
    )
    envelope = json.loads(capsys.readouterr().out)

    assert code == 20
    assert envelope["command"] == "review show"
    assert envelope["error"]["code"] == "review_candidate_stale"

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from iwiki.workspace import open_workspace

from app.adapters.iwiki.portable_gateway import IWikiPortableGateway
from app.cli.main import main
from app.core.portable.bundle_assembler import BundleAssembler
from tests.integration.test_video_bundle_assembly import (
    BUNDLE_ID,
    DRAFT_ARTIFACT_ID,
    EVIDENCE_ARTIFACT_ID,
    FAITHFUL_DRAFT_ARTIFACT_ID,
    FIXTURE_ROOT,
    QUALITY_ARTIFACT_ID,
    SOURCE_ID,
    TRANSCRIPT_ARTIFACT_ID,
    _bundle_input,
    _v2_bundle_input,
)


MISSING_ARTIFACT_ID = "art_018f0000-0000-7000-8000-0000000001ff"


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "工作 空间"
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


def _commit(workspace_root: Path, *, v2: bool = False) -> None:
    bundle_input = (
        _v2_bundle_input(workspace_root, dual=True)
        if v2
        else _bundle_input(workspace_root)
    )
    candidate = BundleAssembler().assemble(bundle_input)
    gateway = IWikiPortableGateway()
    prepared = gateway.prepare_candidate(
        workspace_root,
        candidate.staging_relative_path,
        expected_bundle_id=candidate.bundle_id,
        expected_manifest_sha256=candidate.manifest_sha256,
    )
    gateway.commit_prepared(prepared)


def _file_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("command", ("inspect", "show"))
def test_artifact_inspect_returns_safe_verified_projection_without_body_or_writes(
    command: str,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _commit(workspace_root)
    before = _file_snapshot(workspace_root)

    code = main(
        [
            "artifact",
            command,
            DRAFT_ARTIFACT_ID,
            "--workspace",
            str(workspace_root),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)

    assert code == 0
    assert envelope["ok"] is True
    assert envelope["command"] == f"artifact {command}"
    data = envelope["data"]
    assert data["target_kind"] == "artifact"
    assert data["bundle"]["bundle_id"] == BUNDLE_ID
    assert data["bundle"]["manifest_sha256"].startswith("sha256:")
    assert data["artifact"]["artifact_id"] == DRAFT_ARTIFACT_ID
    assert data["artifact"]["kind"] == "knowledge.draft.markdown.v1"
    assert data["artifact"]["media_type"] == "text/markdown"
    assert data["artifact"]["size_bytes"] > 0
    assert data["artifact"]["sha256"].startswith("sha256:")
    assert data["artifact"]["compiler_identity"] == (
        "alltonote.video-source-bundle@1.0.0"
    )
    assert data["artifact"]["parent_artifact_ids"] == []
    assert data["artifact"]["source_revision_ids"] == [
        "rev_018f0000-0000-7000-8000-000000000103"
    ]
    assert data["artifact"]["quality_report_ids"] == [QUALITY_ARTIFACT_ID]
    assert data["quality"] == {
        "overall": "pass",
        "publish_eligible": True,
        "repair_attempts": 0,
    }
    assert data["source"] == {
        "sources": [
            {
                "source_id": SOURCE_ID,
                "kind": "video",
                "connector_id": "youtube",
                "platform": "youtube",
            }
        ],
        "source_revision_ids": [
            "rev_018f0000-0000-7000-8000-000000000103"
        ],
    }
    assert data["evidence"]["transcript_artifact_id"] == TRANSCRIPT_ARTIFACT_ID
    assert data["evidence"]["evidence_set_artifact_id"] == EVIDENCE_ARTIFACT_ID
    assert "body" not in data
    assert str(workspace_root) not in captured.out
    assert "portable101" not in captured.out
    assert "youtube.com" not in captured.out
    assert "openai/gpt" not in captured.out
    assert "provider_raw" not in captured.out
    assert "prompt" not in captured.out.casefold()
    assert captured.err == ""
    assert _file_snapshot(workspace_root) == before


def test_bundle_inspect_returns_bounded_inventory_without_payload_paths(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _commit(workspace_root)

    code = main(
        [
            "artifact",
            "inspect",
            BUNDLE_ID,
            "--workspace",
            str(workspace_root),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    data = json.loads(captured.out)["data"]

    assert code == 0
    assert data["target_kind"] == "bundle"
    assert data["bundle"]["artifact_count"] == 5
    assert data["bundle"]["primary_draft_artifact_id"] == DRAFT_ARTIFACT_ID
    assert {artifact["artifact_id"] for artifact in data["artifacts"]} == {
        DRAFT_ARTIFACT_ID,
        EVIDENCE_ARTIFACT_ID,
        QUALITY_ARTIFACT_ID,
        TRANSCRIPT_ARTIFACT_ID,
        "art_018f0000-0000-7000-8000-000000000104",
    }
    assert "path" not in captured.out.casefold()
    assert str(workspace_root) not in captured.out


def test_draft_inspect_summarizes_headings_and_evidence_without_default_body(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _commit(workspace_root)

    code = main(
        [
            "draft",
            "inspect",
            DRAFT_ARTIFACT_ID,
            "--workspace",
            str(workspace_root),
            "--json",
        ]
    )
    envelope = json.loads(capsys.readouterr().out)
    data = envelope["data"]

    assert code == 0
    assert data["target_kind"] == "draft"
    assert data["draft"]["artifact_id"] == DRAFT_ARTIFACT_ID
    assert data["draft"]["document_kind"] == "knowledge-note"
    assert data["draft"]["heading_count"] == 2
    assert data["draft"]["headings"] == [
        {"level": 1, "text": "可移植视频笔记"},
        {"level": 2, "text": "验证边界"},
    ]
    assert data["draft"]["evidence_reference_count"] == 1
    assert data["draft"]["evidence_ids"] == [
        "ev_018f0000-0000-7000-8000-000000000109"
    ]
    assert "body" not in data


def test_draft_body_requires_explicit_bounded_option_and_is_truncated_safely(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _commit(workspace_root)

    code = main(
        [
            "draft",
            "inspect",
            DRAFT_ARTIFACT_ID,
            "--body-bytes",
            "48",
            "--workspace",
            str(workspace_root),
            "--json",
        ]
    )
    data = json.loads(capsys.readouterr().out)["data"]

    assert code == 0
    assert 0 < len(data["body"].encode("utf-8")) <= 48
    assert data["body_bytes_limit"] == 48
    assert data["body_bytes_returned"] == len(data["body"].encode("utf-8"))
    assert data["body_truncated"] is True


def test_draft_show_defaults_to_clean_reading_projection(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _commit(workspace_root)
    before = _file_snapshot(workspace_root)

    code = main(
        [
            "draft",
            "show",
            DRAFT_ARTIFACT_ID,
            "--workspace",
            str(workspace_root),
            "--json",
        ]
    )
    data = json.loads(capsys.readouterr().out)["data"]

    assert code == 0
    assert data["body_presentation"] == "reading"
    assert data["body"].startswith("# ")
    assert "Bundle" in data["body"]
    assert "[^ev_" not in data["body"]
    assert ": 视频 00:00" not in data["body"]
    assert data["draft"]["evidence_reference_count"] == 1
    assert _file_snapshot(workspace_root) == before


def test_draft_show_can_return_canonical_audit_markdown(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _commit(workspace_root)

    code = main(
        [
            "draft",
            "show",
            DRAFT_ARTIFACT_ID,
            "--presentation",
            "audit",
            "--workspace",
            str(workspace_root),
            "--json",
        ]
    )
    data = json.loads(capsys.readouterr().out)["data"]

    assert code == 0
    assert data["body_presentation"] == "audit"
    assert "[^ev_018f0000-0000-7000-8000-000000000109]" in data["body"]
    assert ": 视频 00:00–00:01" in data["body"]


def test_draft_show_human_output_is_the_reading_markdown(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _commit(workspace_root)

    code = main(
        [
            "draft",
            "show",
            DRAFT_ARTIFACT_ID,
            "--workspace",
            str(workspace_root),
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out.startswith("# ")
    assert "Bundle" in captured.out
    assert "Artifact:" not in captured.out
    assert "[^ev_" not in captured.out
    assert captured.err == ""


def test_v2_faithful_and_knowledge_drafts_keep_distinct_document_kinds(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _commit(workspace_root, v2=True)

    observed: dict[str, dict[str, object]] = {}
    for artifact_id in (DRAFT_ARTIFACT_ID, FAITHFUL_DRAFT_ARTIFACT_ID):
        code = main(
            [
                "draft",
                "inspect",
                artifact_id,
                "--workspace",
                str(workspace_root),
                "--json",
            ]
        )
        envelope = json.loads(capsys.readouterr().out)
        assert code == 0
        observed[artifact_id] = envelope["data"]

    assert observed[DRAFT_ARTIFACT_ID]["draft"]["document_kind"] == (
        "knowledge-note"
    )
    faithful = observed[FAITHFUL_DRAFT_ARTIFACT_ID]
    assert faithful["draft"]["document_kind"] == "faithful-edition"
    assert faithful["draft"]["heading_count"] == 2
    assert faithful["quality"]["overall"] == "pass"
    assert faithful["bundle"]["draft_artifact_ids"] == [
        DRAFT_ARTIFACT_ID,
        FAITHFUL_DRAFT_ARTIFACT_ID,
    ]


@pytest.mark.parametrize(
    ("command", "target", "expected_code"),
    (
        ("artifact", "../../bundle.json", "artifact_target_invalid"),
        ("artifact", MISSING_ARTIFACT_ID, "artifact_not_found"),
        ("draft", QUALITY_ARTIFACT_ID, "draft_not_found"),
    ),
)
def test_inspect_rejects_paths_missing_ids_and_non_drafts_without_disclosure(
    command: str,
    target: str,
    expected_code: str,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _commit(workspace_root)

    code = main(
        [
            command,
            "inspect",
            target,
            "--workspace",
            str(workspace_root),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)

    assert code == 2
    assert envelope["error"]["code"] == expected_code
    assert str(workspace_root) not in captured.out
    assert "bundle.json" not in envelope["error"]["message"]


def test_tampered_committed_payload_fails_closed_as_stale(
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _commit(workspace_root)
    workspace = open_workspace(workspace_root, writable=True)
    bundle_root = workspace.resolve_contract_path("raw_personal") / "bundles" / BUNDLE_ID
    (bundle_root / "drafts" / f"{DRAFT_ARTIFACT_ID}.md").write_text(
        "# tampered\n",
        encoding="utf-8",
    )

    code = main(
        [
            "draft",
            "inspect",
            DRAFT_ARTIFACT_ID,
            "--workspace",
            str(workspace_root),
            "--json",
        ]
    )
    envelope = json.loads(capsys.readouterr().out)

    assert code == 20
    assert envelope["error"]["code"] == "draft_stale"
    assert envelope["error"]["retryable"] is False


def test_contract_mismatch_is_reported_before_artifact_projection(
    monkeypatch: pytest.MonkeyPatch,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _commit(workspace_root)
    from app.adapters.iwiki import portable_gateway as gateway_module

    original = gateway_module._load_runtime_lock

    def incompatible_lock() -> dict[str, object]:
        payload = original()
        return {**payload, "portable_contract_id": "incompatible-contract"}

    monkeypatch.setattr(gateway_module, "_load_runtime_lock", incompatible_lock)

    code = main(
        [
            "artifact",
            "inspect",
            DRAFT_ARTIFACT_ID,
            "--workspace",
            str(workspace_root),
            "--json",
        ]
    )
    envelope = json.loads(capsys.readouterr().out)

    assert code == 10
    assert envelope["error"]["code"] == "portable_contract_incompatible"


@pytest.mark.parametrize("limit", ("0", "262145"))
def test_body_limit_is_strictly_bounded(
    limit: str,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _commit(workspace_root)

    code = main(
        [
            "artifact",
            "inspect",
            DRAFT_ARTIFACT_ID,
            "--body-bytes",
            limit,
            "--workspace",
            str(workspace_root),
            "--json",
        ]
    )
    envelope = json.loads(capsys.readouterr().out)

    assert code == 2
    assert envelope["error"]["code"] == "artifact_body_limit_invalid"


def test_artifact_usage_error_keeps_nested_command_identity(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["artifact", "inspect", "--json"])
    envelope = json.loads(capsys.readouterr().out)

    assert code == 2
    assert envelope["command"] == "artifact inspect"
    assert envelope["error"]["code"] == "cli_usage_invalid"

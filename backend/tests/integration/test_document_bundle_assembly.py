from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from app.adapters.iwiki.portable_gateway import IWikiPortableGateway
from app.core.domain.document import (
    DocumentBlock,
    DocumentBoundingBox,
    DocumentPage,
    ParsedDocument,
)
from app.core.domain.ids import new_typed_id
from app.core.portable.document_bundle_assembler import DocumentBundleAssembler


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "workspace-v2"


def test_document_candidate_is_semantically_valid_portable_bundle(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    shutil.copytree(FIXTURE_ROOT, workspace)
    shutil.rmtree(workspace / "raw" / "personal" / ".staging")
    for relative in (
        "raw/common",
        "raw/personal/.staging",
        "wiki/common",
        "wiki/personal",
        ".cache",
    ):
        (workspace / relative).mkdir(parents=True, exist_ok=True)
    text = "Real-Time Rendering of Glossy Reflections"
    parsed = ParsedDocument(
        source_sha256="sha256:" + "a" * 64,
        source_name="paper.pdf",
        parser_id="docling",
        parser_version="2.117.0",
        model_revision="8f39ad3c0b4c58e9c2d2c84a38465abf757272d8",
        pages=(
            DocumentPage(
                1,
                612.0,
                792.0,
                (
                    DocumentBlock(
                        "blk_title",
                        1,
                        0,
                        "section_header",
                        text,
                        "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
                        DocumentBoundingBox(50, 50, 500, 80),
                    ),
                ),
            ),
        ),
        metadata={"status": "success"},
        warnings=(),
    )
    body_text = "A deterministic document paragraph."
    parsed = ParsedDocument(
        source_sha256=parsed.source_sha256,
        source_name=parsed.source_name,
        parser_id=parsed.parser_id,
        parser_version=parsed.parser_version,
        model_revision=parsed.model_revision,
        pages=(
            DocumentPage(
                1,
                612.0,
                792.0,
                (
                    parsed.pages[0].blocks[0],
                    DocumentBlock(
                        "blk_body",
                        1,
                        1,
                        "paragraph",
                        body_text,
                        "sha256:" + hashlib.sha256(body_text.encode()).hexdigest(),
                        DocumentBoundingBox(50, 100, 500, 140),
                    ),
                ),
            ),
        ),
        metadata=parsed.metadata,
        warnings=parsed.warnings,
    )
    gateway = IWikiPortableGateway()
    candidate = DocumentBundleAssembler().assemble(
        parsed,
        job_id=new_typed_id("job"),
        created_at="2026-07-30T00:00:00.000Z",
        location=gateway.candidate_location(
            workspace,
            local_instance_id="document-x0b",
            nonce="fixture",
        ),
    )

    report = gateway.validate_candidate(
        workspace,
        candidate.candidate.staging_relative_path,
    )

    assert report.valid
    assert candidate.candidate.manifest_sha256.startswith("sha256:")
    assert candidate.primary_draft_artifact_id in candidate.candidate.artifact_ids
    primary_draft = (
        candidate.candidate.absolute_path
        / "drafts"
        / f"{candidate.primary_draft_artifact_id}.md"
    ).read_text(encoding="utf-8")
    evidence_records = [
        json.loads(line)
        for line in (
            candidate.candidate.absolute_path
            / "evidence"
            / "evidence-set.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]

    assert primary_draft == f"# {text}\n\n{body_text}\n\n"
    assert "[^ev_" not in primary_draft
    assert "Document page" not in primary_draft
    assert evidence_records[0]["record_count"] == 2
    assert evidence_records[1]["evidence_id"].startswith("ev_")
    assert evidence_records[1]["locator"] == {
        "scheme": "document-page.v1",
        "page": 1,
        "bbox": {
            "left": 50 / 612,
            "top": 50 / 792,
            "right": 500 / 612,
            "bottom": 80 / 792,
        },
    }
    assert evidence_records[1]["excerpt_sha256"] == parsed.pages[0].blocks[0].content_sha256
    assert evidence_records[2]["locator"]["page"] == 1
    assert evidence_records[2]["excerpt_sha256"] == parsed.pages[0].blocks[1].content_sha256

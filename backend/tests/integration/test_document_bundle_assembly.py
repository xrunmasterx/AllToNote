from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import replace
from pathlib import Path

from markdown_it import MarkdownIt

from app.adapters.iwiki.portable_gateway import IWikiPortableGateway
from app.core.application.document_knowledge_compiler import (
    CompiledDocumentKnowledgeNoteV1,
    DocumentKnowledgeClaimV1,
    DocumentKnowledgeSectionV1,
)
from app.core.application.document_knowledge_verifier import (
    DocumentKnowledgeClaimVerificationV1,
    DocumentKnowledgeVerificationV1,
    compiled_document_knowledge_sha256,
    document_knowledge_evidence_sha256,
    document_knowledge_claims,
)
from app.core.domain.document import (
    DocumentBlock,
    DocumentBoundingBox,
    DocumentPage,
    ParsedDocument,
)
from app.core.domain.ids import new_typed_id
from app.core.errors import DomainError, ErrorCategory
from app.core.portable.document_bundle_assembler import DocumentBundleAssembler
from app.core.portable.markdown_safety import validate_markdown_safety


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "workspace-v2"


def _candidate_workspace(tmp_path: Path) -> Path:
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
    return workspace


def _document_with_blocks(*blocks: tuple[str, str]) -> ParsedDocument:
    document_blocks = tuple(
        DocumentBlock(
            f"blk_{index}",
            1,
            index,
            kind,
            text,
            "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
            DocumentBoundingBox(50, 50 + index * 50, 500, 80 + index * 50),
        )
        for index, (kind, text) in enumerate(blocks)
    )
    return ParsedDocument(
        source_sha256="sha256:" + "a" * 64,
        source_name="untrusted.pdf",
        parser_id="docling",
        parser_version="2.117.0",
        model_revision="8f39ad3c0b4c58e9c2d2c84a38465abf757272d8",
        pages=(DocumentPage(1, 612.0, 792.0, document_blocks),),
        metadata={},
        warnings=(),
    )


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
        source_canonical_identity="sha256:" + "f" * 64,
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


def test_document_candidate_renders_untrusted_pdf_text_without_markdown_authority(
    tmp_path: Path,
) -> None:
    workspace = _candidate_workspace(tmp_path)
    title = "<script>alert(1)</script>"
    body = (
        "![tracking pixel](https://tracker.invalid/pixel)\n\n"
        "# Injected heading\n- injected item\n1. injected order\n"
        "[external link](https://tracker.invalid/page)\n\n"
        "```mermaid\ngraph TD; A-->B\n```"
    )
    table = "| value |\n| --- |\n| <img src=x onerror=alert(1)> |"
    parsed = _document_with_blocks(
        ("section_header", title),
        ("paragraph", body),
        ("table", table),
    )
    gateway = IWikiPortableGateway()

    candidate = DocumentBundleAssembler().assemble(
        parsed,
        job_id=new_typed_id("job"),
        created_at="2026-07-31T00:00:00.000Z",
        source_canonical_identity="sha256:" + "f" * 64,
        location=gateway.candidate_location(
            workspace,
            local_instance_id="document-markdown-safety",
            nonce="fixture",
        ),
    )
    draft_path = (
        candidate.candidate.absolute_path
        / "drafts"
        / f"{candidate.primary_draft_artifact_id}.md"
    )
    draft = draft_path.read_text(encoding="utf-8")
    quality = json.loads(
        (
            candidate.candidate.absolute_path
            / "quality"
            / f"{candidate.quality_report_artifact_id}.json"
        ).read_text(encoding="utf-8")
    )
    receipt = json.loads(
        (candidate.candidate.absolute_path / "receipt.json").read_text(encoding="utf-8")
    )
    normalized_records = [
        json.loads(line)
        for line in (
            candidate.candidate.absolute_path
            / "evidence"
            / "document-content.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]

    validate_markdown_safety(
        draft,
        bundle_relative_path=f"drafts/{candidate.primary_draft_artifact_id}.md",
    )
    assert gateway.validate_candidate(
        workspace,
        candidate.candidate.staging_relative_path,
    ).valid
    assert title in draft
    assert "tracking pixel" in draft
    assert "graph TD" in draft
    assert "\\# Injected heading" in draft
    assert "\\- injected item" in draft
    assert "1\\. injected order" in draft
    assert "\\[external link\\]" in draft
    assert table in draft
    assert [record["text"] for record in normalized_records[1:]] == [
        title,
        body,
        table,
    ]
    assert candidate.quality_overall == "pass"
    assert candidate.publish_eligible is False
    assert quality["profile"] == {
        "id": "alltonote.document-native-extraction",
        "version": 1,
    }
    checks = {check["id"]: check for check in quality["checks"]}
    assert checks["markdown-safety"]["status"] == "pass"
    assert checks["knowledge-note-quality"] == {
        "id": "knowledge-note-quality",
        "status": "skipped",
        "reason": "not-evaluated",
    }
    assert "knowledge-note-quality-not-evaluated" in quality["messages"]
    assert receipt["quality"] == {
        "overall": "pass",
        "publish_eligible": False,
        "repair_attempts": 0,
    }


def test_document_candidate_fails_closed_when_final_markdown_validation_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _candidate_workspace(tmp_path)
    parsed = _document_with_blocks(
        ("section_header", "Document title"),
        ("paragraph", "Original extracted body"),
    )
    gateway = IWikiPortableGateway()

    def reject_markdown(*args, **kwargs):
        del args, kwargs
        raise DomainError(
            "unsafe_markdown",
            ErrorCategory.INVALID_REQUEST,
            "unsafe markdown",
        )

    monkeypatch.setattr(
        "app.core.portable.document_bundle_assembler.validate_markdown_safety",
        reject_markdown,
    )
    candidate = DocumentBundleAssembler().assemble(
        parsed,
        job_id=new_typed_id("job"),
        created_at="2026-07-31T00:00:00.000Z",
        source_canonical_identity="sha256:" + "f" * 64,
        location=gateway.candidate_location(
            workspace,
            local_instance_id="document-markdown-fail-closed",
            nonce="fixture",
        ),
    )
    draft = (
        candidate.candidate.absolute_path
        / "drafts"
        / f"{candidate.primary_draft_artifact_id}.md"
    ).read_text(encoding="utf-8")
    quality = json.loads(
        (
            candidate.candidate.absolute_path
            / "quality"
            / f"{candidate.quality_report_artifact_id}.json"
        ).read_text(encoding="utf-8")
    )
    receipt = json.loads(
        (candidate.candidate.absolute_path / "receipt.json").read_text(encoding="utf-8")
    )

    assert gateway.validate_candidate(
        workspace,
        candidate.candidate.staging_relative_path,
    ).valid
    assert draft == (
        "# Document output blocked\n\n"
        "The extracted content could not be rendered safely.\n"
    )
    assert "Original extracted body" not in draft
    assert candidate.quality_overall == "fail"
    assert candidate.publish_eligible is False
    assert quality["overall"] == "fail"
    assert {check["id"]: check["status"] for check in quality["checks"]}[
        "markdown-safety"
    ] == "fail"
    assert receipt["quality"]["publish_eligible"] is False
    assert receipt["quality"]["overall"] == "fail"
    assert quality["subject"]["artifact_id"] == candidate.primary_draft_artifact_id
    assert quality["subject"]["sha256"] == (
        "sha256:" + hashlib.sha256(draft.encode()).hexdigest()
    )


def test_document_candidate_is_not_publishable_when_extraction_is_empty(
    tmp_path: Path,
) -> None:
    workspace = _candidate_workspace(tmp_path)
    parsed = ParsedDocument(
        source_sha256="sha256:" + "a" * 64,
        source_name="empty.pdf",
        parser_id="docling",
        parser_version="2.117.0",
        model_revision="fixture-model",
        pages=(DocumentPage(1, 612.0, 792.0, ()),),
        metadata={"status": "success"},
        warnings=(),
    )

    candidate = DocumentBundleAssembler().assemble(
        parsed,
        job_id=new_typed_id("job"),
        created_at="2026-07-31T00:00:00.000Z",
        source_canonical_identity="sha256:" + "f" * 64,
        location=IWikiPortableGateway().candidate_location(
            workspace,
            local_instance_id="document-empty-quality",
            nonce="fixture",
        ),
    )
    quality = json.loads(
        (
            candidate.candidate.absolute_path
            / "quality"
            / f"{candidate.quality_report_artifact_id}.json"
        ).read_text(encoding="utf-8")
    )

    assert candidate.quality_overall == "fail"
    assert candidate.publish_eligible is False
    checks = {check["id"]: check["status"] for check in quality["checks"]}
    assert checks["native-text"] == "fail"
    assert checks["page-coverage"] == "fail"
    assert "empty-document" in quality["messages"]


def test_document_candidate_is_not_publishable_for_partial_extraction(
    tmp_path: Path,
) -> None:
    workspace = _candidate_workspace(tmp_path)
    parsed = replace(
        _document_with_blocks(("paragraph", "Only the recovered page")),
        metadata={"status": "partial_success"},
        warnings=("A page could not be converted",),
    )

    candidate = DocumentBundleAssembler().assemble(
        parsed,
        job_id=new_typed_id("job"),
        created_at="2026-07-31T00:00:00.000Z",
        source_canonical_identity="sha256:" + "f" * 64,
        location=IWikiPortableGateway().candidate_location(
            workspace,
            local_instance_id="document-partial-quality",
            nonce="fixture",
        ),
    )
    quality = json.loads(
        (
            candidate.candidate.absolute_path
            / "quality"
            / f"{candidate.quality_report_artifact_id}.json"
        ).read_text(encoding="utf-8")
    )

    assert candidate.quality_overall == "fail"
    assert candidate.publish_eligible is False
    checks = {check["id"]: check["status"] for check in quality["checks"]}
    assert checks["parser-completeness"] == "fail"
    assert "partial-extraction" in quality["messages"]


def test_document_literal_projection_blocks_gfm_autolinks_and_setext_headings(
    tmp_path: Path,
) -> None:
    workspace = _candidate_workspace(tmp_path)
    body = (
        "Bare URL https://tracker.invalid/page\n"
        "Website www.tracker.invalid/page\n"
        "Underscore _www.tracker.invalid/under\n"
        "Email reviewer@tracker.invalid\n"
        "Injected heading one\n=\n"
        "Injected heading two\n-"
    )
    candidate = DocumentBundleAssembler().assemble(
        _document_with_blocks(("paragraph", body)),
        job_id=new_typed_id("job"),
        created_at="2026-07-31T00:00:00.000Z",
        source_canonical_identity="sha256:" + "f" * 64,
        location=IWikiPortableGateway().candidate_location(
            workspace,
            local_instance_id="document-gfm-literal-safety",
            nonce="fixture",
        ),
    )
    draft = (
        candidate.candidate.absolute_path
        / "drafts"
        / f"{candidate.primary_draft_artifact_id}.md"
    ).read_text(encoding="utf-8")
    rendered = MarkdownIt("commonmark").render(draft)
    tokens = MarkdownIt("commonmark").parse(draft)
    inline_children = [
        child
        for token in tokens
        if token.children
        for child in token.children
    ]
    heading_text = [
        tokens[index + 1].content
        for index, token in enumerate(tokens)
        if token.type == "heading_open"
    ]
    code_text = {
        token.content for token in inline_children if token.type == "code_inline"
    }
    gfm_autolink_trigger = re.compile(
        r"https?://|(?<![A-Za-z0-9])www\.|[^\s<>@]+@[^\s<>@]+",
        re.IGNORECASE,
    )

    assert {
        "https://tracker.invalid/page",
        "www.tracker.invalid/page",
        "www.tracker.invalid/under",
        "reviewer@tracker.invalid",
    }.issubset(code_text)
    assert all(token.type != "link_open" for token in inline_children)
    assert all(
        gfm_autolink_trigger.search(token.content) is None
        for token in inline_children
        if token.type == "text"
    )
    assert heading_text == ["untrusted.pdf"]
    assert "<a " not in rendered
    assert "<code>https://tracker.invalid/page</code>" in rendered
    assert "<code>www.tracker.invalid/page</code>" in rendered
    assert "_<code>www.tracker.invalid/under</code>" in rendered
    assert "<code>reviewer@tracker.invalid</code>" in rendered
    assert candidate.publish_eligible is False


def test_document_candidate_preserves_safe_docling_table_structure(
    tmp_path: Path,
) -> None:
    workspace = _candidate_workspace(tmp_path)
    table = (
        "Table 1: Runtime entry points\n\n"
        "| Type | Entry |\n"
        "| --- | --- |\n"
        "| UJ3BakedVolumetricGIData | GIData::Serialize() |\n"
        "| Pipe expression | A &#124; B |"
    )
    parsed = _document_with_blocks(
        ("section_header", "Runtime reference"),
        ("table", table),
    )

    candidate = DocumentBundleAssembler().assemble(
        parsed,
        job_id=new_typed_id("job"),
        created_at="2026-07-31T00:00:00.000Z",
        source_canonical_identity="sha256:" + "f" * 64,
        location=IWikiPortableGateway().candidate_location(
            workspace,
            local_instance_id="document-table-safety",
            nonce="fixture",
        ),
    )
    draft = (
        candidate.candidate.absolute_path
        / "drafts"
        / f"{candidate.primary_draft_artifact_id}.md"
    ).read_text(encoding="utf-8")

    validate_markdown_safety(
        draft,
        bundle_relative_path=f"drafts/{candidate.primary_draft_artifact_id}.md",
    )
    assert "```text" not in draft
    assert "| Type | Entry |" in draft
    assert "| --- | --- |" in draft
    assert "GIData\\::Serialize\\(\\)" in draft
    assert "A &#124; B" in draft
    rendered = MarkdownIt("commonmark").enable("table").render(draft)
    assert "<table>" in rendered
    assert "A | B" in rendered
    assert candidate.publish_eligible is False


def _compiled_note(
    *,
    overview_sources: tuple[str, ...] = ("blk_1",),
    section_sources: tuple[str, ...] = ("blk_1", "blk_2"),
) -> CompiledDocumentKnowledgeNoteV1:
    referenced = tuple(dict.fromkeys((*overview_sources, *section_sources)))
    return CompiledDocumentKnowledgeNoteV1(
        title=DocumentKnowledgeClaimV1(
            "A human-readable knowledge note",
            overview_sources,
        ),
        overview=(
            DocumentKnowledgeClaimV1(
                "This note explains the central problem.",
                overview_sources,
            ),
        ),
        sections=(
            DocumentKnowledgeSectionV1(
                DocumentKnowledgeClaimV1(
                    "How the method works",
                    section_sources,
                ),
                (
                    DocumentKnowledgeClaimV1(
                        "The method follows a clear sequence.",
                        section_sources,
                    ),
                ),
                (
                    DocumentKnowledgeClaimV1(
                        "The source supports the main result.",
                        section_sources,
                    ),
                ),
            ),
        ),
        referenced_block_ids=referenced,
        model_identity="fixture/model-v1",
        input_tokens=120,
        output_tokens=80,
        token_counts_complete=True,
    )


def _verification(
    compiled: CompiledDocumentKnowledgeNoteV1,
    *,
    parsed: ParsedDocument,
    status: str = "supported",
) -> DocumentKnowledgeVerificationV1:
    return DocumentKnowledgeVerificationV1(
        compiled_sha256=compiled_document_knowledge_sha256(compiled),
        evidence_input_sha256=document_knowledge_evidence_sha256(parsed, compiled),
        claims=tuple(
            DocumentKnowledgeClaimVerificationV1(claim_id, status)
            for claim_id, _claim in document_knowledge_claims(compiled)
        ),
        model_identity="fixture/verifier-v1",
        input_tokens=40,
        output_tokens=20,
        token_counts_complete=True,
    )


def test_compiled_document_is_not_publishable_without_semantic_verification(
    tmp_path: Path,
) -> None:
    workspace = _candidate_workspace(tmp_path)
    parsed = _document_with_blocks(
        ("section_header", "Source title"),
        ("paragraph", "The source states the problem."),
        ("paragraph", "The source explains the method."),
    )
    gateway = IWikiPortableGateway()

    candidate = DocumentBundleAssembler().assemble(
        parsed,
        compiled=_compiled_note(),
        job_id=new_typed_id("job"),
        created_at="2026-07-31T00:00:00.000Z",
        source_canonical_identity="sha256:" + "f" * 64,
        location=gateway.candidate_location(
            workspace,
            local_instance_id="document-knowledge-note",
            nonce="fixture",
        ),
    )

    draft = (
        candidate.candidate.absolute_path
        / "drafts"
        / f"{candidate.primary_draft_artifact_id}.md"
    ).read_text(encoding="utf-8")
    knowledge_map = json.loads(
        (
            candidate.candidate.absolute_path
            / "evidence"
            / "knowledge-map.json"
        ).read_text(encoding="utf-8")
    )
    quality = json.loads(
        (
            candidate.candidate.absolute_path
            / "quality"
            / f"{candidate.quality_report_artifact_id}.json"
        ).read_text(encoding="utf-8")
    )

    assert draft == (
        "# A human-readable knowledge note\n\n"
        "This note explains the central problem.\n\n"
        "## How the method works\n\n"
        "The method follows a clear sequence.\n\n"
        "- The source supports the main result.\n"
    )
    assert "blk_" not in draft
    assert "Document page" not in draft
    assert knowledge_map["referenced_block_ids"] == ["blk_1", "blk_2"]
    assert knowledge_map["items"][0] == {
        "note_item_id": "title-0001",
        "source_block_ids": ["blk_1"],
    }
    assert knowledge_map["items"][2] == {
        "note_item_id": "section-0001-heading-0001",
        "source_block_ids": ["blk_1", "blk_2"],
    }
    assert candidate.knowledge_map_artifact_id is not None
    assert candidate.quality_overall == "fail"
    assert candidate.publish_eligible is False
    assert quality["profile"] == {
        "id": "alltonote.document-knowledge-note",
        "version": 1,
    }
    checks = {value["id"]: value for value in quality["checks"]}
    assert checks["knowledge-note-quality"] == {
        "id": "knowledge-note-quality",
        "status": "skipped",
        "reason": "semantic-not-evaluated",
    }
    assert checks["source-coverage"]["status"] == "pass"
    assert "knowledge-note-semantic-quality-not-evaluated" in quality["messages"]
    assert quality["metrics"]["referenced_block_count"] == 2


def test_compiled_document_low_source_coverage_is_not_publishable(
    tmp_path: Path,
) -> None:
    workspace = _candidate_workspace(tmp_path)
    parsed = _document_with_blocks(
        ("section_header", "Source title"),
        ("paragraph", "A" * 100),
        ("paragraph", "B" * 100),
        ("paragraph", "C" * 100),
        ("paragraph", "D" * 100),
    )
    gateway = IWikiPortableGateway()

    candidate = DocumentBundleAssembler().assemble(
        parsed,
        compiled=_compiled_note(
            overview_sources=("blk_1",),
            section_sources=("blk_1",),
        ),
        job_id=new_typed_id("job"),
        created_at="2026-07-31T00:00:00.000Z",
        source_canonical_identity="sha256:" + "f" * 64,
        location=gateway.candidate_location(
            workspace,
            local_instance_id="document-low-coverage",
            nonce="fixture",
        ),
    )
    quality = json.loads(
        (
            candidate.candidate.absolute_path
            / "quality"
            / f"{candidate.quality_report_artifact_id}.json"
        ).read_text(encoding="utf-8")
    )

    assert candidate.quality_overall == "fail"
    assert candidate.publish_eligible is False
    checks = {value["id"]: value for value in quality["checks"]}
    assert checks["knowledge-note-quality"] == {
        "id": "knowledge-note-quality",
        "status": "skipped",
        "reason": "semantic-not-evaluated",
    }
    assert checks["source-coverage"]["status"] == "fail"


def test_semantically_verified_document_is_publishable_and_auditable(
    tmp_path: Path,
) -> None:
    workspace = _candidate_workspace(tmp_path)
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
        created_at="2026-07-31T00:00:00.000Z",
        source_canonical_identity="sha256:" + "f" * 64,
        location=gateway.candidate_location(
            workspace,
            local_instance_id="document-semantic-quality",
            nonce="fixture",
        ),
    )
    quality = json.loads(
        (
            candidate.candidate.absolute_path
            / "quality"
            / f"{candidate.quality_report_artifact_id}.json"
        ).read_text(encoding="utf-8")
    )
    knowledge_map = json.loads(
        (
            candidate.candidate.absolute_path
            / "evidence"
            / "knowledge-map.json"
        ).read_text(encoding="utf-8")
    )

    assert gateway.validate_candidate(
        workspace,
        candidate.candidate.staging_relative_path,
    ).valid
    assert candidate.quality_overall == "pass"
    assert candidate.publish_eligible is True
    checks = {value["id"]: value["status"] for value in quality["checks"]}
    assert checks["knowledge-note-quality"] == "pass"
    assert checks["source-coverage"] == "pass"
    assert quality["method"] == {"kind": "model"}
    assert knowledge_map["semantic_verification"]["model_identity"] == (
        "fixture/verifier-v1"
    )
    assert all(
        claim["status"] == "supported"
        for claim in knowledge_map["semantic_verification"]["claims"]
    )

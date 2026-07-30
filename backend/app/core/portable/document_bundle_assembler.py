from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from uuid import UUID

from app.core.domain.document import ParsedDocument
from app.core.domain.ids import new_typed_id, sha256_digest
from app.core.errors import DomainError
from app.core.portable.bundle_assembler import CandidateBundle
from app.core.portable.jsonio import encode_json, encode_ndjson
from app.core.portable.markdown_safety import validate_markdown_safety
from app.core.ports.portable import CandidateLocationCapabilityPort


@dataclass(frozen=True, slots=True)
class DocumentCandidate:
    candidate: CandidateBundle
    run_id: str
    source_id: str
    source_revision_id: str
    source_metadata_artifact_id: str
    primary_draft_artifact_id: str
    normalized_artifact_id: str
    evidence_set_artifact_id: str
    quality_report_artifact_id: str
    quality_overall: str
    publish_eligible: bool


@dataclass(frozen=True, slots=True)
class _Payload:
    artifact_id: str
    artifact_type: str
    path: str
    media_type: str
    data: bytes
    charset: str = "utf-8"


_MARKDOWN_PUNCTUATION = re.compile(r"([\\`*_\[\]{}()<>#+!|&])")
_DANGEROUS_LITERAL_SCHEME = re.compile(
    r"(javascript|vbscript|data|file)(\s*):",
    re.IGNORECASE,
)
_GFM_AUTOLINK_TOKEN = re.compile(
    r"https?://[^\s<]+|(?<![A-Za-z0-9])www\.[^\s<]+|[^\s<>@]+@[^\s<>@]+",
    re.IGNORECASE,
)
_TABLE_PIPE_ENTITY = re.compile(r"&#(?:124|x0*7c);", re.IGNORECASE)
_TABLE_SEPARATOR = re.compile(
    r"^\s*\|?(?:\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?\s*$"
)


def _markdown_literal(text: str) -> str:
    escaped_lines: list[str] = []
    for line in text.split("\n"):
        parts: list[str] = []
        cursor = 0
        for match in _GFM_AUTOLINK_TOKEN.finditer(line):
            literal = _MARKDOWN_PUNCTUATION.sub(
                r"\\\1",
                line[cursor : match.start()],
            )
            parts.append(_DANGEROUS_LITERAL_SCHEME.sub(r"\1\2\\:", literal))
            parts.append(_code_span(match.group()))
            cursor = match.end()
        literal = _MARKDOWN_PUNCTUATION.sub(r"\\\1", line[cursor:])
        parts.append(_DANGEROUS_LITERAL_SCHEME.sub(r"\1\2\\:", literal))
        escaped = "".join(parts)
        escaped = re.sub(r"^(\s{0,3})-(?=\s)", r"\1\\-", escaped)
        escaped = re.sub(r"^(\s{0,3})(\d+)\.(?=\s)", r"\1\2\\.", escaped)
        escaped = re.sub(r"^(\s{0,3})(?=[=-]+\s*$)", r"\1\\", escaped)
        escaped_lines.append(escaped)
    return "\n".join(escaped_lines)


def _table_cell_markdown_literal(text: str) -> str:
    parts: list[str] = []
    cursor = 0
    for match in _TABLE_PIPE_ENTITY.finditer(text):
        parts.append(_markdown_literal(text[cursor : match.start()]))
        parts.append(match.group())
        cursor = match.end()
    parts.append(_markdown_literal(text[cursor:]))
    return "".join(parts)


def _table_markdown_literal(text: str) -> str:
    lines: list[str] = []
    for line in text.split("\n"):
        if _TABLE_SEPARATOR.fullmatch(line):
            lines.append(line)
        elif line.strip().startswith("|") and line.strip().endswith("|"):
            lines.append(
                "|".join(
                    _table_cell_markdown_literal(cell)
                    for cell in line.split("|")
                )
            )
        else:
            lines.append(_markdown_literal(line))
    return "\n".join(lines)


def _longest_backtick_run(text: str) -> int:
    return max((len(match.group()) for match in re.finditer(r"`+", text)), default=0)


def _code_span(text: str) -> str:
    delimiter = "`" * max(1, _longest_backtick_run(text) + 1)
    return f"{delimiter} {text} {delimiter}"


def _code_block(text: str) -> str:
    fence = "`" * max(3, _longest_backtick_run(text) + 1)
    return f"{fence}text\n{text}\n{fence}"


def _markdown_is_safe(markdown: str) -> bool:
    try:
        validate_markdown_safety(
            markdown,
            bundle_relative_path="drafts/document.md",
        )
    except DomainError:
        return False
    return True


def _render_inline_text(text: str, *, prefix: str, suffix: str = "") -> str:
    preferred = f"{prefix}{_markdown_literal(text)}{suffix}"
    if _markdown_is_safe(preferred):
        return preferred
    return f"{prefix}{_code_span(text)}{suffix}"


def _render_document_block(kind: str, text: str) -> str:
    if kind == "section_header":
        return _render_inline_text(text, prefix="## ")
    if kind == "caption":
        return _render_inline_text(text, prefix="*", suffix="*")
    preferred = (
        _table_markdown_literal(text)
        if kind == "table"
        else _markdown_literal(text)
    )
    return preferred if _markdown_is_safe(preferred) else _code_block(text)


class DocumentBundleAssembler:
    @staticmethod
    def _derived_id(job_id: str, prefix: str, role: str) -> str:
        value = UUID(job_id.removeprefix("job_"))
        now_ms = int(value.hex[:12], 16)
        randomness = bytes.fromhex(sha256_digest(f"{job_id}:{role}")[7:])[:10]
        return new_typed_id(prefix, now_ms=now_ms, randomness=randomness)

    def assemble(
        self,
        parsed: ParsedDocument,
        *,
        job_id: str,
        created_at: str,
        location: CandidateLocationCapabilityPort,
        source_id: str | None = None,
    ) -> DocumentCandidate:
        ids = {
            role: self._derived_id(
                job_id,
                "art" if role in {"metadata", "normalized", "evidence", "draft", "quality"} else prefix,
                role,
            )
            for role, prefix in (
                ("run", "run"),
                ("bundle", "bnd"),
                ("source", "src"),
                ("revision", "rev"),
                ("metadata", "art"),
                ("normalized", "art"),
                ("evidence", "art"),
                ("draft", "art"),
                ("quality", "art"),
            )
        }
        if source_id is not None:
            ids["source"] = source_id
        blocks = tuple(block for page in parsed.pages for block in page.blocks)
        pages = {page.page_number: page for page in parsed.pages}
        evidence_ids = {
            block.block_id: self._derived_id(job_id, "ev", f"evidence:{block.block_id}")
            for block in blocks
        }
        normalized = encode_ndjson(
            [
                {
                    "record_type": "document_normalized_header",
                    "schema_version": 1,
                    "source_sha256": parsed.source_sha256,
                    "page_count": len(parsed.pages),
                },
                *(
                    {
                        "record_type": "block",
                        "block_id": block.block_id,
                        "page_number": block.page_number,
                        "reading_order": block.reading_order,
                        "kind": block.kind,
                        "text": block.text,
                        "content_sha256": block.content_sha256,
                        "bbox": {
                            "left": block.bbox.left,
                            "top": block.bbox.top,
                            "right": block.bbox.right,
                            "bottom": block.bbox.bottom,
                            "origin": block.bbox.origin,
                        },
                        "basis": block.basis,
                    }
                    for block in blocks
                ),
            ]
        )
        normalized_ref = {
            "bundle_id": ids["bundle"],
            "artifact_id": ids["normalized"],
            "sha256": sha256_digest(normalized),
        }
        evidence = encode_ndjson(
            [
                {
                    "record_type": "evidence_set_header",
                    "evidence_set_schema_version": 1,
                    "bundle_id": ids["bundle"],
                    "record_count": len(blocks),
                },
                *(
                    {
                        "evidence_ref_schema_version": 1,
                        "evidence_id": evidence_ids[block.block_id],
                        "source_revision_ref": {
                            "bundle_id": ids["bundle"],
                            "source_revision_id": ids["revision"],
                        },
                        "target_artifact_ref": normalized_ref,
                        "locator": {
                            "scheme": "document-page.v1",
                            "page": block.page_number,
                            "bbox": {
                                "left": block.bbox.left / pages[block.page_number].width,
                                "top": block.bbox.top / pages[block.page_number].height,
                                "right": block.bbox.right / pages[block.page_number].width,
                                "bottom": block.bbox.bottom / pages[block.page_number].height,
                            },
                        },
                        "excerpt_sha256": block.content_sha256,
                        "extensions": {"basis": block.basis},
                    }
                    for block in blocks
                ),
            ]
        )
        title_block = next(
            (block for block in blocks if block.kind in {"title", "section_header"}),
            None,
        )
        title = title_block.text if title_block is not None else parsed.source_name
        draft_lines = [_render_inline_text(title, prefix="# "), ""]
        for block in blocks:
            if block is title_block:
                continue
            draft_lines.extend((_render_document_block(block.kind, block.text), ""))
        draft_text = "\n".join(draft_lines) + "\n"
        try:
            validate_markdown_safety(
                draft_text,
                bundle_relative_path=f"drafts/{ids['draft']}.md",
            )
            markdown_safe = True
        except DomainError:
            markdown_safe = False
            draft_text = (
                "# Document output blocked\n\n"
                "The extracted content could not be rendered safely.\n"
            )
        draft = draft_text.encode("utf-8")
        has_native_text = bool(blocks)
        pages_have_content = all(page.blocks for page in parsed.pages)
        parser_status = parsed.metadata.get("status", "").strip().casefold()
        parser_complete = (
            parser_status in {"", "success"} and not parsed.warnings
        )
        publish_eligible = (
            markdown_safe
            and has_native_text
            and pages_have_content
            and parser_complete
        )
        quality_overall = "pass" if publish_eligible else "fail"
        quality_messages = list(parsed.warnings)
        if not markdown_safe:
            quality_messages.append("markdown-safety")
        if not has_native_text:
            quality_messages.append("empty-document")
        if not pages_have_content:
            quality_messages.append("empty-page")
        if not parser_complete:
            quality_messages.append("partial-extraction")
        quality = encode_json(
            {
                "quality_report_schema_version": 1,
                "subject": {
                    "bundle_id": ids["bundle"],
                    "artifact_id": ids["draft"],
                    "sha256": sha256_digest(draft),
                },
                "profile": {"id": "alltonote.document-note", "version": 1},
                "overall": quality_overall,
                "checks": [
                    {
                        "id": "native-text",
                        "status": "pass" if has_native_text else "fail",
                    },
                    {
                        "id": "page-coverage",
                        "status": "pass" if pages_have_content else "fail",
                    },
                    {
                        "id": "parser-completeness",
                        "status": "pass" if parser_complete else "fail",
                    },
                    {"id": "page-bbox", "status": "pass"},
                    {"id": "source-hash", "status": "pass"},
                    {
                        "id": "markdown-safety",
                        "status": "pass" if markdown_safe else "fail",
                    },
                ],
                "method": {"kind": "deterministic"},
                "metrics": {"quality_repair_attempts": 0},
                "messages": quality_messages,
                "evidence_ids": list(evidence_ids.values()),
            }
        )
        metadata = encode_json(
            {
                "source_metadata_schema_version": 1,
                "source_kind": "document",
                "source_id": ids["source"],
                "source_revision_id": ids["revision"],
                "file_name": parsed.source_name,
                "source_sha256": parsed.source_sha256,
                "page_count": len(parsed.pages),
                "parser": {
                    "id": parsed.parser_id,
                    "version": parsed.parser_version,
                    "model_revision": parsed.model_revision,
                },
                "extensions": {},
            }
        )
        payloads = tuple(
            sorted(
                (
                    _Payload(ids["metadata"], "source.metadata.v1", "sources/document-metadata.json", "application/json", metadata),
                    _Payload(ids["normalized"], "document.normalized-content.v1", "evidence/document-content.jsonl", "application/x-ndjson", normalized),
                    _Payload(ids["evidence"], "evidence.reference-set.v1", "evidence/evidence-set.jsonl", "application/x-ndjson", evidence),
                    _Payload(ids["draft"], "knowledge.draft.markdown.v1", f"drafts/{ids['draft']}.md", "text/markdown", draft),
                    _Payload(ids["quality"], "quality.report.v1", f"quality/{ids['quality']}.json", "application/json", quality),
                ),
                key=lambda item: item.path,
            )
        )
        source_revision_ref = {
            "bundle_id": ids["bundle"],
            "source_revision_id": ids["revision"],
        }
        refs = {
            payload.artifact_id: {
                "bundle_id": ids["bundle"],
                "artifact_id": payload.artifact_id,
                "sha256": sha256_digest(payload.data),
            }
            for payload in payloads
        }
        parents = {
            ids["evidence"]: [refs[ids["normalized"]]],
            ids["draft"]: [refs[ids["normalized"]], refs[ids["evidence"]]],
            ids["quality"]: [refs[ids["draft"]]],
        }
        artifact_documents = [
            {
                "artifact_schema_version": 1,
                "artifact_id": payload.artifact_id,
                "artifact_type": payload.artifact_type,
                "payload": {
                    "representation": "bundle_file",
                    "path": payload.path,
                    "media_type": payload.media_type,
                    "charset": payload.charset,
                    "byte_length": len(payload.data),
                    "sha256": sha256_digest(payload.data),
                },
                "created_at": created_at,
                "parents": parents.get(payload.artifact_id, []),
                "source_revision_refs": [source_revision_ref],
                "generated_by": {"run_id": ids["run"]},
                "generation": {
                    "recipe": {"id": "alltonote.document-note", "version": 1},
                    "capability": "alltonote.document-source-bundle@1.0.0",
                },
                "quality_report_refs": (
                    [refs[ids["quality"]]] if payload.artifact_id == ids["draft"] else []
                ),
                "extensions": {},
            }
            for payload in payloads
        ]
        source_document = {
            "source_schema_version": 1,
            "source_id": ids["source"],
            "source_kind": "document",
            "canonical_identity": {"scheme": "sha256", "value": parsed.source_sha256[7:]},
            "display": {"title": title},
            "extensions": {},
        }
        revision_document = {
            "source_revision_schema_version": 1,
            "source_revision_id": ids["revision"],
            "source_ref": {"bundle_id": ids["bundle"], "source_id": ids["source"]},
            "captured_at": created_at,
            "observed_revision": {"sha256": parsed.source_sha256},
            "content_digest": parsed.source_sha256,
            "materialization": {"kind": "reference_only", "reason_code": "user-local-file"},
            "license": {"status": "unknown", "archive_permission": "unknown"},
            "privacy": "personal",
            "freshness": {"kind": "snapshot", "observed_at": created_at},
            "extensions": {},
        }
        receipt = encode_json(
            {
                "receipt_schema_version": 1,
                "run_id": ids["run"],
                "state": "succeeded",
                "started_at": created_at,
                "completed_at": created_at,
                "recipe": {"id": "alltonote.document-note", "version": 1},
                "parameters": {"sha256": sha256_digest("document-first-slice-v1"), "summary": {"job_id": job_id}},
                "inputs": [source_revision_ref],
                "outputs": list(refs.values()),
                "capabilities": ["alltonote.document-source-bundle@1.0.0"],
                "executors": [
                    {"kind": "alltonote-runtime", "product": "alltonote", "version": "0.1.0", "portable_contract_id": "iwiki-portable-contract-v1"},
                    {"kind": "document-parser", "identity": f"docling/{parsed.parser_version}@{parsed.model_revision}"},
                ],
                "usage": {"pages": len(parsed.pages), "blocks": len(blocks)},
                "quality": {
                    "overall": quality_overall,
                    "publish_eligible": publish_eligible,
                    "repair_attempts": 0,
                },
                "redactions": {"source_path": "omitted"},
            }
        )
        manifest = encode_json(
            {
                "$schema": "urn:iwiki:portable:bundle:v1",
                "bundle_schema_version": 1,
                "bundle_id": ids["bundle"],
                "created_at": created_at,
                "producer": {
                    "product": "alltonote",
                    "runtime_version": "0.1.0",
                    "recipe": {"id": "alltonote.document-note", "version": 1},
                    "capability": "alltonote.document-source-bundle@1.0.0",
                    "portable_contract_id": "iwiki-portable-contract-v1",
                },
                "sources": [source_document],
                "source_revisions": [revision_document],
                "dependencies": [],
                "artifacts": artifact_documents,
                "outputs": {
                    "primary_draft": ids["draft"],
                    "evidence_set": ids["evidence"],
                    "quality_reports": [ids["quality"]],
                    "source_snapshots": [ids["metadata"]],
                    "display_assets": [],
                },
                "receipt": {"path": "receipt.json", "byte_length": len(receipt), "sha256": sha256_digest(receipt)},
                "required_contracts": [],
                "extensions": {},
            }
        )
        writer = location.begin(job_id)
        try:
            for payload in payloads:
                writer.write_payload(payload.path, payload.data)
            writer.write_payload("receipt.json", receipt)
            candidate_location = writer.complete(manifest)
        except BaseException:
            try:
                writer.close()
            except BaseException:
                pass
            raise
        writer.close()
        return DocumentCandidate(
            candidate=CandidateBundle(
                candidate_location,
                ids["bundle"],
                sha256_digest(manifest),
                tuple(payload.artifact_id for payload in payloads),
            ),
            run_id=ids["run"],
            source_id=ids["source"],
            source_revision_id=ids["revision"],
            source_metadata_artifact_id=ids["metadata"],
            primary_draft_artifact_id=ids["draft"],
            normalized_artifact_id=ids["normalized"],
            evidence_set_artifact_id=ids["evidence"],
            quality_report_artifact_id=ids["quality"],
            quality_overall=quality_overall,
            publish_eligible=publish_eligible,
        )


__all__ = ["DocumentBundleAssembler", "DocumentCandidate"]

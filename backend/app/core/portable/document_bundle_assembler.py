from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from uuid import UUID

from app.core.application.document_knowledge_compiler import (
    CompiledDocumentKnowledgeNoteV1,
)
from app.core.application.document_knowledge_verifier import (
    DocumentKnowledgeVerificationV1,
    compiled_document_knowledge_sha256,
    document_knowledge_evidence_sha256,
)
from app.core.domain.document import ParsedDocument
from app.core.domain.ids import new_typed_id, sha256_digest
from app.core.errors import DomainError, ErrorCategory
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
    knowledge_map_artifact_id: str | None
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


def _compiled_projection(
    compiled: CompiledDocumentKnowledgeNoteV1,
) -> tuple[str, tuple[dict[str, object], ...]]:
    lines = [_render_inline_text(compiled.title.text, prefix="# "), ""]
    items: list[dict[str, object]] = [
        {
            "note_item_id": "title-0001",
            "source_block_ids": list(compiled.title.source_block_ids),
        }
    ]

    for index, claim in enumerate(compiled.overview, start=1):
        lines.extend((_markdown_literal(claim.text), ""))
        items.append(
            {
                "note_item_id": f"overview-{index:04d}",
                "source_block_ids": list(claim.source_block_ids),
            }
        )
    for section_index, section in enumerate(compiled.sections, start=1):
        lines.extend((_render_inline_text(section.heading.text, prefix="## "), ""))
        items.append(
            {
                "note_item_id": f"section-{section_index:04d}-heading-0001",
                "source_block_ids": list(section.heading.source_block_ids),
            }
        )
        for paragraph_index, claim in enumerate(section.paragraphs, start=1):
            lines.extend((_markdown_literal(claim.text), ""))
            items.append(
                {
                    "note_item_id": (
                        f"section-{section_index:04d}-paragraph-{paragraph_index:04d}"
                    ),
                    "source_block_ids": list(claim.source_block_ids),
                }
            )
        for point_index, claim in enumerate(section.key_points, start=1):
            lines.extend((f"- {_markdown_literal(claim.text)}", ""))
            items.append(
                {
                    "note_item_id": (
                        f"section-{section_index:04d}-key-point-{point_index:04d}"
                    ),
                    "source_block_ids": list(claim.source_block_ids),
                }
            )
    return "\n".join(lines).rstrip() + "\n", tuple(items)


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
        compiled: CompiledDocumentKnowledgeNoteV1 | None = None,
        verification: DocumentKnowledgeVerificationV1 | None = None,
        job_id: str,
        created_at: str,
        location: CandidateLocationCapabilityPort,
        source_canonical_identity: str,
        source_id: str | None = None,
        quality_repair_attempts: int = 0,
    ) -> DocumentCandidate:
        if (
            type(quality_repair_attempts) is not int
            or not 0 <= quality_repair_attempts <= 1
        ):
            raise DomainError(
                "document_quality_repair_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Document quality repair count is invalid",
            )
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
                ("knowledge_map", "art"),
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
        knowledge_map: bytes | None = None
        if compiled is None:
            if verification is not None:
                raise DomainError(
                    "document_knowledge_verification_invalid",
                    ErrorCategory.RECIPE_FAILED,
                    "Document semantic verification does not match the knowledge note",
                )
            draft_lines = [_render_inline_text(title, prefix="# "), ""]
            for block in blocks:
                if block is title_block:
                    continue
                draft_lines.extend((_render_document_block(block.kind, block.text), ""))
            draft_text = "\n".join(draft_lines) + "\n"
        else:
            draft_text, knowledge_items = _compiled_projection(compiled)
            if verification is not None and (
                verification.compiled_sha256
                != compiled_document_knowledge_sha256(compiled)
                or verification.evidence_input_sha256
                != document_knowledge_evidence_sha256(parsed, compiled)
                or tuple(claim.claim_id for claim in verification.claims)
                != tuple(item["note_item_id"] for item in knowledge_items)
            ):
                raise DomainError(
                    "document_knowledge_verification_invalid",
                    ErrorCategory.RECIPE_FAILED,
                    "Document semantic verification does not match the knowledge note",
                )
            knowledge_map = encode_json(
                {
                    "knowledge_map_schema_version": 1,
                    "model_identity": compiled.model_identity,
                    "referenced_block_ids": list(compiled.referenced_block_ids),
                    "items": list(knowledge_items),
                    **(
                        {
                            "semantic_verification": {
                                "model_identity": verification.model_identity,
                                "claims": [
                                    {
                                        "claim_id": claim.claim_id,
                                        "status": claim.status,
                                    }
                                    for claim in verification.claims
                                ],
                            }
                        }
                        if verification is not None
                        else {}
                    ),
                }
            )
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
        extraction_passed = (
            markdown_safe
            and has_native_text
            and pages_have_content
            and parser_complete
        )
        referenced_ids = (
            frozenset(compiled.referenced_block_ids)
            if compiled is not None
            else frozenset()
        )
        substantive_blocks = tuple(
            block
            for block in blocks
            if block.kind not in {"title", "section_header"}
        ) or blocks
        substantive_bytes = sum(
            len(block.text.encode("utf-8")) for block in substantive_blocks
        )
        referenced_bytes = sum(
            len(block.text.encode("utf-8"))
            for block in substantive_blocks
            if block.block_id in referenced_ids
        )
        substantive_pages = {
            block.page_number for block in substantive_blocks
        }
        referenced_pages = {
            block.page_number
            for block in substantive_blocks
            if block.block_id in referenced_ids
        }
        source_coverage_ratio = (
            referenced_bytes / substantive_bytes if substantive_bytes else 0.0
        )
        page_reference_coverage_ratio = (
            len(referenced_pages) / len(substantive_pages)
            if substantive_pages
            else 0.0
        )
        source_coverage_passed = (
            compiled is not None
            and source_coverage_ratio >= 0.35
            and page_reference_coverage_ratio >= 0.60
        )
        semantic_quality_passed = (
            verification is not None
            and verification.passed
            and verification.model_identity != compiled.model_identity
        )
        quality_overall = (
            "pass"
            if compiled is not None
            and extraction_passed
            and source_coverage_passed
            and semantic_quality_passed
            else (
                "fail"
                if compiled is not None
                else ("pass" if extraction_passed else "fail")
            )
        )
        publish_eligible = compiled is not None and quality_overall == "pass"
        quality_messages = list(parsed.warnings)
        if not markdown_safe:
            quality_messages.append("markdown-safety")
        if not has_native_text:
            quality_messages.append("empty-document")
        if not pages_have_content:
            quality_messages.append("empty-page")
        if not parser_complete:
            quality_messages.append("partial-extraction")
        if compiled is None:
            quality_messages.append("knowledge-note-quality-not-evaluated")
        elif verification is None:
            quality_messages.append("knowledge-note-semantic-quality-not-evaluated")
        elif verification.model_identity == compiled.model_identity:
            quality_messages.append("knowledge-note-same-model-review-not-independent")
        elif not semantic_quality_passed:
            quality_messages.append("knowledge-note-semantic-quality")
        if compiled is not None and not source_coverage_passed:
            quality_messages.append("knowledge-note-source-coverage")
        checks: list[dict[str, object]] = [
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
        ]
        if compiled is None:
            checks.append(
                {
                    "id": "knowledge-note-quality",
                    "status": "skipped",
                    "reason": "not-evaluated",
                }
            )
            quality_profile = "alltonote.document-native-extraction"
            metrics: dict[str, object] = {
                "quality_repair_attempts": quality_repair_attempts
            }
        else:
            knowledge_quality_check: dict[str, object]
            if verification is None:
                knowledge_quality_check = {
                    "id": "knowledge-note-quality",
                    "status": "skipped",
                    "reason": "semantic-not-evaluated",
                }
            else:
                knowledge_quality_check = (
                    {
                        "id": "knowledge-note-quality",
                        "status": "skipped",
                        "reason": "same-model-review-not-independent",
                    }
                    if verification.model_identity == compiled.model_identity
                    else {
                        "id": "knowledge-note-quality",
                        "status": "pass" if verification.passed else "fail",
                    }
                )
            checks.extend(
                (
                    knowledge_quality_check,
                    {
                        "id": "source-coverage",
                        "status": "pass" if source_coverage_passed else "fail",
                    },
                )
            )
            quality_profile = "alltonote.document-knowledge-note"
            metrics = {
                "quality_repair_attempts": quality_repair_attempts,
                "referenced_block_count": len(referenced_ids),
                "substantive_block_count": len(substantive_blocks),
                "source_coverage_ratio": source_coverage_ratio,
                "page_reference_coverage_ratio": page_reference_coverage_ratio,
                "input_tokens": compiled.input_tokens,
                "output_tokens": compiled.output_tokens,
                "token_counts_complete": compiled.token_counts_complete,
            }
            if verification is not None:
                metrics.update(
                    {
                        "semantic_verification_input_tokens": verification.input_tokens,
                        "semantic_verification_output_tokens": verification.output_tokens,
                        "semantic_verification_token_counts_complete": (
                            verification.token_counts_complete
                        ),
                        "semantic_verifier_model_identity": verification.model_identity,
                    }
                )
        quality = encode_json(
            {
                "quality_report_schema_version": 1,
                "subject": {
                    "bundle_id": ids["bundle"],
                    "artifact_id": ids["draft"],
                    "sha256": sha256_digest(draft),
                },
                "profile": {
                    "id": quality_profile,
                    "version": 1,
                },
                "overall": quality_overall,
                "checks": checks,
                "method": {
                    "kind": "model" if verification is not None else "deterministic"
                },
                "metrics": metrics,
                "messages": quality_messages,
                "evidence_ids": [
                    evidence_ids[block.block_id]
                    for block in blocks
                    if compiled is None or block.block_id in referenced_ids
                ],
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
        payload_values = [
            _Payload(ids["metadata"], "source.metadata.v1", "sources/document-metadata.json", "application/json", metadata),
            _Payload(ids["normalized"], "document.normalized-content.v1", "evidence/document-content.jsonl", "application/x-ndjson", normalized),
            _Payload(ids["evidence"], "evidence.reference-set.v1", "evidence/evidence-set.jsonl", "application/x-ndjson", evidence),
            _Payload(ids["draft"], "knowledge.draft.markdown.v1", f"drafts/{ids['draft']}.md", "text/markdown", draft),
            _Payload(ids["quality"], "quality.report.v1", f"quality/{ids['quality']}.json", "application/json", quality),
        ]
        if knowledge_map is not None:
            payload_values.append(
                _Payload(
                    ids["knowledge_map"],
                    "document.knowledge-map.v1",
                    "evidence/knowledge-map.json",
                    "application/json",
                    knowledge_map,
                )
            )
        payloads = tuple(sorted(payload_values, key=lambda item: item.path))
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
            ids["draft"]: [
                refs[ids["normalized"]],
                refs[ids["evidence"]],
                *(
                    [refs[ids["knowledge_map"]]]
                    if knowledge_map is not None
                    else []
                ),
            ],
            ids["quality"]: [
                refs[ids["draft"]],
                *(
                    [refs[ids["knowledge_map"]]]
                    if knowledge_map is not None
                    else []
                ),
            ],
        }
        if knowledge_map is not None:
            parents[ids["knowledge_map"]] = [
                refs[ids["normalized"]],
                refs[ids["evidence"]],
            ]
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
        external_ref_id = f"ext_{ids['source'].removeprefix('src_')}"
        source_document = {
            "source_schema_version": 1,
            "source_id": ids["source"],
            "source_kind": "document",
            "canonical_identity": {
                "scheme": "local-document-path-v1",
                "value": source_canonical_identity,
            },
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
            "materialization": {
                "kind": "external_local",
                "external_ref_id": external_ref_id,
            },
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
                "parameters": {
                    "sha256": sha256_digest(
                        "document-knowledge-note-v2"
                        if compiled is not None
                        else "document-first-slice-v1"
                    ),
                    "summary": {"job_id": job_id},
                },
                "inputs": [source_revision_ref],
                "outputs": list(refs.values()),
                "capabilities": ["alltonote.document-source-bundle@1.0.0"],
                "executors": [
                    {"kind": "alltonote-runtime", "product": "alltonote", "version": "0.1.0", "portable_contract_id": "iwiki-portable-contract-v1"},
                    {"kind": "document-parser", "identity": f"docling/{parsed.parser_version}@{parsed.model_revision}"},
                    *(
                        [
                            {
                                "kind": "model",
                                "identity": compiled.model_identity,
                            }
                        ]
                        if compiled is not None
                        else []
                    ),
                    *(
                        [
                            {
                                "kind": "model",
                                "identity": verification.model_identity,
                            }
                        ]
                        if verification is not None
                        else []
                    ),
                ],
                "usage": {
                    "pages": len(parsed.pages),
                    "blocks": len(blocks),
                    **(
                        {
                            "input_tokens": compiled.input_tokens,
                            "output_tokens": compiled.output_tokens,
                        }
                        if compiled is not None
                        else {}
                    ),
                },
                "quality": {
                    "overall": quality_overall,
                    "publish_eligible": publish_eligible,
                    "repair_attempts": quality_repair_attempts,
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
            knowledge_map_artifact_id=(
                ids["knowledge_map"] if knowledge_map is not None else None
            ),
            quality_report_artifact_id=ids["quality"],
            quality_overall=quality_overall,
            publish_eligible=publish_eligible,
        )


__all__ = ["DocumentBundleAssembler", "DocumentCandidate"]

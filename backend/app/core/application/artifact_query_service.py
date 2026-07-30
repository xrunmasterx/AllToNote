from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

from app.core.errors import DomainError, ErrorCategory
from app.core.ports.portable_queries import (
    PortableArtifactQueryRecord,
    PortableInspectionPort,
    PortableInspectionRecord,
)
from app.core.portable.markdown_safety import markdown_visible_mask


_ARTIFACT_ID = re.compile(
    r"art_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_BUNDLE_ID = re.compile(
    r"bnd_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_EVIDENCE_REF = re.compile(
    r"\[\^(ev_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12})\]"
)
_EVIDENCE_DEFINITION = re.compile(
    r"^ {0,3}\[\^(ev_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12})\]:(?:[ \t]|$)"
)
_ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)\s*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_MAX_BODY_BYTES = 256 * 1024
_MAX_DRAFT_ANALYSIS_BYTES = 8 * 1024 * 1024
_MAX_HEADINGS = 100
_MAX_HEADING_TEXT = 200
_MAX_EVIDENCE_IDS = 100


class ArtifactQueryService:
    def __init__(self, portable: PortableInspectionPort | None = None) -> None:
        if portable is None:
            from app.adapters.iwiki.portable_gateway import IWikiPortableGateway

            portable = IWikiPortableGateway()
        self._portable = portable

    def inspect_artifact(
        self,
        workspace_root: Path,
        target_id: str,
        *,
        body_bytes: int | None = None,
    ) -> Mapping[str, object]:
        _require_body_limit(body_bytes)
        if (
            type(target_id) is not str
            or (
                _ARTIFACT_ID.fullmatch(target_id) is None
                and _BUNDLE_ID.fullmatch(target_id) is None
            )
        ):
            raise DomainError(
                "artifact_target_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Artifact inspection requires an artifact or bundle ID",
            )
        if body_bytes is not None and _BUNDLE_ID.fullmatch(target_id) is not None:
            raise DomainError(
                "artifact_body_target_invalid",
                ErrorCategory.INVALID_REQUEST,
                "A body preview requires an artifact ID",
            )
        try:
            inspected = self._portable.inspect_committed(
                workspace_root,
                target_id,
                payload_limit=body_bytes,
            )
        except DomainError as error:
            raise _public_query_error(error, subject="artifact", target_id=target_id)
        data = _common_projection(inspected)
        if _BUNDLE_ID.fullmatch(target_id) is not None:
            data.update(
                {
                    "target_kind": "bundle",
                    "artifacts": [
                        _artifact_projection(artifact)
                        for artifact in inspected.artifacts
                    ],
                }
            )
            return data

        artifact = inspected.target_artifact
        if artifact is None:
            raise DomainError(
                "artifact_not_found",
                ErrorCategory.INVALID_REQUEST,
                "The requested artifact was not found",
            )
        data.update(
            {
                "target_kind": "artifact",
                "artifact": _artifact_projection(artifact),
            }
        )
        if body_bytes is not None:
            data.update(_body_projection(inspected, body_bytes))
        return data

    def inspect_draft(
        self,
        workspace_root: Path,
        draft_id: str,
        *,
        body_bytes: int | None = None,
        presentation: str = "audit",
    ) -> Mapping[str, object]:
        _require_body_limit(body_bytes)
        _require_presentation(presentation)
        if type(draft_id) is not str or _ARTIFACT_ID.fullmatch(draft_id) is None:
            raise DomainError(
                "draft_target_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Draft inspection requires a draft artifact ID",
            )
        try:
            inspected = self._portable.inspect_committed(
                workspace_root,
                draft_id,
                payload_limit=_MAX_DRAFT_ANALYSIS_BYTES,
            )
        except DomainError as error:
            raise _public_query_error(error, subject="draft", target_id=draft_id)
        draft = inspected.target_artifact
        if (
            draft is None
            or not draft.kind.startswith("knowledge.draft.")
            or draft.media_type != "text/markdown"
            or draft.document_kind is None
        ):
            raise DomainError(
                "draft_not_found",
                ErrorCategory.INVALID_REQUEST,
                "The requested draft was not found",
            )
        if inspected.payload is None or inspected.payload_truncated:
            raise DomainError(
                "draft_inspection_limit_exceeded",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "The draft exceeds the bounded inspection limit",
            )
        text = _decode_text(inspected.payload, draft, subject="draft")
        headings, heading_count, evidence_ids = _draft_summary(text)
        draft_projection = _artifact_projection(draft)
        draft_projection.update(
            {
                "heading_count": heading_count,
                "headings": list(headings),
                "headings_truncated": heading_count > len(headings),
                "evidence_reference_count": len(evidence_ids),
                "evidence_ids": list(evidence_ids[:_MAX_EVIDENCE_IDS]),
                "evidence_ids_truncated": len(evidence_ids) > _MAX_EVIDENCE_IDS,
            }
        )
        data = _common_projection(inspected)
        data.update({"target_kind": "draft", "draft": draft_projection})
        if body_bytes is not None:
            presented_text = (
                project_reading_markdown(text)
                if presentation == "reading"
                else text
            )
            data.update(_text_body_projection(presented_text, body_bytes))
            data["body_presentation"] = presentation
        return data


def _require_body_limit(body_bytes: int | None) -> None:
    if body_bytes is not None and (
        type(body_bytes) is not int
        or body_bytes <= 0
        or body_bytes > _MAX_BODY_BYTES
    ):
        raise DomainError(
            "artifact_body_limit_invalid",
            ErrorCategory.INVALID_REQUEST,
            "Body preview limit must be between 1 and 262144 bytes",
        )


def _require_presentation(presentation: str) -> None:
    if presentation not in {"audit", "reading"}:
        raise DomainError(
            "draft_presentation_invalid",
            ErrorCategory.INVALID_REQUEST,
            "Draft presentation must be 'audit' or 'reading'",
        )


def project_reading_markdown(markdown: str) -> str:
    """Project an audited draft into human reading Markdown.

    The committed draft remains authoritative. This projection removes only
    rendered system Evidence footnotes; ordinary footnotes and code literals
    remain part of the reading text.
    """

    visible = markdown_visible_mask(markdown)
    projected_lines: list[str] = []
    offset = 0
    changed = False
    fence_character: str | None = None
    fence_length = 0

    for line in markdown.splitlines(keepends=True):
        source_line_length = len(line)
        content = line.rstrip("\r\n")
        line_ending = line[len(content) :]
        fence = _FENCE.match(content)
        if fence is not None:
            marker = fence.group(1)
            if fence_character is None:
                fence_character = marker[0]
                fence_length = len(marker)
            elif marker[0] == fence_character and len(marker) >= fence_length:
                fence_character = None
                fence_length = 0
            projected_lines.append(line)
            offset += len(line)
            continue

        definition = (
            _EVIDENCE_DEFINITION.match(content)
            if fence_character is None
            else None
        )
        if definition is not None:
            changed = True
            offset += source_line_length
            continue

        removals: list[tuple[int, int]] = []
        for match in _EVIDENCE_REF.finditer(content):
            absolute_start = offset + match.start()
            absolute_end = offset + match.end()
            if (
                not all(visible[absolute_start:absolute_end])
                or _backslash_escaped(markdown, absolute_start)
            ):
                continue
            start = match.start()
            end = match.end()
            if start > 0 and content[start - 1] in " \t":
                start -= 1
            removals.append((start, end))

        if removals:
            changed = True
            pieces: list[str] = []
            cursor = 0
            for start, end in removals:
                pieces.append(content[cursor:start])
                cursor = end
            pieces.append(content[cursor:])
            content = "".join(pieces).rstrip(" \t")
            line = content + line_ending
        projected_lines.append(line)
        offset += source_line_length

    if not changed:
        return markdown
    projected = "".join(projected_lines).rstrip(" \t\r\n")
    return f"{projected}\n" if projected else ""


def _backslash_escaped(value: str, index: int) -> bool:
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and value[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _public_query_error(
    error: DomainError,
    *,
    subject: str,
    target_id: str,
) -> DomainError:
    del target_id
    if error.code == "portable_artifact_not_found":
        return DomainError(
            f"{subject}_not_found",
            ErrorCategory.INVALID_REQUEST,
            f"The requested {subject} was not found",
        )
    if error.code == "portable_bundle_not_found":
        return DomainError(
            "bundle_not_found",
            ErrorCategory.INVALID_REQUEST,
            "The requested bundle was not found",
        )
    if error.code == "portable_artifact_stale":
        return DomainError(
            f"{subject}_stale",
            ErrorCategory.CONFLICT,
            f"The requested {subject} no longer matches its committed bundle",
        )
    if error.code == "portable_bundle_stale":
        return DomainError(
            "bundle_stale",
            ErrorCategory.CONFLICT,
            "The requested bundle no longer matches its committed manifest",
        )
    return error


def _common_projection(inspected: PortableInspectionRecord) -> dict[str, object]:
    bundle = inspected.bundle
    return {
        "bundle": {
            "bundle_id": bundle.bundle_id,
            "manifest_sha256": bundle.manifest_sha256,
            "created_at": bundle.created_at,
            "producer": {
                "product": bundle.producer_product,
                "runtime_version": bundle.runtime_version,
                "recipe": {
                    "id": bundle.recipe_id,
                    "version": bundle.recipe_version,
                },
                "capability": bundle.capability,
                "portable_contract_id": bundle.portable_contract_id,
            },
            "artifact_count": bundle.artifact_count,
            "artifacts_truncated": inspected.artifacts_truncated,
            "primary_draft_artifact_id": bundle.primary_draft_artifact_id,
            "draft_artifact_ids": list(bundle.draft_artifact_ids),
        },
        "quality": {
            "overall": inspected.quality.overall,
            "publish_eligible": inspected.quality.publish_eligible,
            "repair_attempts": inspected.quality.repair_attempts,
        },
        "source": {
            "sources": [
                {
                    "source_id": source.source_id,
                    "kind": source.kind,
                    "connector_id": source.connector_id,
                    "platform": source.platform,
                }
                for source in inspected.sources
            ],
            "source_revision_ids": list(inspected.source_revision_ids),
        },
        "evidence": {
            "transcript_artifact_id": bundle.transcript_artifact_id,
            "evidence_set_artifact_id": bundle.evidence_set_artifact_id,
        },
    }


def _artifact_projection(
    artifact: PortableArtifactQueryRecord,
) -> dict[str, object]:
    return {
        "artifact_id": artifact.artifact_id,
        "kind": artifact.kind,
        "media_type": artifact.media_type,
        "charset": artifact.charset,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
        "created_at": artifact.created_at,
        "source_revision_ids": list(artifact.source_revision_ids),
        "parent_artifact_ids": list(artifact.parent_artifact_ids),
        "quality_report_ids": list(artifact.quality_report_ids),
        "recipe": {
            "id": artifact.recipe_id,
            "version": artifact.recipe_version,
        },
        "compiler_identity": artifact.compiler_identity,
        "document_kind": artifact.document_kind,
        "primary": artifact.primary,
    }


def _body_projection(
    inspected: PortableInspectionRecord,
    body_bytes: int,
) -> dict[str, object]:
    artifact = inspected.target_artifact
    if artifact is None or inspected.payload is None:
        raise DomainError(
            "artifact_body_unavailable",
            ErrorCategory.CONFLICT,
            "The requested artifact body is unavailable",
        )
    text = _decode_text(inspected.payload, artifact, subject="artifact")
    projected = _text_body_projection(text, body_bytes)
    projected["body_truncated"] = (
        inspected.payload_truncated or projected["body_truncated"]
    )
    return projected


def _decode_text(
    payload: bytes,
    artifact: PortableArtifactQueryRecord,
    *,
    subject: str,
) -> str:
    media_type = artifact.media_type.casefold()
    charset = artifact.charset.casefold() if artifact.charset is not None else None
    text_media = (
        media_type.startswith("text/")
        or media_type in {
            "application/json",
            "application/jsonl",
            "application/x-ndjson",
        }
    )
    if not text_media or charset not in {None, "utf-8"}:
        raise DomainError(
            f"{subject}_body_not_text",
            ErrorCategory.INVALID_REQUEST,
            f"The requested {subject} body is not UTF-8 text",
        )
    try:
        return payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        if error.end == len(payload) and error.reason == "unexpected end of data":
            return payload[: error.start].decode("utf-8", errors="strict")
        raise DomainError(
            f"{subject}_body_not_text",
            ErrorCategory.CONFLICT,
            f"The requested {subject} body is not valid UTF-8 text",
        ) from None


def _text_body_projection(text: str, body_bytes: int) -> dict[str, object]:
    encoded = text.encode("utf-8")
    prefix = encoded[:body_bytes]
    while prefix:
        try:
            body = prefix.decode("utf-8", errors="strict")
            break
        except UnicodeDecodeError as error:
            if error.end != len(prefix):
                raise
            prefix = prefix[: error.start]
    else:
        body = ""
    return {
        "body": body,
        "body_bytes_limit": body_bytes,
        "body_bytes_returned": len(prefix),
        "body_truncated": len(prefix) < len(encoded),
    }


def _draft_summary(
    text: str,
) -> tuple[tuple[dict[str, object], ...], int, tuple[str, ...]]:
    headings: list[dict[str, object]] = []
    heading_count = 0
    evidence_ids: list[str] = []
    seen_evidence: set[str] = set()
    fence_character: str | None = None
    fence_length = 0
    for line in text.splitlines():
        fence = _FENCE.match(line)
        if fence is not None:
            marker = fence.group(1)
            if fence_character is None:
                fence_character = marker[0]
                fence_length = len(marker)
            elif marker[0] == fence_character and len(marker) >= fence_length:
                fence_character = None
                fence_length = 0
            continue
        if fence_character is not None:
            continue
        heading = _ATX_HEADING.match(line)
        if heading is not None:
            heading_count += 1
            if len(headings) < _MAX_HEADINGS:
                value = re.sub(r"[ \t]+#+[ \t]*$", "", heading.group(2)).strip()
                headings.append(
                    {
                        "level": len(heading.group(1)),
                        "text": value[:_MAX_HEADING_TEXT],
                    }
                )
        for match in _EVIDENCE_REF.finditer(line):
            evidence_id = match.group(1)
            if evidence_id not in seen_evidence:
                seen_evidence.add(evidence_id)
                evidence_ids.append(evidence_id)
    return tuple(headings), heading_count, tuple(evidence_ids)


__all__ = ["ArtifactQueryService", "project_reading_markdown"]

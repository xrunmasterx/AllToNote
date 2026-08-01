from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from app.core.errors import DomainError, ErrorCategory
from app.core.ports.portable_queries import (
    PortableArtifactQueryRecord,
    PortableInspectionPort,
    PortableInspectionRecord,
)


_DRAFT_ID = re.compile(
    r"art_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_EVIDENCE_ID = re.compile(
    r"ev_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_NOTE_ITEM_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,127}\Z")
_MAX_METADATA_BYTES = 256 * 1024
_MAX_QUALITY_BYTES = 512 * 1024
_MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
_MAX_TRANSCRIPT_BYTES = 32 * 1024 * 1024
_MAX_KNOWLEDGE_MAP_BYTES = 8 * 1024 * 1024
_MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
_MAX_RECORDS = 100_000
_MAX_QUALITY_REPORTS = 8
_MAX_CHECKS = 256
_MAX_MESSAGES = 256
_MAX_SOURCE_BLOCKS = 64
_MAX_TEXT = 1_000_000
_SUPPORTED_PUBLICATION_QUALITY_PROFILES = frozenset(
    {
        ("alltonote.video-course-note", 1),
        ("alltonote.video-course-note", 2),
        ("alltonote.video-faithful-edition", 1),
        ("alltonote.document-knowledge-note", 1),
    }
)
_QUALITY_OVERALLS = frozenset({"pass", "pass_with_warnings", "fail"})
_SEMANTIC_STATUSES = frozenset(
    {"supported", "unsupported", "insufficient-evidence", "contradicted"}
)
_SENSITIVE_URL_QUERY_KEYS = frozenset(
    {
        "accesskey",
        "accesskeyid",
        "accesstoken",
        "apikey",
        "auth",
        "authtoken",
        "authorization",
        "awsaccesskeyid",
        "credential",
        "expires",
        "expiration",
        "keypairid",
        "oauth",
        "oauthtoken",
        "password",
        "policy",
        "secret",
        "securitytoken",
        "sig",
        "signature",
        "token",
    }
)


class ReviewCandidateService:
    def __init__(self, portable: PortableInspectionPort | None = None) -> None:
        if portable is None:
            from app.adapters.iwiki.portable_gateway import IWikiPortableGateway

            portable = IWikiPortableGateway()
        self._portable = portable

    def show(
        self,
        workspace_root: Path,
        draft_id: str,
        *,
        evidence_id: str | None = None,
        note_item_id: str | None = None,
    ) -> Mapping[str, object]:
        if type(draft_id) is not str or _DRAFT_ID.fullmatch(draft_id) is None:
            raise _error(
                "review_target_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Review requires a draft artifact ID",
            )
        if evidence_id is not None and (
            type(evidence_id) is not str
            or _EVIDENCE_ID.fullmatch(evidence_id) is None
        ):
            raise _error(
                "review_evidence_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Review evidence ID is invalid",
            )
        if note_item_id is not None and (
            type(note_item_id) is not str
            or _NOTE_ITEM_ID.fullmatch(note_item_id) is None
        ):
            raise _error(
                "review_note_item_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Review note item ID is invalid",
            )
        if evidence_id is not None and note_item_id is not None:
            raise _error(
                "review_focus_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Review accepts only one focused item",
            )

        inspected = self._inspect(workspace_root, draft_id)
        draft = inspected.target_artifact
        if (
            draft is None
            or not draft.kind.startswith("knowledge.draft.")
            or draft.media_type != "text/markdown"
            or draft.document_kind is None
        ):
            raise _error(
                "review_candidate_not_found",
                ErrorCategory.INVALID_REQUEST,
                "The requested Review candidate was not found",
            )
        if inspected.artifacts_truncated:
            raise _limit_error()

        artifacts = {artifact.artifact_id: artifact for artifact in inspected.artifacts}
        metadata = _single_kind(inspected, "source.metadata.v1")
        source = self._source_projection(
            workspace_root,
            inspected,
            draft,
            metadata,
        )
        reports = self._quality_reports(workspace_root, inspected, draft, artifacts)
        document_independently_verified = (
            self._document_independently_verified(
                workspace_root,
                inspected,
                draft,
                reports,
            )
            if any(
                report.get("profile")
                == {"id": "alltonote.document-knowledge-note", "version": 1}
                for report in reports
            )
            else None
        )
        admission = _publication_admission(
            stored_publish_eligible=inspected.quality.publish_eligible,
            reports=reports,
            document_independently_verified=document_independently_verified,
        )
        data: dict[str, object] = {
            "candidate": {
                "draft_id": draft.artifact_id,
                "draft_sha256": draft.sha256,
                "document_kind": draft.document_kind,
                "bundle_id": inspected.bundle.bundle_id,
            },
            "source": source,
            "quality": {
                "overall": inspected.quality.overall,
                "publish_eligible": admission["status"] == "pass",
                "admission": admission,
                "reports": reports,
            },
        }
        if evidence_id is not None:
            data["focus"] = self._evidence_focus(
                workspace_root,
                inspected,
                draft,
                evidence_id,
            )
        elif note_item_id is not None:
            data["focus"] = self._note_item_focus(
                workspace_root,
                inspected,
                draft,
                note_item_id,
            )
        return data

    def _inspect(
        self,
        workspace_root: Path,
        target_id: str,
        *,
        payload_limit: int | None = None,
    ) -> PortableInspectionRecord:
        try:
            return self._portable.inspect_committed(
                workspace_root,
                target_id,
                payload_limit=payload_limit,
            )
        except DomainError as error:
            if error.code in {"portable_artifact_stale", "portable_bundle_stale"}:
                raise _error(
                    "review_candidate_stale",
                    ErrorCategory.CONFLICT,
                    "The Review candidate no longer matches its committed bundle",
                ) from None
            if error.code in {
                "portable_artifact_not_found",
                "portable_bundle_not_found",
            }:
                raise _error(
                    "review_candidate_not_found",
                    ErrorCategory.INVALID_REQUEST,
                    "The requested Review candidate was not found",
                ) from None
            raise

    def _payload(
        self,
        workspace_root: Path,
        base: PortableInspectionRecord,
        artifact: PortableArtifactQueryRecord,
        *,
        expected_kind: str,
        limit: int,
    ) -> bytes:
        inspected = self._inspect(
            workspace_root,
            artifact.artifact_id,
            payload_limit=limit,
        )
        target = inspected.target_artifact
        if (
            inspected.bundle.bundle_id != base.bundle.bundle_id
            or target is None
            or target.artifact_id != artifact.artifact_id
            or target.kind != expected_kind
            or target.sha256 != artifact.sha256
        ):
            raise _invalid_error()
        if inspected.payload is None:
            raise _invalid_error()
        if inspected.payload_truncated:
            raise _limit_error()
        return inspected.payload

    def _source_projection(
        self,
        workspace_root: Path,
        inspected: PortableInspectionRecord,
        draft: PortableArtifactQueryRecord,
        artifact: PortableArtifactQueryRecord,
    ) -> Mapping[str, object]:
        document = _json_document(
            self._payload(
                workspace_root,
                inspected,
                artifact,
                expected_kind="source.metadata.v1",
                limit=_MAX_METADATA_BYTES,
            )
        )
        if document.get("source_metadata_schema_version") != 1:
            raise _invalid_error()
        source_revision_id = _bounded_string(
            document.get("source_revision_id"),
            80,
        )
        source_id = _bounded_string(document.get("source_id"), 80)
        if (
            not draft.source_revision_ids
            or artifact.source_revision_ids != draft.source_revision_ids
            or draft.source_revision_ids != (source_revision_id,)
            or len(inspected.sources) != 1
            or inspected.sources[0].source_id != source_id
        ):
            raise _invalid_error()
        kind = _bounded_string(document.get("source_kind"), 32)
        if kind == "video":
            link = _safe_http_url(document.get("safe_source_link"))
            return {
                "kind": "video",
                "title": _bounded_nullable_string(document.get("title"), 512),
                "author": _bounded_nullable_string(document.get("author"), 256),
                "channel": _bounded_nullable_string(document.get("channel"), 256),
                "duration_ms": _nonnegative_int(document.get("duration_ms")),
                "language": _bounded_nullable_string(document.get("language"), 64),
                "link": link,
            }
        if kind == "document":
            return {
                "kind": "document",
                "name": _safe_display_name(document.get("file_name")),
                "page_count": _positive_int(document.get("page_count")),
            }
        raise _invalid_error()

    def _quality_reports(
        self,
        workspace_root: Path,
        inspected: PortableInspectionRecord,
        draft: PortableArtifactQueryRecord,
        artifacts: Mapping[str, PortableArtifactQueryRecord],
    ) -> list[Mapping[str, object]]:
        if (
            not draft.quality_report_ids
            or len(draft.quality_report_ids) > _MAX_QUALITY_REPORTS
        ):
            raise _invalid_error()
        reports: list[Mapping[str, object]] = []
        for artifact_id in draft.quality_report_ids:
            artifact = artifacts.get(artifact_id)
            if (
                artifact is None
                or draft.artifact_id not in artifact.parent_artifact_ids
                or artifact.source_revision_ids != draft.source_revision_ids
            ):
                raise _invalid_error()
            document = _json_document(
                self._payload(
                    workspace_root,
                    inspected,
                    artifact,
                    expected_kind="quality.report.v1",
                    limit=_MAX_QUALITY_BYTES,
                )
            )
            report = _quality_projection(document, inspected, draft, artifact_id)
            if report["overall"] != inspected.quality.overall:
                raise _invalid_error()
            reports.append(report)
        return reports

    def _evidence_focus(
        self,
        workspace_root: Path,
        inspected: PortableInspectionRecord,
        draft: PortableArtifactQueryRecord,
        evidence_id: str,
    ) -> Mapping[str, object]:
        artifact_id = inspected.bundle.evidence_set_artifact_id
        if artifact_id is None:
            raise _focus_not_found("evidence")
        artifact = _artifact_by_id(inspected, artifact_id)
        if artifact.source_revision_ids != draft.source_revision_ids:
            raise _invalid_error()
        records = _ndjson_records(
            self._payload(
                workspace_root,
                inspected,
                artifact,
                expected_kind="evidence.reference-set.v1",
                limit=_MAX_EVIDENCE_BYTES,
            )
        )
        _validate_evidence_header(records, inspected.bundle.bundle_id)
        matches = [record for record in records[1:] if record.get("evidence_id") == evidence_id]
        if len(matches) != 1:
            raise _focus_not_found("evidence")
        record = matches[0]
        source_revision_ref = _mapping(record.get("source_revision_ref"))
        if (
            source_revision_ref.get("bundle_id") != inspected.bundle.bundle_id
            or source_revision_ref.get("source_revision_id")
            not in draft.source_revision_ids
        ):
            raise _invalid_error()
        locator = _mapping(record.get("locator"))
        scheme = _bounded_string(locator.get("scheme"), 64)
        if scheme != "video-time-range.v1":
            raise _error(
                "review_evidence_unsupported",
                ErrorCategory.INVALID_REQUEST,
                "This evidence locator is not supported by the current Review view",
            )
        start_ms = _nonnegative_int(locator.get("start_ms"))
        end_ms = _positive_int(locator.get("end_ms"))
        if end_ms <= start_ms:
            raise _invalid_error()
        target_ref = _mapping(record.get("target_artifact_ref"))
        transcript_id = _bounded_string(target_ref.get("artifact_id"), 80)
        transcript = _artifact_by_id(inspected, transcript_id)
        if (
            target_ref.get("bundle_id") != inspected.bundle.bundle_id
            or target_ref.get("sha256") != transcript.sha256
        ):
            raise _invalid_error()
        transcript_records = _ndjson_records(
            self._payload(
                workspace_root,
                inspected,
                transcript,
                expected_kind="evidence.transcript.v1",
                limit=_MAX_TRANSCRIPT_BYTES,
            )
        )
        header = transcript_records[0]
        if (
            header.get("record_type") != "transcript_header"
            or header.get("transcript_schema_version") != 1
            or header.get("source_revision_id") not in draft.source_revision_ids
            or header.get("time_base") != "millisecond"
        ):
            raise _invalid_error()
        _bounded_string(header.get("language"), 64)
        segments = [
            value
            for value in transcript_records[1:]
            if value.get("record_type") == "segment"
            and value.get("start_ms") == start_ms
            and value.get("end_ms") == end_ms
        ]
        if len(segments) != 1:
            raise _invalid_error()
        excerpt = _bounded_string(segments[0].get("text"), _MAX_TEXT)
        if record.get("excerpt_sha256") != _sha256_text(excerpt):
            raise _invalid_error()
        return {
            "kind": "evidence",
            "evidence_id": evidence_id,
            "locator": {
                "scheme": scheme,
                "start_ms": start_ms,
                "end_ms": end_ms,
            },
            "excerpt": excerpt,
        }

    def _document_independently_verified(
        self,
        workspace_root: Path,
        inspected: PortableInspectionRecord,
        draft: PortableArtifactQueryRecord,
        reports: Sequence[Mapping[str, object]],
    ) -> bool:
        matching_reports = [
            report
            for report in reports
            if report.get("profile")
            == {"id": "alltonote.document-knowledge-note", "version": 1}
        ]
        if len(matching_reports) != 1:
            return False
        report = matching_reports[0]
        if report.get("method") != {"kind": "model"}:
            return False
        checks = report.get("checks")
        if not isinstance(checks, Sequence):
            return False
        check_status = {
            check.get("id"): check.get("status")
            for check in checks
            if isinstance(check, Mapping)
        }
        if check_status.get("knowledge-note-quality") != "pass" or (
            check_status.get("source-coverage") != "pass"
        ):
            return False

        matches = [
            artifact
            for artifact in inspected.artifacts
            if artifact.kind == "document.knowledge-map.v1"
        ]
        if len(matches) != 1:
            return False
        knowledge_artifact = matches[0]
        if (
            knowledge_artifact.artifact_id not in draft.parent_artifact_ids
            or knowledge_artifact.source_revision_ids != draft.source_revision_ids
        ):
            return False
        knowledge = _json_document(
            self._payload(
                workspace_root,
                inspected,
                knowledge_artifact,
                expected_kind="document.knowledge-map.v1",
                limit=_MAX_KNOWLEDGE_MAP_BYTES,
            )
        )
        if knowledge.get("knowledge_map_schema_version") != 1:
            return False
        composer_identity = knowledge.get("model_identity")
        verification = knowledge.get("semantic_verification")
        if type(composer_identity) is not str or not isinstance(verification, Mapping):
            return False
        verifier_identity = verification.get("model_identity")
        if (
            type(verifier_identity) is not str
            or not composer_identity.strip()
            or not verifier_identity.strip()
            or composer_identity == verifier_identity
        ):
            return False
        items = _mapping_list(knowledge.get("items"), _MAX_RECORDS)
        claims = _mapping_list(verification.get("claims"), _MAX_RECORDS)
        item_ids = [
            _bounded_string(item.get("note_item_id"), 128) for item in items
        ]
        claim_ids: list[str] = []
        for claim in claims:
            if claim.get("status") != "supported":
                return False
            claim_ids.append(_bounded_string(claim.get("claim_id"), 128))
        return bool(item_ids) and len(set(item_ids)) == len(item_ids) and (
            sorted(item_ids) == sorted(claim_ids)
        )

    def _note_item_focus(
        self,
        workspace_root: Path,
        inspected: PortableInspectionRecord,
        draft: PortableArtifactQueryRecord,
        note_item_id: str,
    ) -> Mapping[str, object]:
        knowledge_artifact = _single_kind(inspected, "document.knowledge-map.v1")
        normalized_artifact = _single_kind(
            inspected,
            "document.normalized-content.v1",
        )
        if (
            knowledge_artifact.artifact_id not in draft.parent_artifact_ids
            or normalized_artifact.artifact_id
            not in knowledge_artifact.parent_artifact_ids
            or knowledge_artifact.source_revision_ids != draft.source_revision_ids
            or normalized_artifact.source_revision_ids != draft.source_revision_ids
        ):
            raise _invalid_error()
        knowledge = _json_document(
            self._payload(
                workspace_root,
                inspected,
                knowledge_artifact,
                expected_kind="document.knowledge-map.v1",
                limit=_MAX_KNOWLEDGE_MAP_BYTES,
            )
        )
        if knowledge.get("knowledge_map_schema_version") != 1:
            raise _invalid_error()
        items = _mapping_list(knowledge.get("items"), _MAX_RECORDS)
        matches = [item for item in items if item.get("note_item_id") == note_item_id]
        if len(matches) != 1:
            raise _focus_not_found("note item")
        source_ids = _unique_string_list(
            matches[0].get("source_block_ids"),
            _MAX_SOURCE_BLOCKS,
        )
        verification = _mapping(knowledge.get("semantic_verification"))
        claims = _mapping_list(verification.get("claims"), _MAX_RECORDS)
        claim_matches = [claim for claim in claims if claim.get("claim_id") == note_item_id]
        if len(claim_matches) != 1:
            raise _invalid_error()
        verifier_identity = _bounded_string(verification.get("model_identity"), 512)
        status = _bounded_string(claim_matches[0].get("status"), 64)
        if status not in _SEMANTIC_STATUSES:
            raise _invalid_error()

        records = _ndjson_records(
            self._payload(
                workspace_root,
                inspected,
                normalized_artifact,
                expected_kind="document.normalized-content.v1",
                limit=_MAX_DOCUMENT_BYTES,
            )
        )
        if not records or records[0] != {
            "record_type": "document_normalized_header",
            "schema_version": 1,
            "source_sha256": records[0].get("source_sha256"),
            "page_count": records[0].get("page_count"),
        }:
            raise _invalid_error()
        by_id: dict[str, Mapping[str, object]] = {}
        for record in records[1:]:
            if record.get("record_type") != "block":
                raise _invalid_error()
            block_id = _bounded_string(record.get("block_id"), 256)
            if block_id in by_id:
                raise _invalid_error()
            by_id[block_id] = record
        blocks = [_block_projection(by_id.get(block_id), block_id) for block_id in source_ids]
        return {
            "kind": "note_item",
            "note_item_id": note_item_id,
            "verification": {
                "status": status,
                "model_identity": verifier_identity,
            },
            "source_blocks": blocks,
        }


def _single_kind(
    inspected: PortableInspectionRecord,
    kind: str,
) -> PortableArtifactQueryRecord:
    matches = [artifact for artifact in inspected.artifacts if artifact.kind == kind]
    if len(matches) != 1:
        raise _invalid_error()
    return matches[0]


def _artifact_by_id(
    inspected: PortableInspectionRecord,
    artifact_id: str,
) -> PortableArtifactQueryRecord:
    matches = [
        artifact for artifact in inspected.artifacts if artifact.artifact_id == artifact_id
    ]
    if len(matches) != 1:
        raise _invalid_error()
    return matches[0]


def _quality_projection(
    document: Mapping[str, object],
    inspected: PortableInspectionRecord,
    draft: PortableArtifactQueryRecord,
    artifact_id: str,
) -> Mapping[str, object]:
    if document.get("quality_report_schema_version") != 1:
        raise _invalid_error()
    subject = _mapping(document.get("subject"))
    if subject != {
        "bundle_id": inspected.bundle.bundle_id,
        "artifact_id": draft.artifact_id,
        "sha256": draft.sha256,
    }:
        raise _invalid_error()
    profile = _mapping(document.get("profile"))
    profile_projection = {
        "id": _bounded_string(profile.get("id"), 256),
        "version": _positive_int(profile.get("version")),
    }
    checks = _mapping_list(document.get("checks"), _MAX_CHECKS)
    check_projection: list[Mapping[str, object]] = []
    seen: set[str] = set()
    for check in checks:
        check_id = _bounded_string(check.get("id"), 256)
        if check_id in seen:
            raise _invalid_error()
        seen.add(check_id)
        projected: dict[str, object] = {
            "id": check_id,
            "status": _bounded_string(check.get("status"), 32),
        }
        if "reason" in check:
            projected["reason"] = _bounded_string(check.get("reason"), 512)
        check_projection.append(projected)
    messages = [
        _bounded_string(message, 512)
        for message in _list(document.get("messages"), _MAX_MESSAGES)
    ]
    method = _mapping(document.get("method"))
    method_projection = {"kind": _bounded_string(method.get("kind"), 64)}
    overall = _bounded_string(document.get("overall"), 32)
    if overall not in _QUALITY_OVERALLS:
        raise _invalid_error()
    return {
        "artifact_id": artifact_id,
        "profile": profile_projection,
        "overall": overall,
        "method": method_projection,
        "checks": check_projection,
        "messages": messages,
    }


def _publication_admission(
    *,
    stored_publish_eligible: bool,
    reports: Sequence[Mapping[str, object]],
    document_independently_verified: bool | None = None,
) -> Mapping[str, str]:
    if not stored_publish_eligible or any(
        report.get("overall") not in {"pass", "pass_with_warnings"}
        for report in reports
    ):
        return {
            "status": "blocked",
            "reason": "quality-report-not-publishable",
        }
    for report in reports:
        profile = report.get("profile")
        if not isinstance(profile, Mapping):
            return {
                "status": "blocked",
                "reason": "quality-profile-unsupported",
            }
        identity = (profile.get("id"), profile.get("version"))
        if identity == ("alltonote.document-note", 1):
            return {
                "status": "blocked",
                "reason": "legacy-document-quality-profile-not-publishable",
            }
        if identity == ("alltonote.document-native-extraction", 1):
            return {
                "status": "blocked",
                "reason": "document-native-extraction-not-publishable",
            }
        if identity == ("alltonote.document-knowledge-note", 1) and not (
            document_independently_verified
        ):
            return {
                "status": "blocked",
                "reason": "document-independent-verification-not-proven",
            }
        if identity not in _SUPPORTED_PUBLICATION_QUALITY_PROFILES:
            return {
                "status": "blocked",
                "reason": "quality-profile-unsupported",
            }
    return {"status": "pass", "reason": "quality-profile-supported"}


def _block_projection(
    record: Mapping[str, object] | None,
    block_id: str,
) -> Mapping[str, object]:
    if record is None or record.get("block_id") != block_id:
        raise _invalid_error()
    text = _bounded_string(record.get("text"), _MAX_TEXT)
    if record.get("content_sha256") != _sha256_text(text):
        raise _invalid_error()
    bbox = _mapping(record.get("bbox"))
    return {
        "block_id": block_id,
        "page": _positive_int(record.get("page_number")),
        "kind": _bounded_string(record.get("kind"), 64),
        "text": text,
        "bbox": {
            "left": _finite_number(bbox.get("left")),
            "top": _finite_number(bbox.get("top")),
            "right": _finite_number(bbox.get("right")),
            "bottom": _finite_number(bbox.get("bottom")),
            "origin": _bounded_string(bbox.get("origin"), 32),
        },
        "basis": _bounded_string(record.get("basis"), 64),
    }


def _json_document(payload: bytes) -> Mapping[str, object]:
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, ValueError):
        raise _invalid_error() from None
    return _mapping(value)


def _ndjson_records(payload: bytes) -> list[Mapping[str, object]]:
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeError:
        raise _invalid_error() from None
    lines = text.splitlines()
    if not lines or len(lines) > _MAX_RECORDS:
        raise _invalid_error()
    records: list[Mapping[str, object]] = []
    for line in lines:
        if not line:
            raise _invalid_error()
        try:
            value = json.loads(
                line,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
        except ValueError:
            raise _invalid_error() from None
        records.append(_mapping(value))
    return records


def _validate_evidence_header(
    records: list[Mapping[str, object]],
    bundle_id: str,
) -> None:
    if not records or records[0] != {
        "record_type": "evidence_set_header",
        "evidence_set_schema_version": 1,
        "bundle_id": bundle_id,
        "record_count": len(records) - 1,
    }:
        raise _invalid_error()


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> Mapping[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError
        document[key] = value
    return document


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _invalid_error()
    return value


def _mapping_list(value: object, maximum: int) -> list[Mapping[str, object]]:
    return [_mapping(item) for item in _list(value, maximum)]


def _list(value: object, maximum: int) -> list[object]:
    if type(value) is not list or len(value) > maximum:
        raise _invalid_error()
    return value


def _unique_string_list(value: object, maximum: int) -> list[str]:
    result = [_bounded_string(item, 256) for item in _list(value, maximum)]
    if not result or len(result) != len(set(result)):
        raise _invalid_error()
    return result


def _bounded_string(value: object, maximum: int) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise _invalid_error()
    return value


def _bounded_nullable_string(value: object, maximum: int) -> str | None:
    if value is None:
        return None
    return _bounded_string(value, maximum)


def _nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise _invalid_error()
    return value


def _positive_int(value: object) -> int:
    value = _nonnegative_int(value)
    if value == 0:
        raise _invalid_error()
    return value


def _finite_number(value: object) -> int | float:
    if type(value) not in {int, float} or not math.isfinite(value):
        raise _invalid_error()
    return value


def _safe_http_url(value: object) -> str | None:
    if value is None:
        return None
    url = _bounded_string(value, 2048)
    try:
        parsed = urlsplit(url)
        query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=False)
        fragment = parse_qsl(
            parsed.fragment,
            keep_blank_values=True,
            strict_parsing=False,
        )
        _, separator, fragment_tail = parsed.fragment.partition("?")
        fragment_query = (
            parse_qsl(fragment_tail, keep_blank_values=True, strict_parsing=False)
            if separator
            else ()
        )
    except (UnicodeError, ValueError):
        raise _invalid_error() from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(
            _sensitive_query_key(key)
            for key, _value in (*query, *fragment, *fragment_query)
        )
    ):
        raise _invalid_error()
    return url


def _safe_display_name(value: object) -> str:
    name = _bounded_string(value, 512)
    if (
        name in {".", ".."}
        or "/" in name
        or "\\" in name
        or ":" in name
        or any(ord(character) < 32 for character in name)
    ):
        raise _invalid_error()
    return name


def _sensitive_query_key(value: str) -> bool:
    canonical = re.sub(r"[^a-z0-9]", "", value.casefold())
    return (
        canonical in _SENSITIVE_URL_QUERY_KEYS
        or canonical.startswith("xamz")
        or canonical.startswith("xgoog")
    )


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _focus_not_found(subject: str) -> DomainError:
    return _error(
        "review_focus_not_found",
        ErrorCategory.INVALID_REQUEST,
        f"The requested Review {subject} was not found",
    )


def _limit_error() -> DomainError:
    return _error(
        "review_candidate_limit_exceeded",
        ErrorCategory.WORKSPACE_INCOMPATIBLE,
        "The Review candidate exceeds the bounded inspection limit",
    )


def _invalid_error() -> DomainError:
    return _error(
        "review_candidate_invalid",
        ErrorCategory.WORKSPACE_INCOMPATIBLE,
        "The Review candidate is malformed or internally inconsistent",
    )


def _error(code: str, category: ErrorCategory, message: str) -> DomainError:
    return DomainError(code, category, message)


__all__ = ["ReviewCandidateService"]

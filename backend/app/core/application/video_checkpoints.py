from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from uuid import UUID

from app.core.domain.video import (
    GeneratedVideoDraft,
    QualityOverall,
    ScreenshotRequest,
    TranscriptDocument,
    TranscriptSegment,
)
from app.core.errors import DomainError, ErrorCategory
from app.core.portable.bundle_assembler import DisplayAssetInput, VideoSourceMetadata


def checkpoint_error() -> DomainError:
    return DomainError(
        "checkpoint_content_invalid",
        ErrorCategory.INTERNAL,
        "Checkpoint content is invalid",
    )


def decode_object(payload: bytes, *, keys: frozenset[str]) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except (UnicodeError, ValueError, RecursionError):
        raise checkpoint_error() from None
    if type(value) is not dict or frozenset(value) != keys:
        raise checkpoint_error()
    return value


def encode_object(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True)
class CandidateCheckpoint:
    staging_relative_path: str
    bundle_id: str
    manifest_sha256: str
    run_id: str
    source_id: str
    connector_id: str
    canonical_identity: str
    source_revision_id: str
    primary_draft_artifact_id: str
    transcript_artifact_id: str
    evidence_set_artifact_id: str
    quality_report_artifact_id: str
    display_asset_ids: tuple[str, ...]
    quality_overall: QualityOverall
    publish_eligible: bool
    usage: Mapping[str, int]
    warnings: tuple[str, ...]

    _KEYS = frozenset(
        {
            "step",
            "staging_relative_path",
            "bundle_id",
            "manifest_sha256",
            "run_id",
            "source_id",
            "connector_id",
            "canonical_identity",
            "source_revision_id",
            "primary_draft_artifact_id",
            "transcript_artifact_id",
            "evidence_set_artifact_id",
            "quality_report_artifact_id",
            "display_asset_ids",
            "quality_overall",
            "publish_eligible",
            "usage",
            "warnings",
        }
    )
    _USAGE_KEYS = frozenset({"input_tokens", "output_tokens"})

    def encode(self) -> bytes:
        return encode_object(
            {
                "step": "assemble_candidate_bundle",
                "staging_relative_path": self.staging_relative_path,
                "bundle_id": self.bundle_id,
                "manifest_sha256": self.manifest_sha256,
                "run_id": self.run_id,
                "source_id": self.source_id,
                "connector_id": self.connector_id,
                "canonical_identity": self.canonical_identity,
                "source_revision_id": self.source_revision_id,
                "primary_draft_artifact_id": self.primary_draft_artifact_id,
                "transcript_artifact_id": self.transcript_artifact_id,
                "evidence_set_artifact_id": self.evidence_set_artifact_id,
                "quality_report_artifact_id": self.quality_report_artifact_id,
                "display_asset_ids": list(self.display_asset_ids),
                "quality_overall": self.quality_overall.value,
                "publish_eligible": self.publish_eligible,
                "usage": dict(self.usage),
                "warnings": list(self.warnings),
            }
        )

    @classmethod
    def decode(cls, payload: bytes) -> CandidateCheckpoint:
        try:
            value = json.loads(payload)
            if (
                type(value) is not dict
                or frozenset(value) != cls._KEYS
                or value["step"] != "assemble_candidate_bundle"
            ):
                raise TypeError
            usage = value["usage"]
            if (
                type(usage) is not dict
                or not frozenset(usage) <= cls._USAGE_KEYS
                or any(type(item) is not int or item < 0 for item in usage.values())
            ):
                raise TypeError
            display_asset_ids = value["display_asset_ids"]
            warnings = value["warnings"]
            if (
                type(display_asset_ids) is not list
                or any(not _typed_id_is_valid(item, "art") for item in display_asset_ids)
                or len(set(display_asset_ids)) != len(display_asset_ids)
                or type(warnings) is not list
                or any(type(item) is not str for item in warnings)
                or type(value["publish_eligible"]) is not bool
                or not _staging_path_is_valid(value["staging_relative_path"])
            ):
                raise TypeError
            for field, prefix in (
                ("bundle_id", "bnd"),
                ("run_id", "run"),
                ("source_id", "src"),
                ("source_revision_id", "rev"),
                ("primary_draft_artifact_id", "art"),
                ("transcript_artifact_id", "art"),
                ("evidence_set_artifact_id", "art"),
                ("quality_report_artifact_id", "art"),
            ):
                if not _typed_id_is_valid(value[field], prefix):
                    raise TypeError
            digest = value["manifest_sha256"]
            if (
                type(digest) is not str
                or len(digest) != 71
                or not digest.startswith("sha256:")
                or any(character not in "0123456789abcdef" for character in digest[7:])
                or type(value["connector_id"]) is not str
                or not value["connector_id"]
                or type(value["canonical_identity"]) is not str
                or not value["canonical_identity"]
            ):
                raise TypeError
            return cls(
                staging_relative_path=value["staging_relative_path"],
                bundle_id=value["bundle_id"],
                manifest_sha256=digest,
                run_id=value["run_id"],
                source_id=value["source_id"],
                connector_id=value["connector_id"],
                canonical_identity=value["canonical_identity"],
                source_revision_id=value["source_revision_id"],
                primary_draft_artifact_id=value["primary_draft_artifact_id"],
                transcript_artifact_id=value["transcript_artifact_id"],
                evidence_set_artifact_id=value["evidence_set_artifact_id"],
                quality_report_artifact_id=value["quality_report_artifact_id"],
                display_asset_ids=tuple(display_asset_ids),
                quality_overall=QualityOverall(value["quality_overall"]),
                publish_eligible=value["publish_eligible"],
                usage=usage,
                warnings=tuple(warnings),
            )
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise DomainError(
                "candidate_checkpoint_invalid",
                ErrorCategory.INTERNAL,
                "Candidate checkpoint is invalid",
            ) from None


def _typed_id_is_valid(value: object, prefix: str) -> bool:
    if type(value) is not str or not value.startswith(f"{prefix}_"):
        return False
    suffix = value[len(prefix) + 1 :]
    try:
        parsed = UUID(suffix)
    except (AttributeError, ValueError):
        return False
    return str(parsed) == suffix and parsed.version == 7 and suffix[19] in "89ab"


def _staging_path_is_valid(value: object) -> bool:
    if type(value) is not str or "\\" in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and path.as_posix() == value
        and path.parts[:3] == ("raw", "personal", ".staging")
        and len(path.parts) >= 5
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def decode_preflight(payload: bytes) -> str:
    value = decode_object(payload, keys=frozenset({"step", "policy_hash"}))
    policy_hash = value["policy_hash"]
    if (
        value["step"] != "preflight"
        or type(policy_hash) is not str
        or not policy_hash.startswith("sha256:")
    ):
        raise checkpoint_error()
    return policy_hash


def encode_source(source: VideoSourceMetadata) -> bytes:
    value = {"step": "resolve_source"}
    value.update(
        {
            field: dict(item) if field == "extensions" else item
            for field, item in vars(source).items()
        }
    )
    return encode_object(value)


def decode_source(payload: bytes) -> VideoSourceMetadata:
    keys = frozenset(VideoSourceMetadata.__dataclass_fields__) | {"step"}
    value = decode_object(payload, keys=keys)
    try:
        if value.pop("step") != "resolve_source":
            raise TypeError
        return VideoSourceMetadata(**value)
    except (DomainError, TypeError, ValueError):
        raise checkpoint_error() from None


def encode_acquired(value: object) -> bytes:
    if type(value) is not str or not value:
        raise checkpoint_error()
    return encode_object({"step": "acquire", "reference": value})


def decode_acquired(payload: bytes) -> object:
    value = decode_object(payload, keys=frozenset({"step", "reference"}))
    reference = value["reference"]
    if value["step"] != "acquire" or type(reference) is not str or not reference:
        raise checkpoint_error()
    return reference


def encode_transcript(transcript: TranscriptDocument) -> bytes:
    return encode_object(
        {
            "step": "normalize_transcript",
            "language": transcript.language,
            "segments": [
                {
                    "segment_id": item.segment_id,
                    "start_ms": item.start_ms,
                    "end_ms": item.end_ms,
                    "text": item.text,
                }
                for item in transcript.segments
            ],
        }
    )


def decode_transcript(payload: bytes) -> TranscriptDocument:
    value = decode_object(payload, keys=frozenset({"step", "language", "segments"}))
    try:
        if value["step"] != "normalize_transcript" or type(value["segments"]) is not list:
            raise TypeError
        return TranscriptDocument(
            language=value["language"],
            segments=tuple(TranscriptSegment(**item) for item in value["segments"]),
        )
    except (DomainError, TypeError, ValueError):
        raise checkpoint_error() from None


def decode_revision(payload: bytes) -> str:
    value = decode_object(payload, keys=frozenset({"step", "revision_id"}))
    revision_id = value["revision_id"]
    if (
        value["step"] != "create_source_revision"
        or type(revision_id) is not str
        or not revision_id.startswith("rev_")
    ):
        raise checkpoint_error()
    return revision_id


def encode_draft(draft: GeneratedVideoDraft) -> bytes:
    return encode_object(
        {
            "step": "generate_draft",
            "markdown": draft.markdown,
            "cited_segment_ids": list(draft.cited_segment_ids),
            "screenshot_requests": [
                {"segment_id": item.segment_id, "offset_ms": item.offset_ms}
                for item in draft.screenshot_requests
            ],
            "model_identity": draft.model_identity,
            "usage": dict(draft.usage),
            "warnings": list(draft.warnings),
        }
    )


def decode_draft(payload: bytes) -> GeneratedVideoDraft:
    keys = frozenset(
        {
            "markdown",
            "step",
            "cited_segment_ids",
            "screenshot_requests",
            "model_identity",
            "usage",
            "warnings",
        }
    )
    value = decode_object(payload, keys=keys)
    try:
        if value["step"] != "generate_draft" or type(value["screenshot_requests"]) is not list:
            raise TypeError
        return GeneratedVideoDraft(
            markdown=value["markdown"],
            cited_segment_ids=tuple(value["cited_segment_ids"]),
            screenshot_requests=tuple(
                ScreenshotRequest(**item) for item in value["screenshot_requests"]
            ),
            model_identity=value["model_identity"],
            usage=value["usage"],
            warnings=tuple(value["warnings"]),
        )
    except (DomainError, TypeError, ValueError):
        raise checkpoint_error() from None


def encode_screenshots(values: tuple[DisplayAssetInput, ...]) -> bytes:
    return encode_object(
        {
            "step": "optional_screenshots",
            "assets": [
                {
                    "artifact_id": item.artifact_id,
                    "relative_path": item.relative_path,
                    "media_type": item.media_type,
                    "payload_base64": base64.b64encode(item.payload).decode("ascii"),
                    "artifact_type": item.artifact_type,
                }
                for item in values
            ],
        }
    )


def decode_screenshots(payload: bytes) -> tuple[DisplayAssetInput, ...]:
    value = decode_object(payload, keys=frozenset({"step", "assets"}))
    try:
        if value["step"] != "optional_screenshots" or type(value["assets"]) is not list:
            raise TypeError
        return tuple(
            DisplayAssetInput(
                artifact_id=item["artifact_id"],
                relative_path=item["relative_path"],
                media_type=item["media_type"],
                payload=base64.b64decode(item["payload_base64"], validate=True),
                artifact_type=item["artifact_type"],
            )
            for item in value["assets"]
        )
    except (DomainError, KeyError, TypeError, ValueError):
        raise checkpoint_error() from None


def encode_validation(report: object) -> bytes:
    return encode_object(
        {
            "step": "quality_and_portable_validation",
            "valid": getattr(report, "valid", None),
            "bundle_id": getattr(report, "bundle_id", None),
            "manifest_sha256": getattr(report, "manifest_sha256", None),
        }
    )


def decode_validation(payload: bytes, expected: CandidateCheckpoint) -> object:
    value = decode_object(
        payload,
        keys=frozenset({"step", "valid", "bundle_id", "manifest_sha256"}),
    )
    if (
        value["step"] != "quality_and_portable_validation"
        or value["valid"] is not True
        or value["bundle_id"] != expected.bundle_id
        or value["manifest_sha256"] != expected.manifest_sha256
    ):
        raise checkpoint_error()
    return value

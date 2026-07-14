from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath, PureWindowsPath
from types import MappingProxyType
from urllib.parse import parse_qsl, urlsplit

from app.core.domain.ids import sha256_digest
from app.core.domain.video import QualityOverall, TranscriptDocument
from app.core.errors import DomainError, ErrorCategory
from app.core.portable.artifacts import PortableArtifactRef, build_transcript
from app.core.portable.evidence import EvidenceSet, build_evidence_set
from app.core.portable.jsonio import encode_json
from app.core.portable.quality import QualityOutcome
from app.core.ports.portable import (
    CandidateBundleLocation,
    CandidateLocationCapabilityPort,
)


_CONTROL_FILES = frozenset({"bundle.json", "receipt.json", "commit.json"})
_WINDOWS_DEVICE_STEMS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
    | {f"com{number}" for number in "¹²³"}
    | {f"lpt{number}" for number in "¹²³"}
)
_TYPED_ID = re.compile(
    r"(?P<prefix>[a-z]+)_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_EMBEDDED_WINDOWS_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")
_EMBEDDED_UNC_PATH = re.compile(r"(?<![\\])\\\\[^\\\s]+\\[^\\\s]+")
_EMBEDDED_POSIX_PATH = re.compile(r"(?<![:/A-Za-z0-9._-])/(?!/)[^\s]+")
_EXECUTOR_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@+-]{0,127}\Z")
_FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "api_key",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "full_prompt",
        "password",
        "pid",
        "process_id",
        "provider_raw",
        "provider_raw_request",
        "provider_raw_response",
        "provider_request_id",
        "secret",
        "secret_value",
        "lease",
        "lease_id",
        "fence",
        "fencing",
        "fencing_token",
    }
)
_FORBIDDEN_CANONICAL_FIELD_NAMES = frozenset(
    re.sub(r"[^a-z0-9]", "", name.casefold()) for name in _FORBIDDEN_FIELD_NAMES
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
        "xamzcredential",
        "xamzsecuritytoken",
        "xamzsignature",
    }
)
_USAGE_FIELDS = frozenset({"input_tokens", "output_tokens"})
_REDACTION_FIELDS = frozenset({"secrets", "prompts", "provider_payloads"})
_REDACTION_VALUES = {
    "secrets": frozenset({"omitted"}),
    "prompts": frozenset({"hash_only", "omitted"}),
    "provider_payloads": frozenset({"omitted"}),
}

def _error(code: str, message: str) -> DomainError:
    return DomainError(code, ErrorCategory.INVALID_REQUEST, message)


def _require_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise _error("video_bundle_input_invalid", f"{field_name} must not be empty")
    return value


def _require_id(value: object, prefix: str, field_name: str) -> str:
    if type(value) is not str:
        raise _error("video_bundle_reference_invalid", f"{field_name} is invalid")
    match = _TYPED_ID.fullmatch(value)
    if match is None or match.group("prefix") != prefix:
        raise _error("video_bundle_reference_invalid", f"{field_name} is invalid")
    return value


def _require_digest(value: object, field_name: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise _error("video_bundle_reference_invalid", f"{field_name} is invalid")
    return value


def _parse_timestamp(value: object, field_name: str) -> datetime:
    if type(value) is not str or len(value) != 24:
        raise _error("video_bundle_input_invalid", f"{field_name} is invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        raise _error("video_bundle_input_invalid", f"{field_name} is invalid") from None
    return parsed.replace(tzinfo=timezone.utc)


def _snapshot_mapping(value: Mapping[str, object], field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _error("video_bundle_input_invalid", f"{field_name} must be an object")
    try:
        document = json.loads(encode_json(dict(value)))
    except MemoryError:
        raise
    except DomainError:
        raise _error("video_bundle_input_invalid", f"{field_name} must be JSON-safe") from None
    except Exception:
        raise _error("video_bundle_input_invalid", f"{field_name} must be JSON-safe") from None
    assert isinstance(document, dict)
    return MappingProxyType(document)


def _is_absolute_or_local_path(value: str) -> bool:
    lowered = value.lower()
    return (
        PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
        or lowered.startswith("file:")
    )


def _validate_safe_value(value: object, field_name: str = "value") -> None:
    if value is None or type(value) in {bool, int, float}:
        return
    if type(value) is str:
        if (
            _is_absolute_or_local_path(value)
            or _EMBEDDED_WINDOWS_PATH.search(value)
            or _EMBEDDED_UNC_PATH.search(value)
            or _EMBEDDED_POSIX_PATH.search(value)
        ):
            raise _error(
                "video_bundle_sensitive_data",
                "Portable bundle content contains an absolute local path",
            )
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if type(key) is not str:
                raise _error("video_bundle_input_invalid", f"{field_name} has a non-text key")
            canonical_key = re.sub(r"[^a-z0-9]", "", key.casefold())
            if canonical_key in _FORBIDDEN_CANONICAL_FIELD_NAMES:
                raise _error(
                    "video_bundle_sensitive_data",
                    "Portable bundle content contains forbidden provenance data",
                )
            _validate_safe_value(item, f"{field_name}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_safe_value(item, f"{field_name}[{index}]")
        return
    raise _error("video_bundle_input_invalid", f"{field_name} is not JSON-safe")


def _portable_path(value: str, *, allow_control: bool = False) -> str:
    if (
        type(value) is not str
        or not value
        or "\\" in value
        or "\0" in value
    ):
        raise _error("video_bundle_path_invalid", "Bundle path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise _error("video_bundle_path_invalid", "Bundle path is invalid")
    for part in path.parts:
        try:
            part.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise _error("video_bundle_path_invalid", "Bundle path is not portable") from None
        if (
            ":" in part
            or part.endswith((" ", "."))
            or unicodedata.normalize("NFC", part) != part
            or part.split(".", 1)[0].casefold() in _WINDOWS_DEVICE_STEMS
        ):
            raise _error("video_bundle_path_invalid", "Bundle path is not portable")
    if not allow_control and value.lower() in _CONTROL_FILES:
        raise _error("video_bundle_path_invalid", "Artifact path collides with a control file")
    return value


def _portable_path_key(value: str) -> tuple[str, ...]:
    return tuple(
        unicodedata.normalize("NFC", part).casefold()
        for part in PurePosixPath(value).parts
    )


def _require_safe_url(value: str, field_name: str) -> None:
    try:
        parsed = urlsplit(value)
        query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=False)
    except (UnicodeError, ValueError):
        raise _error("video_bundle_sensitive_data", f"{field_name} is not a safe URL") from None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(_is_sensitive_url_query_key(key) for key, _ in query)
    ):
        raise _error("video_bundle_sensitive_data", f"{field_name} is not a safe URL")


def _require_executor_identity(value: str, field_name: str) -> None:
    if _EXECUTOR_IDENTITY.fullmatch(value) is None:
        raise _error(
            "video_bundle_sensitive_data",
            f"{field_name} is not an approved executor identity",
        )


def _is_sensitive_url_query_key(key: str) -> bool:
    canonical = re.sub(r"[^a-z0-9]", "", key.casefold())
    return (
        canonical in _SENSITIVE_URL_QUERY_KEYS
        or canonical.startswith("xamz")
        or canonical.startswith("xgoog")
    )


def _is_webp(payload: bytes) -> bool:
    if (
        len(payload) < 20
        or payload[:4] != b"RIFF"
        or payload[8:12] != b"WEBP"
        or int.from_bytes(payload[4:8], "little") != len(payload) - 8
        or payload[12:16] not in {b"VP8 ", b"VP8L", b"VP8X"}
    ):
        return False
    offset = 12
    while offset < len(payload):
        if offset + 8 > len(payload):
            return False
        chunk_length = int.from_bytes(payload[offset + 4 : offset + 8], "little")
        offset += 8 + chunk_length + (chunk_length % 2)
        if offset > len(payload):
            return False
    return offset == len(payload)


@dataclass(frozen=True)
class VideoArtifactIds:
    source_metadata: str
    transcript: str
    evidence_set: str
    primary_draft: str
    quality_report: str

    def __post_init__(self) -> None:
        values = (
            self.source_metadata,
            self.transcript,
            self.evidence_set,
            self.primary_draft,
            self.quality_report,
        )
        for index, value in enumerate(values):
            _require_id(value, "art", f"artifact_ids[{index}]")
        if len(values) != len(set(values)):
            raise _error("video_bundle_reference_invalid", "Artifact IDs must be unique")


@dataclass(frozen=True)
class VideoSourceMetadata:
    source_id: str
    source_revision_id: str
    connector_id: str
    platform: str
    canonical_identity_scheme: str
    stable_video_identity: str
    canonical_uri: str
    title: str
    author: str
    channel: str
    duration_ms: int
    published_at: str | None
    observed_at: str
    language: str
    subtitle_acquisition: str
    source_link: str
    materialization_reason: str
    license: str
    privacy: str
    freshness: str
    extensions: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_id(self.source_id, "src", "source_id")
        _require_id(self.source_revision_id, "rev", "source_revision_id")
        for field_name in (
            "connector_id",
            "platform",
            "canonical_identity_scheme",
            "stable_video_identity",
            "canonical_uri",
            "title",
            "author",
            "channel",
            "observed_at",
            "language",
            "subtitle_acquisition",
            "source_link",
            "materialization_reason",
            "license",
            "privacy",
            "freshness",
        ):
            _require_text(getattr(self, field_name), field_name)
        if type(self.duration_ms) is not int or self.duration_ms <= 0:
            raise _error("video_bundle_input_invalid", "duration_ms must be positive")
        if self.published_at is not None:
            _require_text(self.published_at, "published_at")
        if self.license not in {"known", "unknown", "restricted"}:
            raise _error("video_bundle_input_invalid", "license is invalid")
        if self.privacy not in {"public", "personal", "sensitive", "confidential", "unknown"}:
            raise _error("video_bundle_input_invalid", "privacy is invalid")
        _require_safe_url(self.canonical_uri, "canonical_uri")
        _require_safe_url(self.source_link, "source_link")
        object.__setattr__(self, "extensions", _snapshot_mapping(self.extensions, "extensions"))


@dataclass(frozen=True)
class StepAttemptSummary:
    step_id: str
    attempt: int
    state: str
    started_at: str
    completed_at: str

    def __post_init__(self) -> None:
        for field_name in ("step_id", "state", "started_at", "completed_at"):
            _require_text(getattr(self, field_name), field_name)
        if type(self.attempt) is not int or self.attempt < 1:
            raise _error("video_bundle_input_invalid", "Step attempt must be positive")


@dataclass(frozen=True)
class ReceiptProvenance:
    run_id: str
    job_id: str
    attempt_id: str
    started_at: str
    completed_at: str
    recipe_id: str
    recipe_version: int
    capability_id: str
    capability_version: str
    runtime_version: str
    portable_contract_id: str
    effective_policy_hashes: Mapping[str, object]
    model_identity: str
    transcriber_identity: str
    usage: Mapping[str, object]
    warnings: tuple[str, ...]
    redactions: Mapping[str, object]
    steps: tuple[StepAttemptSummary, ...]
    retry_of_job_id: str | None = None
    parent_run_id: str | None = None

    def __post_init__(self) -> None:
        _require_id(self.run_id, "run", "run_id")
        _require_id(self.job_id, "job", "job_id")
        _require_id(self.attempt_id, "att", "attempt_id")
        if self.retry_of_job_id is not None:
            _require_id(self.retry_of_job_id, "job", "retry_of_job_id")
        if self.parent_run_id is not None:
            _require_id(self.parent_run_id, "run", "parent_run_id")
        for field_name in (
            "started_at",
            "completed_at",
            "recipe_id",
            "capability_id",
            "capability_version",
            "runtime_version",
            "portable_contract_id",
            "model_identity",
            "transcriber_identity",
        ):
            _require_text(getattr(self, field_name), field_name)
        if type(self.recipe_version) is not int or self.recipe_version < 1:
            raise _error("video_bundle_input_invalid", "recipe_version must be positive")
        policy_hashes = _snapshot_mapping(self.effective_policy_hashes, "effective_policy_hashes")
        if not policy_hashes:
            raise _error("video_bundle_input_invalid", "effective_policy_hashes must not be empty")
        for value in policy_hashes.values():
            _require_digest(value, "effective_policy_hash")
        object.__setattr__(self, "effective_policy_hashes", policy_hashes)
        object.__setattr__(self, "usage", _snapshot_mapping(self.usage, "usage"))
        object.__setattr__(self, "redactions", _snapshot_mapping(self.redactions, "redactions"))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "steps", tuple(self.steps))
        if not self.steps or len(self.steps) > 32:
            raise _error(
                "video_bundle_input_invalid",
                "Receipt steps must be bounded and non-empty",
            )
        if any(not isinstance(step, StepAttemptSummary) for step in self.steps):
            raise _error("video_bundle_input_invalid", "Receipt step summary is invalid")
        if any(type(warning) is not str or not warning for warning in self.warnings):
            raise _error("video_bundle_input_invalid", "Receipt warnings are invalid")
        _require_executor_identity(self.model_identity, "model_identity")
        _require_executor_identity(self.transcriber_identity, "transcriber_identity")
        if frozenset(self.usage) - _USAGE_FIELDS or any(
            type(value) is not int or value < 0 for value in self.usage.values()
        ):
            raise _error(
                "video_bundle_sensitive_data",
                "Receipt usage contains unapproved fields or values",
            )
        if frozenset(self.redactions) != _REDACTION_FIELDS or any(
            self.redactions[field_name] not in allowed
            for field_name, allowed in _REDACTION_VALUES.items()
        ):
            raise _error(
                "video_bundle_sensitive_data",
                "Receipt redactions contain unapproved fields or values",
            )


@dataclass(frozen=True)
class DisplayAssetInput:
    artifact_id: str
    relative_path: str
    media_type: str
    payload: bytes
    artifact_type: str = "evidence.asset.v1"

    def __post_init__(self) -> None:
        _require_id(self.artifact_id, "art", "display_asset.artifact_id")
        path = _portable_path(self.relative_path)
        if not path.startswith("assets/"):
            raise _error("video_bundle_path_invalid", "Display assets must be stored under assets/")
        if not path.endswith(".webp"):
            raise _error("video_bundle_path_invalid", "Display assets must use a .webp path")
        if self.media_type != "image/webp" or self.artifact_type != "evidence.asset.v1":
            raise _error(
                "video_bundle_input_invalid",
                "Display asset type and media type are invalid",
            )
        if not isinstance(self.payload, bytes) or not self.payload:
            raise _error("video_bundle_input_invalid", "Display asset payload must not be empty")
        if not _is_webp(self.payload):
            raise _error("video_bundle_input_invalid", "Display asset payload is not valid WebP")
        object.__setattr__(self, "payload", bytes(self.payload))


@dataclass(frozen=True)
class VideoBundleInput:
    bundle_id: str
    created_at: str
    location: CandidateLocationCapabilityPort
    source: VideoSourceMetadata
    artifact_ids: VideoArtifactIds
    transcript: TranscriptDocument
    evidence_set: EvidenceSet
    quality: QualityOutcome
    receipt: ReceiptProvenance
    display_assets: tuple[DisplayAssetInput, ...] = ()

    def __post_init__(self) -> None:
        _require_id(self.bundle_id, "bnd", "bundle_id")
        _require_text(self.created_at, "created_at")
        if not callable(getattr(self.location, "begin", None)):
            raise _error("video_bundle_input_invalid", "location is invalid")
        if not isinstance(self.source, VideoSourceMetadata):
            raise _error("video_bundle_input_invalid", "source is invalid")
        if not isinstance(self.artifact_ids, VideoArtifactIds):
            raise _error("video_bundle_input_invalid", "artifact_ids are invalid")
        if not isinstance(self.transcript, TranscriptDocument):
            raise _error("video_bundle_input_invalid", "transcript is invalid")
        if not isinstance(self.evidence_set, EvidenceSet):
            raise _error("video_bundle_input_invalid", "evidence_set is invalid")
        if not isinstance(self.quality, QualityOutcome):
            raise _error("video_bundle_input_invalid", "quality is invalid")
        if not isinstance(self.receipt, ReceiptProvenance):
            raise _error("video_bundle_input_invalid", "receipt is invalid")
        object.__setattr__(self, "display_assets", tuple(self.display_assets))
        if any(not isinstance(asset, DisplayAssetInput) for asset in self.display_assets):
            raise _error("video_bundle_input_invalid", "display_assets are invalid")


@dataclass(frozen=True)
class CandidateBundle:
    location: CandidateBundleLocation
    bundle_id: str
    manifest_sha256: str
    artifact_ids: tuple[str, ...]

    @property
    def absolute_path(self) -> Path:
        return self.location.candidate_path

    @property
    def staging_relative_path(self) -> str:
        return self.location.staging_relative_path

    @property
    def target_area(self) -> str:
        return self.location.target_area


@dataclass(frozen=True)
class _ArtifactPayload:
    artifact_id: str
    artifact_type: str
    path: str
    media_type: str
    data: bytes
    charset: str | None = None


def _artifact_ref(bundle_id: str, payload: _ArtifactPayload) -> dict[str, str]:
    return {
        "bundle_id": bundle_id,
        "artifact_id": payload.artifact_id,
        "sha256": sha256_digest(payload.data),
    }


class BundleAssembler:
    def assemble(self, bundle_input: VideoBundleInput) -> CandidateBundle:
        if not isinstance(bundle_input, VideoBundleInput):
            raise _error("video_bundle_input_invalid", "Bundle input is invalid")
        self._validate_timestamps(bundle_input)
        payloads = self._build_payloads(bundle_input)
        self._validate_input_bindings(bundle_input, payloads)

        source_document, revision_document = self._build_source_documents(
            bundle_input,
            next(
                payload
                for payload in payloads
                if payload.artifact_id == bundle_input.artifact_ids.source_metadata
            ),
        )
        artifact_documents = self._build_artifact_documents(bundle_input, payloads)
        receipt_document = self._build_receipt(bundle_input, payloads)
        _validate_safe_value(source_document, "source")
        _validate_safe_value(revision_document, "source_revision")
        _validate_safe_value(receipt_document, "receipt")
        for segment in bundle_input.transcript.segments:
            _validate_safe_value(segment.text, "transcript.text")
        try:
            draft_text = bundle_input.quality.final_draft.decode("utf-8")
        except UnicodeError:
            raise _error(
                "video_bundle_input_invalid",
                "Final draft must be UTF-8 text",
            ) from None
        _validate_safe_value(draft_text, "draft")

        receipt_bytes = encode_json(receipt_document)
        manifest_document = self._build_manifest(
            bundle_input,
            source_document,
            revision_document,
            artifact_documents,
            payloads,
            receipt_bytes,
        )
        _validate_safe_value(manifest_document, "manifest")
        manifest_bytes = encode_json(manifest_document)

        writer = bundle_input.location.begin(bundle_input.receipt.job_id)
        try:
            for payload in payloads:
                writer.write_payload(payload.path, payload.data)
            writer.write_payload("receipt.json", receipt_bytes)
            location = writer.complete(manifest_bytes)
        finally:
            writer.close()
        return CandidateBundle(
            location=location,
            bundle_id=bundle_input.bundle_id,
            manifest_sha256=sha256_digest(manifest_bytes),
            artifact_ids=tuple(payload.artifact_id for payload in payloads),
        )

    @staticmethod
    def _validate_timestamps(bundle_input: VideoBundleInput) -> None:
        source = bundle_input.source
        receipt = bundle_input.receipt
        started = _parse_timestamp(receipt.started_at, "receipt.started_at")
        completed = _parse_timestamp(receipt.completed_at, "receipt.completed_at")
        created = _parse_timestamp(bundle_input.created_at, "created_at")
        observed = _parse_timestamp(source.observed_at, "source.observed_at")
        if not started <= completed <= created or observed > created:
            raise _error("video_bundle_input_invalid", "Bundle timestamps are not ordered")
        if source.published_at is not None:
            published = _parse_timestamp(source.published_at, "source.published_at")
            if published > observed:
                raise _error("video_bundle_input_invalid", "Source timestamps are not ordered")
        for step in receipt.steps:
            step_started = _parse_timestamp(step.started_at, "step.started_at")
            step_completed = _parse_timestamp(step.completed_at, "step.completed_at")
            if not started <= step_started <= step_completed <= completed:
                raise _error(
                    "video_bundle_input_invalid",
                    "Receipt step timestamps are not ordered",
                )

    @staticmethod
    def _build_payloads(bundle_input: VideoBundleInput) -> tuple[_ArtifactPayload, ...]:
        source = bundle_input.source
        metadata = {
            "source_metadata_schema_version": 1,
            "source_kind": "video",
            "source_id": source.source_id,
            "source_revision_id": source.source_revision_id,
            "connector": {"id": source.connector_id},
            "platform": source.platform,
            "stable_video_identity": source.stable_video_identity,
            "canonical_uri": source.canonical_uri,
            "title": source.title,
            "author": source.author,
            "channel": source.channel,
            "duration_ms": source.duration_ms,
            "published_at": source.published_at,
            "observed_at": source.observed_at,
            "language": source.language,
            "subtitle": {"acquisition_mode": source.subtitle_acquisition},
            "safe_source_link": source.source_link,
            "materialization": {
                "kind": "reference_only",
                "reason_code": source.materialization_reason,
            },
            "license": {
                "status": source.license,
                "archive_permission": "unknown",
            },
            "privacy": source.privacy,
            "freshness": {
                "kind": source.freshness,
                "observed_at": source.observed_at,
            },
            "extensions": {"alltonote.video:metadata": dict(source.extensions)},
        }
        _validate_safe_value(metadata, "source_metadata")
        transcript = build_transcript(
            source.source_revision_id,
            bundle_input.transcript.language,
            bundle_input.transcript.segments,
        )
        artifact_ids = bundle_input.artifact_ids
        payloads = [
            _ArtifactPayload(
                artifact_ids.source_metadata,
                "source.metadata.v1",
                "sources/video-metadata.json",
                "application/json",
                encode_json(metadata),
                "utf-8",
            ),
            _ArtifactPayload(
                artifact_ids.transcript,
                "evidence.transcript.v1",
                "evidence/transcript.jsonl",
                "application/x-ndjson",
                transcript,
                "utf-8",
            ),
            _ArtifactPayload(
                artifact_ids.evidence_set,
                "evidence.reference-set.v1",
                "evidence/evidence-set.jsonl",
                "application/x-ndjson",
                bundle_input.evidence_set.payload,
                "utf-8",
            ),
            _ArtifactPayload(
                artifact_ids.primary_draft,
                "knowledge.draft.markdown.v1",
                f"drafts/{artifact_ids.primary_draft}.md",
                "text/markdown",
                bundle_input.quality.final_draft,
                "utf-8",
            ),
            _ArtifactPayload(
                artifact_ids.quality_report,
                "quality.report.v1",
                f"quality/{artifact_ids.quality_report}.json",
                "application/json",
                bundle_input.quality.report.payload,
                "utf-8",
            ),
        ]
        payloads.extend(
            _ArtifactPayload(
                asset.artifact_id,
                asset.artifact_type,
                asset.relative_path,
                asset.media_type,
                asset.payload,
            )
            for asset in bundle_input.display_assets
        )
        for payload in payloads:
            _portable_path(payload.path)
        paths = [_portable_path_key(payload.path) for payload in payloads]
        ids = [payload.artifact_id for payload in payloads]
        if len(paths) != len(set(paths)) or len(ids) != len(set(ids)):
            raise _error("video_bundle_reference_invalid", "Artifact paths and IDs must be unique")
        return tuple(sorted(payloads, key=lambda payload: payload.path))

    @staticmethod
    def _validate_input_bindings(
        bundle_input: VideoBundleInput,
        payloads: tuple[_ArtifactPayload, ...],
    ) -> None:
        by_id = {payload.artifact_id: payload for payload in payloads}
        ids = bundle_input.artifact_ids
        transcript_payload = by_id[ids.transcript]
        evidence = bundle_input.evidence_set
        if (
            evidence.source_revision_id != bundle_input.source.source_revision_id
            or evidence.transcript != bundle_input.transcript
            or evidence.target_artifact_ref
            != PortableArtifactRef(
                bundle_input.bundle_id,
                ids.transcript,
                sha256_digest(transcript_payload.data),
            )
        ):
            raise _error(
                "video_bundle_reference_mismatch",
                "Transcript evidence binding is invalid",
            )
        rebuilt_evidence = build_evidence_set(
            bundle_input.bundle_id,
            bundle_input.source.source_revision_id,
            evidence.target_artifact_ref,
            bundle_input.transcript,
            evidence.citation_map,
        )
        if rebuilt_evidence.payload != evidence.payload:
            raise _error("video_bundle_reference_mismatch", "Evidence payload is inconsistent")
        quality = bundle_input.quality
        if quality.execution_error is not None:
            raise _error(
                "video_bundle_quality_execution_failed",
                "Quality evaluation did not complete successfully",
            )
        expected_subject = {
            "bundle_id": bundle_input.bundle_id,
            "artifact_id": ids.primary_draft,
            "sha256": sha256_digest(quality.final_draft),
        }
        check_documents: list[dict[str, object]] = []
        for check in quality.report.checks:
            document: dict[str, object] = {
                "id": check.check_id,
                "status": check.status,
            }
            if check.reason is not None:
                document["reason"] = check.reason
            check_documents.append(document)
        expected_report = encode_json(
            {
                "quality_report_schema_version": 1,
                "subject": expected_subject,
                "profile": {"id": "alltonote.video-course-note", "version": 1},
                "overall": quality.overall.value,
                "checks": check_documents,
                "method": {"kind": "deterministic"},
                "metrics": {"quality_repair_attempts": quality.repair_attempts},
                "messages": sorted(
                    check.check_id
                    for check in quality.report.checks
                    if check.status == "fail"
                ),
                "evidence_ids": list(quality.report.evidence_ids),
            }
        )
        expected_publish_eligible = quality.overall is not QualityOverall.FAIL
        if (
            quality.report.payload != expected_report
            or quality.report.subject_sha256 != expected_subject["sha256"]
            or quality.report.overall is not quality.overall
            or quality.publish_eligible is not expected_publish_eligible
            or type(quality.repair_attempts) is not int
            or not 0 <= quality.repair_attempts <= 1
        ):
            raise _error("video_bundle_quality_invalid", "Quality outcome is inconsistent")

    @staticmethod
    def _build_source_documents(
        bundle_input: VideoBundleInput,
        metadata_payload: _ArtifactPayload,
    ) -> tuple[dict[str, object], dict[str, object]]:
        source = bundle_input.source
        source_document = {
            "source_schema_version": 1,
            "source_id": source.source_id,
            "source_kind": "video",
            "canonical_identity": {
                "scheme": source.canonical_identity_scheme,
                "value": source.stable_video_identity,
            },
            "display": {
                "title": source.title,
                "author": source.author,
                "channel": source.channel,
            },
            "extensions": {
                "alltonote.video:source": {
                    "video_metadata_schema_version": 1,
                    "connector_id": source.connector_id,
                    "platform": source.platform,
                    "canonical_uri": source.canonical_uri,
                    "duration_ms": source.duration_ms,
                    "published_at": source.published_at,
                    "observed_at": source.observed_at,
                    "language": source.language,
                    "subtitle_acquisition": source.subtitle_acquisition,
                }
            },
        }
        revision_document = {
            "source_revision_schema_version": 1,
            "source_revision_id": source.source_revision_id,
            "source_ref": {
                "bundle_id": bundle_input.bundle_id,
                "source_id": source.source_id,
            },
            "captured_at": source.observed_at,
            "observed_revision": {
                "stable_video_identity": source.stable_video_identity,
                "observed_at": source.observed_at,
            },
            "content_digest": sha256_digest(metadata_payload.data),
            "materialization": {
                "kind": "reference_only",
                "reason_code": source.materialization_reason,
            },
            "license": {
                "status": source.license,
                "archive_permission": "unknown",
            },
            "privacy": source.privacy,
            "freshness": {
                "kind": source.freshness,
                "observed_at": source.observed_at,
            },
            "extensions": {"alltonote.video:revision": dict(source.extensions)},
        }
        return source_document, revision_document

    @staticmethod
    def _build_artifact_documents(
        bundle_input: VideoBundleInput,
        payloads: tuple[_ArtifactPayload, ...],
    ) -> list[dict[str, object]]:
        by_id = {payload.artifact_id: payload for payload in payloads}
        ids = bundle_input.artifact_ids
        source_revision_ref = {
            "bundle_id": bundle_input.bundle_id,
            "source_revision_id": bundle_input.source.source_revision_id,
        }
        transcript_ref = _artifact_ref(bundle_input.bundle_id, by_id[ids.transcript])
        draft_ref = _artifact_ref(bundle_input.bundle_id, by_id[ids.primary_draft])
        quality_ref = _artifact_ref(bundle_input.bundle_id, by_id[ids.quality_report])
        parents: dict[str, list[dict[str, str]]] = {
            ids.evidence_set: [transcript_ref],
            ids.quality_report: [draft_ref],
        }
        quality_refs: dict[str, list[dict[str, str]]] = {
            ids.primary_draft: [quality_ref],
        }
        capability = (
            f"{bundle_input.receipt.capability_id}@"
            f"{bundle_input.receipt.capability_version}"
        )
        documents: list[dict[str, object]] = []
        for payload in payloads:
            descriptor: dict[str, object] = {
                "representation": "bundle_file",
                "path": payload.path,
                "media_type": payload.media_type,
                "byte_length": len(payload.data),
                "sha256": sha256_digest(payload.data),
            }
            if payload.charset is not None:
                descriptor["charset"] = payload.charset
            documents.append(
                {
                    "artifact_schema_version": 1,
                    "artifact_id": payload.artifact_id,
                    "artifact_type": payload.artifact_type,
                    "payload": descriptor,
                    "created_at": bundle_input.created_at,
                    "parents": parents.get(payload.artifact_id, []),
                    "source_revision_refs": [source_revision_ref],
                    "generated_by": {"run_id": bundle_input.receipt.run_id},
                    "generation": {
                        "recipe": {
                            "id": bundle_input.receipt.recipe_id,
                            "version": bundle_input.receipt.recipe_version,
                        },
                        "capability": capability,
                    },
                    "quality_report_refs": quality_refs.get(payload.artifact_id, []),
                    "extensions": {},
                }
            )
        return documents

    @staticmethod
    def _build_receipt(
        bundle_input: VideoBundleInput,
        payloads: tuple[_ArtifactPayload, ...],
    ) -> dict[str, object]:
        provenance = bundle_input.receipt
        summary = {
            "job_id": provenance.job_id,
            "attempt_id": provenance.attempt_id,
            "steps": [
                {
                    "step_id": step.step_id,
                    "attempt": step.attempt,
                    "state": step.state,
                    "started_at": step.started_at,
                    "completed_at": step.completed_at,
                }
                for step in provenance.steps
            ],
            "effective_policy_hashes": dict(provenance.effective_policy_hashes),
            "retry_of_job_id": provenance.retry_of_job_id,
            "parent_run_id": provenance.parent_run_id,
            "warnings": list(provenance.warnings),
        }
        capability = f"{provenance.capability_id}@{provenance.capability_version}"
        return {
            "receipt_schema_version": 1,
            "run_id": provenance.run_id,
            "state": "succeeded",
            "started_at": provenance.started_at,
            "completed_at": provenance.completed_at,
            "recipe": {
                "id": provenance.recipe_id,
                "version": provenance.recipe_version,
            },
            "parameters": {
                "sha256": sha256_digest(encode_json(summary)),
                "summary": summary,
            },
            "inputs": [
                {
                    "bundle_id": bundle_input.bundle_id,
                    "source_revision_id": bundle_input.source.source_revision_id,
                }
            ],
            "outputs": [
                _artifact_ref(bundle_input.bundle_id, payload) for payload in payloads
            ],
            "capabilities": [capability],
            "executors": [
                {
                    "kind": "alltonote-runtime",
                    "product": "alltonote",
                    "version": provenance.runtime_version,
                    "portable_contract_id": provenance.portable_contract_id,
                },
                {"kind": "model", "identity": provenance.model_identity},
                {
                    "kind": "transcriber",
                    "identity": provenance.transcriber_identity,
                },
            ],
            "usage": dict(provenance.usage),
            "quality": {
                "overall": bundle_input.quality.overall.value,
                "publish_eligible": bundle_input.quality.publish_eligible,
                "repair_attempts": bundle_input.quality.repair_attempts,
            },
            "redactions": dict(provenance.redactions),
        }

    @staticmethod
    def _build_manifest(
        bundle_input: VideoBundleInput,
        source_document: dict[str, object],
        revision_document: dict[str, object],
        artifact_documents: list[dict[str, object]],
        payloads: tuple[_ArtifactPayload, ...],
        receipt_bytes: bytes,
    ) -> dict[str, object]:
        provenance = bundle_input.receipt
        ids = bundle_input.artifact_ids
        capability = f"{provenance.capability_id}@{provenance.capability_version}"
        return {
            "$schema": "urn:iwiki:portable:bundle:v1",
            "bundle_schema_version": 1,
            "bundle_id": bundle_input.bundle_id,
            "created_at": bundle_input.created_at,
            "producer": {
                "product": "alltonote",
                "runtime_version": provenance.runtime_version,
                "recipe": {
                    "id": provenance.recipe_id,
                    "version": provenance.recipe_version,
                },
                "capability": capability,
                "portable_contract_id": provenance.portable_contract_id,
            },
            "sources": [source_document],
            "source_revisions": [revision_document],
            "dependencies": [],
            "artifacts": artifact_documents,
            "outputs": {
                "primary_draft": ids.primary_draft,
                "transcript": ids.transcript,
                "evidence_set": ids.evidence_set,
                "quality_reports": [ids.quality_report],
                "source_snapshots": [ids.source_metadata],
                "display_assets": [
                    asset.artifact_id for asset in bundle_input.display_assets
                ],
            },
            "receipt": {
                "path": "receipt.json",
                "byte_length": len(receipt_bytes),
                "sha256": sha256_digest(receipt_bytes),
            },
            "required_contracts": [],
            "extensions": {
                "alltonote.video:bundle": {"video_bundle_schema_version": 1},
            },
        }

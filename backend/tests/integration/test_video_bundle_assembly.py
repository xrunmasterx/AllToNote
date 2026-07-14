from __future__ import annotations

import ast
import json
import os
import shutil
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from ctypes import wintypes

import pytest
from iwiki.workspace import open_workspace

from app.adapters.iwiki import portable_gateway as gateway_module
from app.adapters.iwiki.portable_gateway import IWikiPortableGateway
from app.core.domain.ids import sha256_digest
from app.core.domain.video import (
    GeneratedVideoDraft,
    QualityOverall,
    TranscriptDocument,
    TranscriptSegment,
)
from app.core.errors import DomainError, ErrorCategory, ErrorDetail
from app.core.portable.artifacts import PortableArtifactRef, build_transcript
from app.core.portable import bundle_assembler as assembler_module
from app.core.portable.bundle_assembler import (
    BundleAssembler,
    DisplayAssetInput,
    ReceiptProvenance,
    StepAttemptSummary,
    VideoArtifactIds,
    VideoBundleInput,
    VideoSourceMetadata,
)
from app.core.portable.evidence import build_evidence_set
from app.core.portable.jsonio import encode_json
from app.core.portable.quality import (
    QualityCheck,
    evaluate_video_draft,
)
from app.core.ports.portable import CandidateBundleLocation


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "workspace-v2"
BUNDLE_ID = "bnd_018f0000-0000-7000-8000-000000000101"
SOURCE_ID = "src_018f0000-0000-7000-8000-000000000102"
REVISION_ID = "rev_018f0000-0000-7000-8000-000000000103"
METADATA_ARTIFACT_ID = "art_018f0000-0000-7000-8000-000000000104"
TRANSCRIPT_ARTIFACT_ID = "art_018f0000-0000-7000-8000-000000000105"
EVIDENCE_ARTIFACT_ID = "art_018f0000-0000-7000-8000-000000000106"
DRAFT_ARTIFACT_ID = "art_018f0000-0000-7000-8000-000000000107"
QUALITY_ARTIFACT_ID = "art_018f0000-0000-7000-8000-000000000108"
EVIDENCE_ID = "ev_018f0000-0000-7000-8000-000000000109"
RUN_ID = "run_018f0000-0000-7000-8000-00000000010a"
JOB_ID = "job_018f0000-0000-7000-8000-00000000010b"
ATTEMPT_ID = "att_018f0000-0000-7000-8000-00000000010c"
STARTED_AT = "2026-07-14T01:02:03.000Z"
COMPLETED_AT = "2026-07-14T01:02:04.000Z"
CREATED_AT = "2026-07-14T01:02:05.000Z"
VALID_WEBP = bytes.fromhex(
    "524946461a000000574542505650384c0d0000002f00000010071011118888fe0700"
)


class _ExplodingMapping(Mapping[str, object]):
    def __init__(self, error: BaseException) -> None:
        self._error = error

    def __getitem__(self, key: str) -> object:
        raise self._error

    def __iter__(self) -> Iterator[str]:
        raise self._error

    def __len__(self) -> int:
        return 1


class _ExplodingIterable:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    def __iter__(self) -> Iterator[object]:
        raise self._error


class _TestCandidateWriter:
    def __init__(
        self,
        location: CandidateBundleLocation,
        *,
        primary_error: BaseException | None,
        close_error: BaseException | None,
    ) -> None:
        self._location = location
        self._primary_error = primary_error
        self._close_error = close_error

    def write_payload(self, relative_path: str, data: bytes) -> None:
        if self._primary_error is not None:
            raise self._primary_error

    def complete(self, manifest: bytes) -> CandidateBundleLocation:
        return self._location

    def close(self) -> None:
        if self._close_error is not None:
            raise self._close_error


class _TestCandidateCapability:
    def __init__(self, writer: _TestCandidateWriter) -> None:
        self._writer = writer

    def begin(self, job_id: str) -> _TestCandidateWriter:
        return self._writer


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    shutil.copytree(FIXTURE_ROOT, root)
    for relative in ("raw/common", "wiki/common", "wiki/personal", ".cache"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    return root


def _bundle_input(workspace_root: Path) -> VideoBundleInput:
    transcript = TranscriptDocument(
        language="zh-CN",
        segments=(
            TranscriptSegment(
                segment_id="seg_000001",
                start_ms=0,
                end_ms=1500,
                text="可移植工件必须能够独立验证。",
            ),
        ),
    )
    transcript_payload = build_transcript(
        REVISION_ID,
        transcript.language,
        transcript.segments,
    )
    transcript_ref = PortableArtifactRef(
        bundle_id=BUNDLE_ID,
        artifact_id=TRANSCRIPT_ARTIFACT_ID,
        sha256=sha256_digest(transcript_payload),
    )
    evidence_set = build_evidence_set(
        BUNDLE_ID,
        REVISION_ID,
        transcript_ref,
        transcript,
        {"seg_000001": EVIDENCE_ID},
    )
    draft = GeneratedVideoDraft(
        markdown=(
            "# 可移植视频笔记\n\n"
            "## 验证边界\n\n"
            f"Bundle 必须能够独立验证。[^{EVIDENCE_ID}]\n\n"
            f"[^{EVIDENCE_ID}]: 视频 00:00–00:01\n"
        ),
        cited_segment_ids=("seg_000001",),
        screenshot_requests=(),
        model_identity="openai/gpt-4.1-mini",
        usage={"input_tokens": 120, "output_tokens": 40},
        warnings=(),
    )
    quality = evaluate_video_draft(
        draft,
        evidence_set,
        draft_bundle_id=BUNDLE_ID,
        draft_artifact_id=DRAFT_ARTIFACT_ID,
    )
    assert quality.publish_eligible

    return VideoBundleInput(
        bundle_id=BUNDLE_ID,
        created_at=CREATED_AT,
        location=IWikiPortableGateway().candidate_location(
            workspace_root,
            local_instance_id="task10",
            nonce="nonce",
        ),
        source=VideoSourceMetadata(
            source_id=SOURCE_ID,
            source_revision_id=REVISION_ID,
            connector_id="youtube",
            platform="youtube",
            canonical_identity_scheme="youtube-video",
            stable_video_identity="portable101",
            canonical_uri="https://www.youtube.com/watch?v=portable101",
            title="Portable Bundle Contract",
            author="AllToNote",
            channel="AllToNote Engineering",
            duration_ms=1500,
            published_at="2026-07-13T00:00:00.000Z",
            observed_at=STARTED_AT,
            language="zh-CN",
            subtitle_acquisition="provided",
            source_link="https://www.youtube.com/watch?v=portable101",
            materialization_reason="remote_video_reference",
            license="unknown",
            privacy="personal",
            freshness="point_in_time",
        ),
        artifact_ids=VideoArtifactIds(
            source_metadata=METADATA_ARTIFACT_ID,
            transcript=TRANSCRIPT_ARTIFACT_ID,
            evidence_set=EVIDENCE_ARTIFACT_ID,
            primary_draft=DRAFT_ARTIFACT_ID,
            quality_report=QUALITY_ARTIFACT_ID,
        ),
        transcript=transcript,
        evidence_set=evidence_set,
        quality=quality,
        receipt=ReceiptProvenance(
            run_id=RUN_ID,
            job_id=JOB_ID,
            attempt_id=ATTEMPT_ID,
            started_at=STARTED_AT,
            completed_at=COMPLETED_AT,
            recipe_id="alltonote.video-course-note",
            recipe_version=1,
            capability_id="alltonote.video-source-bundle",
            capability_version="1.0.0",
            runtime_version="0.1.0",
            portable_contract_id="iwiki-portable-contract-v1",
            effective_policy_hashes={
                "generation": "sha256:" + "1" * 64,
                "redaction": "sha256:" + "2" * 64,
            },
            model_identity="openai/gpt-4.1-mini",
            transcriber_identity="provided-transcript",
            usage={"input_tokens": 120, "output_tokens": 40},
            warnings=(),
            redactions={
                "secrets": "omitted",
                "prompts": "hash_only",
                "provider_payloads": "omitted",
            },
            steps=(
                StepAttemptSummary(
                    step_id="transcribe",
                    attempt=1,
                    state="succeeded",
                    started_at=STARTED_AT,
                    completed_at=STARTED_AT,
                ),
                StepAttemptSummary(
                    step_id="assemble",
                    attempt=1,
                    state="succeeded",
                    started_at=COMPLETED_AT,
                    completed_at=COMPLETED_AT,
                ),
            ),
        ),
    )


def _candidate_path(workspace_root: Path) -> Path:
    workspace = open_workspace(workspace_root, writable=True)
    return (
        workspace.resolve_contract_path("raw_personal")
        / ".staging"
        / "task10"
        / f"{JOB_ID}.nonce"
        / "bundle.partial"
    )


def test_assembled_candidate_passes_real_semantic_validation(
    workspace_root: Path,
) -> None:
    candidate = BundleAssembler().assemble(_bundle_input(workspace_root))

    assert candidate.location.target_area == "raw_personal"
    assert not (candidate.location.candidate_path / "commit.json").exists()

    report = IWikiPortableGateway().validate_candidate(
        workspace_root,
        candidate.location.staging_relative_path,
    )

    assert report.valid
    assert report.bundle_id == BUNDLE_ID
    assert report.manifest_sha256 == candidate.manifest_sha256
    assert report.issues == ()


def _read_json(path: Path) -> dict[str, object]:
    document = json.loads(path.read_bytes())
    assert isinstance(document, dict)
    return document


def _relative_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_candidate_has_exact_canonical_manifest_receipt_and_payload_inventory(
    workspace_root: Path,
) -> None:
    bundle_input = _bundle_input(workspace_root)
    candidate = BundleAssembler().assemble(bundle_input)
    files = _relative_files(candidate.absolute_path)
    manifest = _read_json(candidate.absolute_path / "bundle.json")
    receipt = _read_json(candidate.absolute_path / "receipt.json")

    assert set(files) == {
        "bundle.json",
        "receipt.json",
        "sources/video-metadata.json",
        "evidence/transcript.jsonl",
        "evidence/evidence-set.jsonl",
        f"drafts/{DRAFT_ARTIFACT_ID}.md",
        f"quality/{QUALITY_ARTIFACT_ID}.json",
    }
    assert files["bundle.json"] == encode_json(manifest)
    assert files["receipt.json"] == encode_json(receipt)
    assert set(manifest) == {
        "$schema",
        "bundle_schema_version",
        "bundle_id",
        "created_at",
        "producer",
        "sources",
        "source_revisions",
        "dependencies",
        "artifacts",
        "outputs",
        "receipt",
        "required_contracts",
        "extensions",
    }
    assert manifest["producer"] == {
        "product": "alltonote",
        "runtime_version": "0.1.0",
        "recipe": {"id": "alltonote.video-course-note", "version": 1},
        "capability": "alltonote.video-source-bundle@1.0.0",
        "portable_contract_id": "iwiki-portable-contract-v1",
    }
    assert manifest["outputs"] == {
        "primary_draft": DRAFT_ARTIFACT_ID,
        "transcript": TRANSCRIPT_ARTIFACT_ID,
        "evidence_set": EVIDENCE_ARTIFACT_ID,
        "quality_reports": [QUALITY_ARTIFACT_ID],
        "source_snapshots": [METADATA_ARTIFACT_ID],
        "display_assets": [],
    }
    assert manifest["required_contracts"] == []
    assert manifest["extensions"] == {
        "alltonote.video:bundle": {"video_bundle_schema_version": 1}
    }

    metadata = _read_json(candidate.absolute_path / "sources/video-metadata.json")
    assert metadata == {
        "source_metadata_schema_version": 1,
        "source_kind": "video",
        "source_id": SOURCE_ID,
        "source_revision_id": REVISION_ID,
        "connector": {"id": "youtube"},
        "platform": "youtube",
        "stable_video_identity": "portable101",
        "canonical_uri": "https://www.youtube.com/watch?v=portable101",
        "title": "Portable Bundle Contract",
        "author": "AllToNote",
        "channel": "AllToNote Engineering",
        "duration_ms": 1500,
        "published_at": "2026-07-13T00:00:00.000Z",
        "observed_at": STARTED_AT,
        "language": "zh-CN",
        "subtitle": {"acquisition_mode": "provided"},
        "safe_source_link": "https://www.youtube.com/watch?v=portable101",
        "materialization": {
            "kind": "reference_only",
            "reason_code": "remote_video_reference",
        },
        "license": {"status": "unknown", "archive_permission": "unknown"},
        "privacy": "personal",
        "freshness": {"kind": "point_in_time", "observed_at": STARTED_AT},
        "extensions": {"alltonote.video:metadata": {}},
    }
    assert manifest["sources"] == [
        {
            "source_schema_version": 1,
            "source_id": SOURCE_ID,
            "source_kind": "video",
            "canonical_identity": {
                "scheme": "youtube-video",
                "value": "portable101",
            },
            "display": {
                "title": "Portable Bundle Contract",
                "author": "AllToNote",
                "channel": "AllToNote Engineering",
            },
            "extensions": {
                "alltonote.video:source": {
                    "video_metadata_schema_version": 1,
                    "connector_id": "youtube",
                    "platform": "youtube",
                    "canonical_uri": "https://www.youtube.com/watch?v=portable101",
                    "duration_ms": 1500,
                    "published_at": "2026-07-13T00:00:00.000Z",
                    "observed_at": STARTED_AT,
                    "language": "zh-CN",
                    "subtitle_acquisition": "provided",
                }
            },
        }
    ]
    revision = manifest["source_revisions"][0]
    assert revision == {
        "source_revision_schema_version": 1,
        "source_revision_id": REVISION_ID,
        "source_ref": {"bundle_id": BUNDLE_ID, "source_id": SOURCE_ID},
        "captured_at": STARTED_AT,
        "observed_revision": {
            "stable_video_identity": "portable101",
            "observed_at": STARTED_AT,
        },
        "content_digest": sha256_digest(files["sources/video-metadata.json"]),
        "materialization": {
            "kind": "reference_only",
            "reason_code": "remote_video_reference",
        },
        "license": {"status": "unknown", "archive_permission": "unknown"},
        "privacy": "personal",
        "freshness": {"kind": "point_in_time", "observed_at": STARTED_AT},
        "extensions": {"alltonote.video:revision": {}},
    }

    artifacts = manifest["artifacts"]
    assert [(item["artifact_type"], item["payload"]["path"]) for item in artifacts] == [
        ("knowledge.draft.markdown.v1", f"drafts/{DRAFT_ARTIFACT_ID}.md"),
        ("evidence.reference-set.v1", "evidence/evidence-set.jsonl"),
        ("evidence.transcript.v1", "evidence/transcript.jsonl"),
        ("quality.report.v1", f"quality/{QUALITY_ARTIFACT_ID}.json"),
        ("source.metadata.v1", "sources/video-metadata.json"),
    ]
    for artifact in artifacts:
        descriptor = artifact["payload"]
        payload = files[descriptor["path"]]
        assert descriptor["byte_length"] == len(payload)
        assert descriptor["sha256"] == sha256_digest(payload)
        assert artifact["generated_by"] == {"run_id": RUN_ID}
        assert artifact["source_revision_refs"] == [
            {"bundle_id": BUNDLE_ID, "source_revision_id": REVISION_ID}
        ]

    assert set(receipt) == {
        "receipt_schema_version",
        "run_id",
        "state",
        "started_at",
        "completed_at",
        "recipe",
        "parameters",
        "inputs",
        "outputs",
        "capabilities",
        "executors",
        "usage",
        "quality",
        "redactions",
    }
    assert receipt["run_id"] == RUN_ID
    assert receipt["parameters"]["summary"] == {
        "job_id": JOB_ID,
        "attempt_id": ATTEMPT_ID,
        "steps": [
            {
                "step_id": "transcribe",
                "attempt": 1,
                "state": "succeeded",
                "started_at": STARTED_AT,
                "completed_at": STARTED_AT,
            },
            {
                "step_id": "assemble",
                "attempt": 1,
                "state": "succeeded",
                "started_at": COMPLETED_AT,
                "completed_at": COMPLETED_AT,
            },
        ],
        "effective_policy_hashes": {
            "generation": "sha256:" + "1" * 64,
            "redaction": "sha256:" + "2" * 64,
        },
        "retry_of_job_id": None,
        "parent_run_id": None,
        "warnings": [],
    }
    assert receipt["capabilities"] == ["alltonote.video-source-bundle@1.0.0"]
    assert receipt["usage"] == {"input_tokens": 120, "output_tokens": 40}
    assert receipt["quality"] == {
        "overall": "pass",
        "publish_eligible": True,
        "repair_attempts": 0,
    }
    assert receipt["redactions"] == {
        "secrets": "omitted",
        "prompts": "hash_only",
        "provider_payloads": "omitted",
    }
    assert manifest["receipt"] == {
        "path": "receipt.json",
        "byte_length": len(files["receipt.json"]),
        "sha256": sha256_digest(files["receipt.json"]),
    }


def test_same_input_in_distinct_staging_directories_is_byte_deterministic(
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    first = BundleAssembler().assemble(_bundle_input(workspace_root))
    other_workspace = tmp_path / "other-workspace"
    shutil.copytree(FIXTURE_ROOT, other_workspace)
    for relative in ("raw/common", "wiki/common", "wiki/personal", ".cache"):
        (other_workspace / relative).mkdir(parents=True, exist_ok=True)
    second = BundleAssembler().assemble(_bundle_input(other_workspace))

    assert _relative_files(first.absolute_path) == _relative_files(second.absolute_path)
    assert first.manifest_sha256 == second.manifest_sha256


def test_prepare_and_commit_adds_iwiki_commit_seal_only_after_assembly(
    workspace_root: Path,
) -> None:
    candidate = BundleAssembler().assemble(_bundle_input(workspace_root))
    assert not (candidate.absolute_path / "commit.json").exists()
    gateway = IWikiPortableGateway()
    prepared = gateway.prepare_candidate(
        workspace_root,
        candidate.staging_relative_path,
        expected_bundle_id=candidate.bundle_id,
        expected_manifest_sha256=candidate.manifest_sha256,
    )

    result = gateway.commit_prepared(prepared)

    committed = workspace_root.joinpath(*result.relative_path.split("/"))
    assert (committed / "commit.json").is_file()
    assert sha256_digest((committed / "commit.json").read_bytes()) == result.commit_sha256
    assert not candidate.absolute_path.exists()


def test_gateway_minted_location_uses_raw_personal_contract_without_path_authority(
    workspace_root: Path,
) -> None:
    bundle_input = _bundle_input(workspace_root)

    assert not hasattr(bundle_input.location, "target_root")
    assert not hasattr(bundle_input.location, "candidate_path")
    assert not hasattr(bundle_input.location, "target_area")

    candidate = BundleAssembler().assemble(bundle_input)

    workspace = open_workspace(workspace_root, writable=True)
    assert candidate.absolute_path == _candidate_path(workspace_root)
    assert candidate.absolute_path.is_relative_to(
        workspace.resolve_contract_path("raw_personal")
    )
    assert not candidate.absolute_path.is_relative_to(
        workspace.resolve_contract_path("wiki_personal")
    )
    assert candidate.staging_relative_path == (
        f"raw/personal/.staging/task10/{JOB_ID}.nonce/bundle.partial"
    )


def test_assembler_rejects_existing_candidate_even_when_empty(workspace_root: Path) -> None:
    bundle_input = _bundle_input(workspace_root)
    _candidate_path(workspace_root).mkdir(parents=True)

    with pytest.raises(DomainError) as raised:
        BundleAssembler().assemble(bundle_input)

    assert raised.value.code == "video_bundle_candidate_exists"


@pytest.mark.parametrize(
    "invalid_component",
    ("../outside", "two/parts", ".", "", "CON", "COM\u00b9", "abc.", "e\u0301"),
)
def test_gateway_rejects_invalid_staging_identity_without_external_write(
    workspace_root: Path,
    tmp_path: Path,
    invalid_component: str,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(DomainError) as raised:
        IWikiPortableGateway().candidate_location(
            workspace_root,
            local_instance_id=invalid_component,
            nonce="nonce",
        )

    assert raised.value.code == "video_bundle_location_invalid"
    assert list(outside.iterdir()) == []


def test_gateway_rejects_dotted_nonce_before_any_staging_write(
    workspace_root: Path,
) -> None:
    target_root = open_workspace(workspace_root, writable=True).resolve_contract_path(
        "raw_personal"
    )
    before = tuple(target_root.rglob("*"))

    with pytest.raises(DomainError) as raised:
        IWikiPortableGateway().candidate_location(
            workspace_root,
            local_instance_id="task10",
            nonce="a.b",
        )

    assert raised.value.code == "video_bundle_location_invalid"
    assert tuple(target_root.rglob("*")) == before


def test_gateway_allows_dotted_local_instance_and_real_validator_accepts_bundle(
    workspace_root: Path,
) -> None:
    bundle_input = replace(
        _bundle_input(workspace_root),
        location=IWikiPortableGateway().candidate_location(
            workspace_root,
            local_instance_id="task.10",
            nonce="nonce",
        ),
    )

    candidate = BundleAssembler().assemble(bundle_input)

    assert IWikiPortableGateway().validate_candidate(
        workspace_root,
        candidate.staging_relative_path,
    ).valid


@pytest.mark.parametrize(
    "invalid_path",
    (
        "sources/CON",
        "sources/COM\u00b9.webp",
        "sources/abc.",
        "sources/e\u0301.webp",
        "sources/name:stream",
    ),
)
def test_writer_direct_call_enforces_portable_path_policy(
    workspace_root: Path,
    invalid_path: str,
) -> None:
    writer = IWikiPortableGateway().candidate_location(
        workspace_root,
        local_instance_id="task10",
        nonce="direct-invalid-path",
    ).begin(JOB_ID)

    try:
        with pytest.raises(DomainError) as raised:
            writer.write_payload(invalid_path, b"probe")
    finally:
        writer.close()

    assert raised.value.code == "video_bundle_location_invalid"


@pytest.mark.skipif(os.name != "nt", reason="Win32 API contract")
def test_windows_directory_apis_use_pointer_sized_handle_signatures() -> None:
    kernel32 = gateway_module._FileCandidateBundleWriter._kernel32()

    assert kernel32.CreateFileW.restype is wintypes.HANDLE
    assert kernel32.GetFileInformationByHandle.argtypes[0] is wintypes.HANDLE
    assert kernel32.GetFinalPathNameByHandleW.argtypes[0] is wintypes.HANDLE
    assert kernel32.FlushFileBuffers.argtypes == (wintypes.HANDLE,)
    assert kernel32.CloseHandle.argtypes == (wintypes.HANDLE,)
    assert kernel32.GetFileInformationByHandle.restype is wintypes.BOOL
    assert kernel32.GetFinalPathNameByHandleW.restype is wintypes.DWORD
    assert kernel32.FlushFileBuffers.restype is wintypes.BOOL
    assert kernel32.CloseHandle.restype is wintypes.BOOL


def test_assembler_rejects_symlinked_staging_parent(
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    bundle_input = _bundle_input(workspace_root)
    target = tmp_path / "outside"
    target.mkdir()
    target_root = open_workspace(workspace_root, writable=True).resolve_contract_path(
        "raw_personal"
    )
    link = target_root / ".staging" / "task10"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink creation unavailable: {error}")

    with pytest.raises(DomainError) as raised:
        BundleAssembler().assemble(bundle_input)

    assert raised.value.code == "video_bundle_location_invalid"
    assert list(target.iterdir()) == []


def test_candidate_writer_prevents_or_detects_parent_swap_without_external_write(
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    gateway = IWikiPortableGateway()
    capability = gateway.candidate_location(
        workspace_root,
        local_instance_id="task10",
        nonce="swap",
    )
    writer = capability.begin(JOB_ID)
    target_root = open_workspace(workspace_root, writable=True).resolve_contract_path(
        "raw_personal"
    )
    instance_root = target_root / ".staging" / "task10"
    outside = tmp_path / "outside"
    outside.mkdir()
    held_root = outside / "held"

    try:
        instance_root.rename(held_root)
    except OSError:
        assert os.name == "nt"
        writer.close()
    else:
        try:
            with pytest.raises(DomainError) as write_error:
                writer.write_payload("sources/probe.bin", b"probe")
            with pytest.raises(DomainError) as completion_error:
                writer.complete(b'{"completion":true}\n')
        finally:
            writer.close()
        assert write_error.value.code == "video_bundle_location_invalid"
        assert completion_error.value.code == "video_bundle_location_invalid"

    assert list(outside.rglob("probe.bin")) == []
    assert list(outside.rglob("bundle.json")) == []


def test_candidate_capability_and_writer_are_single_use_and_close_is_idempotent(
    workspace_root: Path,
) -> None:
    capability = IWikiPortableGateway().candidate_location(
        workspace_root,
        local_instance_id="task10",
        nonce="single-use",
    )
    writer = capability.begin(JOB_ID)
    writer.close()
    writer.close()

    with pytest.raises(DomainError, match="video_bundle_writer_closed"):
        writer.write_payload("sources/probe.bin", b"probe")
    with pytest.raises(DomainError, match="video_bundle_location_consumed"):
        capability.begin(JOB_ID)


@pytest.mark.parametrize("primary_kind", ("domain", "fatal"))
@pytest.mark.parametrize("close_kind", ("ordinary", "fatal"))
def test_assembler_close_never_masks_primary_error(
    workspace_root: Path,
    primary_kind: str,
    close_kind: str,
) -> None:
    primary: BaseException = (
        DomainError("primary_error", ErrorCategory.INVALID_REQUEST, "primary")
        if primary_kind == "domain"
        else MemoryError("primary fatal")
    )
    close_error: BaseException = (
        OSError("secondary close secret C:/private/close.log")
        if close_kind == "ordinary"
        else KeyboardInterrupt("secondary close fatal")
    )
    location = CandidateBundleLocation(
        workspace_root=workspace_root,
        candidate_path=workspace_root / "unused",
        staging_relative_path="raw/personal/unused",
        target_area="raw_personal",
    )
    writer = _TestCandidateWriter(
        location,
        primary_error=primary,
        close_error=close_error,
    )
    bundle_input = replace(
        _bundle_input(workspace_root),
        location=_TestCandidateCapability(writer),
    )

    with pytest.raises(type(primary)) as raised:
        BundleAssembler().assemble(bundle_input)

    assert raised.value is primary


def test_assembler_sanitizes_close_error_without_primary(
    workspace_root: Path,
) -> None:
    secret = "C:/private/close.log"
    location = CandidateBundleLocation(
        workspace_root=workspace_root,
        candidate_path=workspace_root / "unused",
        staging_relative_path="raw/personal/unused",
        target_area="raw_personal",
    )
    writer = _TestCandidateWriter(
        location,
        primary_error=None,
        close_error=OSError(f"close failed {secret}"),
    )
    bundle_input = replace(
        _bundle_input(workspace_root),
        location=_TestCandidateCapability(writer),
    )

    with pytest.raises(DomainError) as raised:
        BundleAssembler().assemble(bundle_input)

    assert raised.value.code == "video_bundle_write_failed"
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)


def test_assembler_preserves_fatal_close_error_without_primary(
    workspace_root: Path,
) -> None:
    fatal = MemoryError("fatal close")
    location = CandidateBundleLocation(
        workspace_root=workspace_root,
        candidate_path=workspace_root / "unused",
        staging_relative_path="raw/personal/unused",
        target_area="raw_personal",
    )
    writer = _TestCandidateWriter(
        location,
        primary_error=None,
        close_error=fatal,
    )
    bundle_input = replace(
        _bundle_input(workspace_root),
        location=_TestCandidateCapability(writer),
    )

    with pytest.raises(MemoryError) as raised:
        BundleAssembler().assemble(bundle_input)

    assert raised.value is fatal


def test_write_failure_is_sanitized_and_never_leaves_completion_marker(
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = str((workspace_root / "private" / "secret.bin").resolve())
    calls = 0
    real_fsync = os.fsync

    def fail_second_sync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(f"failed to sync {secret}")
        real_fsync(descriptor)

    monkeypatch.setattr(gateway_module.os, "fsync", fail_second_sync)

    with pytest.raises(DomainError) as raised:
        BundleAssembler().assemble(_bundle_input(workspace_root))

    assert raised.value.code == "video_bundle_write_failed"
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)
    assert not (_candidate_path(workspace_root) / "bundle.json").exists()


@pytest.mark.parametrize("operation", ("payload", "completion"))
@pytest.mark.parametrize("failure_kind", ("ordinary", "fatal"))
def test_fdopen_failure_closes_owned_descriptor_and_cleans_own_marker(
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    failure_kind: str,
) -> None:
    nonce = f"fdopen-{operation}-{failure_kind}"
    writer = IWikiPortableGateway().candidate_location(
        workspace_root,
        local_instance_id="task10",
        nonce=nonce,
    ).begin(JOB_ID)
    captured: list[int] = []
    failure: BaseException = (
        OSError("fdopen secret C:/private/descriptor.log")
        if failure_kind == "ordinary"
        else MemoryError("fatal fdopen failure")
    )

    def fail_fdopen(descriptor: int, mode: str):
        captured.append(descriptor)
        raise failure

    monkeypatch.setattr(gateway_module.os, "fdopen", fail_fdopen)
    candidate_path = (
        open_workspace(workspace_root, writable=True).resolve_contract_path("raw_personal")
        / ".staging"
        / "task10"
        / f"{JOB_ID}.{nonce}"
        / "bundle.partial"
    )

    try:
        expected = DomainError if failure_kind == "ordinary" else MemoryError
        with pytest.raises(expected) as raised:
            if operation == "payload":
                writer.write_payload("sources/probe.bin", b"probe")
            else:
                writer.complete(b'{"completion":true}\n')
        if failure_kind == "ordinary":
            assert raised.value.code == "video_bundle_write_failed"
            assert "C:/private" not in str(raised.value)
        else:
            assert raised.value is failure
        assert captured
        with pytest.raises(OSError):
            os.fstat(captured[0])
        if operation == "completion":
            assert not (candidate_path / "bundle.json").exists()
    finally:
        for descriptor in captured:
            try:
                os.close(descriptor)
            except OSError:
                pass
        writer.close()


def test_directory_sync_failure_removes_completion_marker(
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nonce = "directory-sync-failure"
    capability = IWikiPortableGateway().candidate_location(
        workspace_root,
        local_instance_id="task10",
        nonce=nonce,
    )
    writer = capability.begin(JOB_ID)
    writer.write_payload("sources/probe.bin", b"probe")
    secret = str((workspace_root / "private" / "secret.bin").resolve())

    def fail_directory_sync() -> None:
        raise OSError(f"failed to sync {secret}")

    monkeypatch.setattr(writer, "_sync_directories", fail_directory_sync)

    try:
        with pytest.raises(DomainError) as raised:
            writer.complete(b'{"completion":true}\n')
    finally:
        writer.close()

    candidate_path = (
        open_workspace(workspace_root, writable=True).resolve_contract_path("raw_personal")
        / ".staging"
        / "task10"
        / f"{JOB_ID}.{nonce}"
        / "bundle.partial"
    )
    assert raised.value.code == "video_bundle_write_failed"
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)
    assert not (candidate_path / "bundle.json").exists()


def test_completion_marker_collision_never_deletes_foreign_marker(
    workspace_root: Path,
) -> None:
    nonce = "foreign-completion-marker"
    writer = IWikiPortableGateway().candidate_location(
        workspace_root,
        local_instance_id="task10",
        nonce=nonce,
    ).begin(JOB_ID)
    candidate_path = (
        open_workspace(workspace_root, writable=True).resolve_contract_path("raw_personal")
        / ".staging"
        / "task10"
        / f"{JOB_ID}.{nonce}"
        / "bundle.partial"
    )
    foreign_marker = b'{"owner":"foreign"}\n'
    marker_path = candidate_path / "bundle.json"
    marker_path.write_bytes(foreign_marker)

    try:
        with pytest.raises(DomainError) as raised:
            writer.complete(b'{"owner":"writer"}\n')
    finally:
        writer.close()

    assert raised.value.code == "video_bundle_write_failed"
    assert marker_path.read_bytes() == foreign_marker


def test_completion_cleanup_failure_does_not_mask_primary_io_error(
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = IWikiPortableGateway().candidate_location(
        workspace_root,
        local_instance_id="task10",
        nonce="cleanup-precedence-io",
    ).begin(JOB_ID)

    def fail_directory_sync() -> None:
        raise OSError("primary sync failure")

    def fail_cleanup() -> None:
        raise MemoryError("secondary cleanup failure")

    monkeypatch.setattr(writer, "_sync_directories", fail_directory_sync)
    monkeypatch.setattr(writer, "_discard_completion_marker", fail_cleanup)

    try:
        with pytest.raises(DomainError) as raised:
            writer.complete(b'{"completion":true}\n')
    finally:
        writer.close()

    assert raised.value.code == "video_bundle_write_failed"


def test_completion_cleanup_failure_does_not_mask_primary_fatal_error(
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = IWikiPortableGateway().candidate_location(
        workspace_root,
        local_instance_id="task10",
        nonce="cleanup-precedence-fatal",
    ).begin(JOB_ID)

    def fail_directory_sync() -> None:
        raise MemoryError("primary fatal failure")

    def fail_cleanup() -> None:
        raise KeyboardInterrupt("secondary cleanup failure")

    monkeypatch.setattr(writer, "_sync_directories", fail_directory_sync)
    monkeypatch.setattr(writer, "_discard_completion_marker", fail_cleanup)

    try:
        with pytest.raises(MemoryError, match="primary fatal failure"):
            writer.complete(b'{"completion":true}\n')
    finally:
        writer.close()


def test_successful_assembly_syncs_every_file_before_return(
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    real_fsync = os.fsync

    def record_sync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        real_fsync(descriptor)

    monkeypatch.setattr(gateway_module.os, "fsync", record_sync)

    candidate = BundleAssembler().assemble(_bundle_input(workspace_root))

    assert calls >= 7
    assert (candidate.absolute_path / "bundle.json").is_file()


def test_assembler_rejects_absolute_path_in_portable_metadata(
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    bundle_input = _bundle_input(workspace_root)
    invalid = replace(
        bundle_input,
        source=replace(bundle_input.source, title=str(tmp_path.resolve())),
    )

    with pytest.raises(DomainError) as raised:
        BundleAssembler().assemble(invalid)

    assert raised.value.code == "video_bundle_sensitive_data"
    assert not _candidate_path(workspace_root).exists()


def test_assembler_rejects_forbidden_receipt_field_before_writing(
    workspace_root: Path,
) -> None:
    bundle_input = _bundle_input(workspace_root)
    invalid = replace(
        bundle_input,
        source=replace(
            bundle_input.source,
            extensions={"safe_container": {"api_key": "redacted"}},
        ),
    )

    with pytest.raises(DomainError) as raised:
        BundleAssembler().assemble(invalid)

    assert raised.value.code == "video_bundle_sensitive_data"
    assert not _candidate_path(workspace_root).exists()


@pytest.mark.parametrize(
    "forbidden_key",
    (
        "APIKey",
        "api-key",
        "FullPrompt",
        "providerRaw",
        "Provider-Raw-Response",
        "PID",
        "processID",
        "LeaseID",
        "fencingToken",
        "X-API-Key",
        "X-Auth-Token",
        "aws_secret_access_key",
    ),
)
def test_assembler_canonicalizes_forbidden_provenance_keys(
    workspace_root: Path,
    forbidden_key: str,
) -> None:
    bundle_input = _bundle_input(workspace_root)
    secret = "secret-value-must-not-leak"
    invalid = replace(
        bundle_input,
        source=replace(
            bundle_input.source,
            extensions={"safe_container": {forbidden_key: secret}},
        ),
    )

    with pytest.raises(DomainError) as raised:
        BundleAssembler().assemble(invalid)

    assert raised.value.code == "video_bundle_sensitive_data"
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)
    assert not _candidate_path(workspace_root).exists()


def test_provenance_key_matching_does_not_reject_unrelated_monkey_field(
    workspace_root: Path,
) -> None:
    bundle_input = _bundle_input(workspace_root)
    valid = replace(
        bundle_input,
        source=replace(
            bundle_input.source,
            extensions={"monkey": "banana"},
        ),
    )

    candidate = BundleAssembler().assemble(valid)

    assert IWikiPortableGateway().validate_candidate(
        workspace_root,
        candidate.staging_relative_path,
    ).valid


@pytest.mark.parametrize(
    "unsafe_text",
    (
        "diagnostic /home/alice/private.log failed",
        r"diagnostic C:\Users\alice\private.log failed",
        r"diagnostic \\server\share\private.log failed",
    ),
)
def test_assembler_rejects_embedded_absolute_paths_without_leaking_them(
    workspace_root: Path,
    unsafe_text: str,
) -> None:
    bundle_input = _bundle_input(workspace_root)
    invalid = replace(
        bundle_input,
        receipt=replace(bundle_input.receipt, warnings=(unsafe_text,)),
    )

    with pytest.raises(DomainError) as raised:
        BundleAssembler().assemble(invalid)

    assert raised.value.code == "video_bundle_sensitive_data"
    assert unsafe_text not in str(raised.value)
    assert unsafe_text not in repr(raised.value)


@pytest.mark.parametrize(
    "unsafe_url",
    (
        "file:///home/alice/video.mp4",
        r"\\server\share\video.mp4",
        "https://user:password@example.com/video",
        "https://example.com/video?token=secret",
        "https://example.com/video?sig=secret",
        "https://example.com/video?Signature=secret",
        "https://example.com/video?Expires=secret",
        "https://example.com/video?Policy=secret",
        "https://example.com/video?Key-Pair-Id=secret",
        "https://example.com/video?Access-Key-Id=secret",
        "https://example.com/video?AWSAccessKeyId=secret",
        "https://example.com/video?X-Amz-Credential=secret",
        "https://example.com/video?X-Goog-Signature=secret",
        "https://example.com/video?oauth_token=secret",
        "https://example.com/video?auth_token=secret",
    ),
)
def test_source_urls_reject_local_credentials_and_signed_queries(
    workspace_root: Path,
    unsafe_url: str,
) -> None:
    bundle_input = _bundle_input(workspace_root)
    secret = unsafe_url.rsplit("=", 1)[-1]

    with pytest.raises(DomainError) as raised:
        replace(bundle_input.source, canonical_uri=unsafe_url, source_link=unsafe_url)

    assert raised.value.code == "video_bundle_sensitive_data"
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)


@pytest.mark.parametrize(
    ("field_name", "unsafe_mapping"),
    (
        ("usage", {"input_tokens": 1, "raw_provider_cost": 2}),
        ("redactions", {"secrets": "omitted", "unknown": "value"}),
    ),
)
def test_receipt_rejects_unapproved_usage_and_redaction_fields(
    workspace_root: Path,
    field_name: str,
    unsafe_mapping: dict[str, object],
) -> None:
    bundle_input = _bundle_input(workspace_root)

    with pytest.raises(DomainError) as raised:
        replace(bundle_input.receipt, **{field_name: unsafe_mapping})

    assert raised.value.code == "video_bundle_sensitive_data"


@pytest.mark.parametrize(
    ("field_name", "unsafe_identity"),
    (
        ("model_identity", "openai/gpt?api_key=secret"),
        ("transcriber_identity", "provider raw response: secret"),
    ),
)
def test_receipt_rejects_unbounded_executor_identity(
    workspace_root: Path,
    field_name: str,
    unsafe_identity: str,
) -> None:
    bundle_input = _bundle_input(workspace_root)

    with pytest.raises(DomainError) as raised:
        replace(bundle_input.receipt, **{field_name: unsafe_identity})

    assert raised.value.code == "video_bundle_sensitive_data"
    assert "secret" not in str(raised.value)


def test_snapshot_mapping_sanitizes_plain_exceptions_without_secret_leak(
    workspace_root: Path,
) -> None:
    secret = "mapping-secret-never-print"
    bundle_input = _bundle_input(workspace_root)

    with pytest.raises(DomainError) as raised:
        replace(
            bundle_input.source,
            extensions=_ExplodingMapping(RuntimeError(secret)),
        )

    assert raised.value.code == "video_bundle_input_invalid"
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)


def test_snapshot_mapping_preserves_fatal_base_exception(workspace_root: Path) -> None:
    bundle_input = _bundle_input(workspace_root)

    with pytest.raises(KeyboardInterrupt):
        replace(
            bundle_input.source,
            extensions=_ExplodingMapping(KeyboardInterrupt()),
        )


@pytest.mark.parametrize("field_name", ("warnings", "steps", "display_assets"))
def test_tuple_snapshot_sanitizes_plain_iterable_exceptions(
    workspace_root: Path,
    field_name: str,
) -> None:
    secret = "iterable-secret-never-print"
    bundle_input = _bundle_input(workspace_root)
    exploding = _ExplodingIterable(RuntimeError(secret))

    with pytest.raises(DomainError) as raised:
        if field_name == "display_assets":
            replace(bundle_input, display_assets=exploding)
        else:
            replace(bundle_input.receipt, **{field_name: exploding})

    assert raised.value.code == "video_bundle_input_invalid"
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)


@pytest.mark.parametrize("fatal_type", (MemoryError, KeyboardInterrupt, SystemExit))
def test_tuple_snapshot_preserves_fatal_iterable_exceptions(
    workspace_root: Path,
    fatal_type: type[BaseException],
) -> None:
    bundle_input = _bundle_input(workspace_root)
    fatal = fatal_type("fatal iterable")

    with pytest.raises(fatal_type) as raised:
        replace(
            bundle_input.receipt,
            warnings=_ExplodingIterable(fatal),
        )

    assert raised.value is fatal


def test_assembler_rejects_absolute_path_in_final_draft_before_writing(
    workspace_root: Path,
) -> None:
    bundle_input = _bundle_input(workspace_root)
    draft = GeneratedVideoDraft(
        markdown=(
            "# 可移植视频笔记\n\n"
            "## 验证边界\n\n"
            f"Bundle 必须能够独立验证。[^{EVIDENCE_ID}]\n\n"
            "本地诊断路径为 C:\\private\\trace.log。\n\n"
            f"[^{EVIDENCE_ID}]: 视频 00:00–00:01\n"
        ),
        cited_segment_ids=("seg_000001",),
        screenshot_requests=(),
        model_identity="openai/gpt-4.1-mini",
        usage={"input_tokens": 120, "output_tokens": 40},
        warnings=(),
    )
    quality = evaluate_video_draft(
        draft,
        bundle_input.evidence_set,
        draft_bundle_id=BUNDLE_ID,
        draft_artifact_id=DRAFT_ARTIFACT_ID,
    )
    invalid = replace(bundle_input, quality=quality)

    with pytest.raises(DomainError) as raised:
        BundleAssembler().assemble(invalid)

    assert raised.value.code == "video_bundle_sensitive_data"
    assert not _candidate_path(workspace_root).exists()


def test_assembler_rejects_invalid_or_inverted_receipt_timestamps_before_writing(
    workspace_root: Path,
) -> None:
    bundle_input = _bundle_input(workspace_root)
    invalid = replace(
        bundle_input,
        created_at="not-a-timestamp",
        receipt=replace(bundle_input.receipt, completed_at=CREATED_AT),
    )

    with pytest.raises(DomainError) as raised:
        BundleAssembler().assemble(invalid)

    assert raised.value.code == "video_bundle_input_invalid"
    assert not _candidate_path(workspace_root).exists()


def test_display_asset_rejects_windows_reserved_path() -> None:
    with pytest.raises(DomainError) as raised:
        DisplayAssetInput(
            artifact_id="art_018f0000-0000-7000-8000-00000000010d",
            relative_path="assets/CON.webp",
            media_type="image/webp",
            payload=b"webp",
        )

    assert raised.value.code == "video_bundle_path_invalid"


@pytest.mark.parametrize(
    "relative_path",
    (
        "assets/COM¹.webp",
        "assets/lpt².webp",
        "assets/pre\u0301view.webp",
    ),
)
def test_display_asset_path_matches_task9_portable_device_and_control_policy(
    relative_path: str,
) -> None:
    with pytest.raises(DomainError) as raised:
        DisplayAssetInput(
            artifact_id="art_018f0000-0000-7000-8000-00000000010d",
            relative_path=relative_path,
            media_type="image/webp",
            payload=VALID_WEBP,
        )

    assert raised.value.code == "video_bundle_path_invalid"


def test_display_asset_path_keeps_pinned_percent_literal_policy() -> None:
    asset = DisplayAssetInput(
        artifact_id="art_018f0000-0000-7000-8000-00000000010d",
        relative_path="assets/preview%20literal.webp",
        media_type="image/webp",
        payload=VALID_WEBP,
    )

    assert asset.relative_path == "assets/preview%20literal.webp"


@pytest.mark.parametrize(
    "relative_path",
    [
        "C:/private/preview.webp",
        "/private/preview.webp",
        "assets/../preview.webp",
        "assets\\preview.webp",
        "assets/preview.webp.",
    ],
)
def test_display_asset_rejects_nonportable_path(relative_path: str) -> None:
    with pytest.raises(DomainError) as raised:
        DisplayAssetInput(
            artifact_id="art_018f0000-0000-7000-8000-00000000010d",
            relative_path=relative_path,
            media_type="image/webp",
            payload=b"webp",
        )

    assert raised.value.code == "video_bundle_path_invalid"


@pytest.mark.parametrize(
    ("changes", "error_code"),
    (
        ({"artifact_type": "image.preview.v1"}, "video_bundle_input_invalid"),
        ({"media_type": "image/png"}, "video_bundle_input_invalid"),
        ({"relative_path": "assets/preview.png"}, "video_bundle_path_invalid"),
        ({"payload": b"RIFF\x04\x00\x00\x00WEBP"}, "video_bundle_input_invalid"),
        ({"payload": b"not-a-webp"}, "video_bundle_input_invalid"),
    ),
)
def test_display_asset_is_exact_valid_webp_evidence_asset(
    changes: dict[str, object],
    error_code: str,
) -> None:
    values: dict[str, object] = {
        "artifact_id": "art_018f0000-0000-7000-8000-00000000010d",
        "relative_path": "assets/preview.webp",
        "media_type": "image/webp",
        "payload": VALID_WEBP,
    }
    values.update(changes)

    with pytest.raises(DomainError) as raised:
        DisplayAssetInput(**values)

    assert raised.value.code == error_code


def test_declared_display_asset_is_in_inventory_outputs_and_semantically_valid(
    workspace_root: Path,
) -> None:
    bundle_input = _bundle_input(workspace_root)
    asset = DisplayAssetInput(
        artifact_id="art_018f0000-0000-7000-8000-00000000010d",
        relative_path="assets/preview.webp",
        media_type="image/webp",
        payload=VALID_WEBP,
    )
    candidate = BundleAssembler().assemble(
        replace(bundle_input, display_assets=(asset,))
    )
    manifest = _read_json(candidate.absolute_path / "bundle.json")

    assert manifest["outputs"]["display_assets"] == [asset.artifact_id]
    assert (
        candidate.absolute_path / "assets" / "preview.webp"
    ).read_bytes() == VALID_WEBP
    assert IWikiPortableGateway().validate_candidate(
        workspace_root,
        candidate.staging_relative_path,
    ).valid


def test_assembler_rejects_duplicate_artifact_id(workspace_root: Path) -> None:
    bundle_input = _bundle_input(workspace_root)
    duplicate = DisplayAssetInput(
        artifact_id=DRAFT_ARTIFACT_ID,
        relative_path="assets/preview.webp",
        media_type="image/webp",
        payload=VALID_WEBP,
    )
    invalid = replace(bundle_input, display_assets=(duplicate,))

    with pytest.raises(DomainError) as raised:
        BundleAssembler().assemble(invalid)

    assert raised.value.code == "video_bundle_reference_invalid"
    assert not _candidate_path(workspace_root).exists()


def test_assembler_rejects_unicode_casefold_artifact_path_collision(
    workspace_root: Path,
) -> None:
    bundle_input = _bundle_input(workspace_root)
    assets = (
        DisplayAssetInput(
            artifact_id="art_018f0000-0000-7000-8000-00000000010d",
            relative_path="assets/straße.webp",
            media_type="image/webp",
            payload=VALID_WEBP,
        ),
        DisplayAssetInput(
            artifact_id="art_018f0000-0000-7000-8000-00000000010e",
            relative_path="assets/STRASSE.webp",
            media_type="image/webp",
            payload=VALID_WEBP,
        ),
    )

    with pytest.raises(DomainError) as raised:
        BundleAssembler().assemble(replace(bundle_input, display_assets=assets))

    assert raised.value.code == "video_bundle_reference_invalid"
    assert not _candidate_path(workspace_root).exists()


def test_quality_execution_error_fails_closed_before_writing(
    workspace_root: Path,
) -> None:
    bundle_input = _bundle_input(workspace_root)
    execution_error = ErrorDetail(
        code="quality_repair_failed",
        category=ErrorCategory.RECIPE_FAILED,
        message="repair failed at C:/private/secret.log",
    )
    invalid_quality = replace(
        bundle_input.quality,
        execution_error=execution_error,
        publish_eligible=False,
    )

    with pytest.raises(DomainError) as raised:
        BundleAssembler().assemble(replace(bundle_input, quality=invalid_quality))

    assert raised.value.code == "video_bundle_quality_execution_failed"
    assert "C:/private/secret.log" not in str(raised.value)
    assert not _candidate_path(workspace_root).exists()


@pytest.mark.parametrize(
    "mutation",
    (
        "profile",
        "method",
        "metrics",
        "checks",
        "publish_eligible",
        "repair_attempts",
    ),
)
def test_quality_outcome_rejects_mutated_typed_or_payload_fields(
    workspace_root: Path,
    mutation: str,
) -> None:
    bundle_input = _bundle_input(workspace_root)
    quality = bundle_input.quality
    if mutation in {"publish_eligible", "repair_attempts"}:
        quality = replace(
            quality,
            **{
                mutation: (
                    not quality.publish_eligible
                    if mutation == "publish_eligible"
                    else quality.repair_attempts + 1
                )
            },
        )
    else:
        document = json.loads(quality.report.payload)
        if mutation == "profile":
            document["profile"] = {"id": "untrusted", "version": 1}
        elif mutation == "method":
            document["method"] = {"kind": "model-self-report"}
        elif mutation == "metrics":
            document["metrics"] = {"quality_repair_attempts": 99}
        else:
            document["checks"] = []
        quality = replace(
            quality,
            report=replace(quality.report, payload=encode_json(document)),
        )

    with pytest.raises(DomainError) as raised:
        BundleAssembler().assemble(replace(bundle_input, quality=quality))

    assert raised.value.code == "video_bundle_quality_invalid"
    assert not _candidate_path(workspace_root).exists()


def test_quality_outcome_rejects_self_consistent_fabricated_assessment(
    workspace_root: Path,
) -> None:
    bundle_input = _bundle_input(workspace_root)
    quality = bundle_input.quality
    made_up_check = QualityCheck("made_up_check", "pass")
    document = json.loads(quality.report.payload)
    document["checks"] = [{"id": "made_up_check", "status": "pass"}]
    document["messages"] = []
    document["evidence_ids"] = []
    forged = replace(
        quality,
        report=replace(
            quality.report,
            checks=(made_up_check,),
            evidence_ids=(),
            payload=encode_json(document),
        ),
    )

    with pytest.raises(DomainError) as raised:
        BundleAssembler().assemble(replace(bundle_input, quality=forged))

    assert raised.value.code == "video_bundle_quality_invalid"
    assert not _candidate_path(workspace_root).exists()


def test_policy_quality_failure_without_execution_error_remains_representable(
    workspace_root: Path,
) -> None:
    bundle_input = _bundle_input(workspace_root)
    failing_draft = GeneratedVideoDraft(
        markdown=(
            "# Incomplete note\n\n"
            "## Unsupported claim\n\n"
            "This substantive section has no evidence citation.\n"
        ),
        cited_segment_ids=(),
        screenshot_requests=(),
        model_identity="openai/gpt-4.1-mini",
        usage={"input_tokens": 1, "output_tokens": 1},
        warnings=(),
    )
    quality = evaluate_video_draft(
        failing_draft,
        bundle_input.evidence_set,
        draft_bundle_id=BUNDLE_ID,
        draft_artifact_id=DRAFT_ARTIFACT_ID,
    )
    assert quality.overall is QualityOverall.FAIL
    assert quality.execution_error is None

    candidate = BundleAssembler().assemble(replace(bundle_input, quality=quality))

    assert (candidate.absolute_path / "bundle.json").is_file()


def test_assembler_rejects_mutated_evidence_reference_before_writing(
    workspace_root: Path,
) -> None:
    bundle_input = _bundle_input(workspace_root)
    invalid_evidence = replace(
        bundle_input.evidence_set,
        target_artifact_ref=replace(
            bundle_input.evidence_set.target_artifact_ref,
            sha256="sha256:" + "0" * 64,
        ),
    )
    invalid = replace(bundle_input, evidence_set=invalid_evidence)

    with pytest.raises(DomainError) as raised:
        BundleAssembler().assemble(invalid)

    assert raised.value.code == "video_bundle_reference_mismatch"
    assert not _candidate_path(workspace_root).exists()


def test_real_validator_rejects_duplicate_output_mutation(workspace_root: Path) -> None:
    candidate = BundleAssembler().assemble(_bundle_input(workspace_root))
    manifest_path = candidate.absolute_path / "bundle.json"
    manifest = _read_json(manifest_path)
    manifest["outputs"]["quality_reports"] = [QUALITY_ARTIFACT_ID, QUALITY_ARTIFACT_ID]
    manifest_path.write_bytes(encode_json(manifest))

    report = IWikiPortableGateway().validate_candidate(
        workspace_root,
        candidate.staging_relative_path,
    )

    assert not report.valid
    assert any(issue.code == "unsupported_schema" for issue in report.issues)


def test_real_validator_rejects_payload_hash_mutation(workspace_root: Path) -> None:
    candidate = BundleAssembler().assemble(_bundle_input(workspace_root))
    draft_path = candidate.absolute_path / "drafts" / f"{DRAFT_ARTIFACT_ID}.md"
    draft_path.write_bytes(draft_path.read_bytes() + b"mutation\n")

    report = IWikiPortableGateway().validate_candidate(
        workspace_root,
        candidate.staging_relative_path,
    )

    assert not report.valid
    assert any(issue.code == "hash_mismatch" for issue in report.issues)


def test_json_documents_recursively_exclude_sensitive_fields_and_absolute_paths(
    workspace_root: Path,
) -> None:
    candidate = BundleAssembler().assemble(_bundle_input(workspace_root))
    forbidden = {
        "api_key",
        "authorization",
        "cookie",
        "credential",
        "full_prompt",
        "password",
        "pid",
        "provider_raw_request",
        "provider_raw_response",
        "provider_request_id",
        "lease",
        "fencing_token",
    }

    def inspect(value: object) -> None:
        if isinstance(value, dict):
            assert not (set(key.lower() for key in value) & forbidden)
            for item in value.values():
                inspect(item)
        elif isinstance(value, list):
            for item in value:
                inspect(item)
        elif isinstance(value, str):
            assert not Path(value).is_absolute()
            assert not (len(value) >= 3 and value[1:3] in {":\\", ":/"})
            assert not value.lower().startswith("file:")

    for relative in ("bundle.json", "receipt.json", "sources/video-metadata.json"):
        inspect(_read_json(candidate.absolute_path / relative))


def test_core_assembler_has_no_iwiki_framework_legacy_or_target_path_dependency() -> None:
    source_path = Path(assembler_module.__file__)
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    forbidden_roots = (
        "iwiki",
        "fastapi",
        "app.adapters",
        "app.routers",
        "app.services",
        "app.downloaders",
        "app.transcriber",
        "app.gpt",
        "app.db",
    )

    assert not any(
        imported == root or imported.startswith(root + ".")
        for imported in imports
        for root in forbidden_roots
    )
    assert "raw/personal" not in source.replace("\\", "/")

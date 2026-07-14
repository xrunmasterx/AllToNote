from __future__ import annotations

import ast
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from iwiki.workspace import open_workspace

from app.adapters.iwiki.portable_gateway import IWikiPortableGateway
from app.core.domain.ids import sha256_digest
from app.core.domain.video import GeneratedVideoDraft, TranscriptDocument, TranscriptSegment
from app.core.errors import DomainError
from app.core.portable.artifacts import PortableArtifactRef, build_transcript
from app.core.portable import bundle_assembler as assembler_module
from app.core.portable.bundle_assembler import (
    BundleAssembler,
    CandidateLocation,
    DisplayAssetInput,
    ReceiptProvenance,
    StepAttemptSummary,
    VideoArtifactIds,
    VideoBundleInput,
    VideoSourceMetadata,
)
from app.core.portable.evidence import build_evidence_set
from app.core.portable.jsonio import encode_json
from app.core.portable.quality import evaluate_video_draft


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


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    shutil.copytree(FIXTURE_ROOT, root)
    for relative in ("raw/common", "wiki/common", "wiki/personal", ".cache"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    return root


def _bundle_input(workspace_root: Path) -> VideoBundleInput:
    workspace = open_workspace(workspace_root, writable=True)
    target_root = workspace.resolve_contract_path("raw_personal")
    candidate_path = (
        target_root
        / ".staging"
        / "task10"
        / "job.nonce"
        / "bundle.partial"
    )
    candidate_path.parent.mkdir(parents=True)

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
        location=CandidateLocation(
            workspace_root=workspace_root,
            target_root=target_root,
            candidate_path=candidate_path,
            staging_relative_path=workspace.relative(candidate_path),
            target_area="raw_personal",
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


@pytest.mark.parametrize("target_area", ["raw_common", "wiki_personal", "wiki_common"])
def test_assembler_rejects_every_non_personal_raw_target(
    workspace_root: Path,
    target_area: str,
) -> None:
    bundle_input = _bundle_input(workspace_root)
    invalid = replace(
        bundle_input,
        location=replace(bundle_input.location, target_area=target_area),
    )

    with pytest.raises(DomainError) as raised:
        BundleAssembler().assemble(invalid)

    assert raised.value.code == "video_bundle_location_invalid"
    assert not bundle_input.location.candidate_path.exists()


def test_assembler_rejects_existing_candidate_even_when_empty(workspace_root: Path) -> None:
    bundle_input = _bundle_input(workspace_root)
    bundle_input.location.candidate_path.mkdir()

    with pytest.raises(DomainError) as raised:
        BundleAssembler().assemble(bundle_input)

    assert raised.value.code == "video_bundle_candidate_exists"


def test_assembler_rejects_candidate_escape_without_external_write(
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    bundle_input = _bundle_input(workspace_root)
    escaped = tmp_path / "outside" / "bundle.partial"
    escaped.parent.mkdir()
    invalid = replace(
        bundle_input,
        location=replace(bundle_input.location, candidate_path=escaped),
    )

    with pytest.raises(DomainError) as raised:
        BundleAssembler().assemble(invalid)

    assert raised.value.code == "video_bundle_location_invalid"
    assert list(escaped.parent.iterdir()) == []


def test_assembler_rejects_symlinked_staging_parent(
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    bundle_input = _bundle_input(workspace_root)
    target = tmp_path / "outside"
    target.mkdir()
    link = bundle_input.location.target_root / ".staging" / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink creation unavailable: {error}")
    candidate_path = link / "bundle.partial"
    staging = candidate_path.relative_to(workspace_root).as_posix()
    invalid = replace(
        bundle_input,
        location=replace(
            bundle_input.location,
            candidate_path=candidate_path,
            staging_relative_path=staging,
        ),
    )

    with pytest.raises(DomainError) as raised:
        BundleAssembler().assemble(invalid)

    assert raised.value.code == "video_bundle_location_invalid"
    assert list(target.iterdir()) == []


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
    assert not bundle_input.location.candidate_path.exists()


def test_assembler_rejects_forbidden_receipt_field_before_writing(
    workspace_root: Path,
) -> None:
    bundle_input = _bundle_input(workspace_root)
    invalid = replace(
        bundle_input,
        receipt=replace(bundle_input.receipt, redactions={"api_key": "redacted"}),
    )

    with pytest.raises(DomainError) as raised:
        BundleAssembler().assemble(invalid)

    assert raised.value.code == "video_bundle_sensitive_data"
    assert not bundle_input.location.candidate_path.exists()


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
    assert not bundle_input.location.candidate_path.exists()


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
    assert not bundle_input.location.candidate_path.exists()


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


def test_declared_display_asset_is_in_inventory_outputs_and_semantically_valid(
    workspace_root: Path,
) -> None:
    bundle_input = _bundle_input(workspace_root)
    asset = DisplayAssetInput(
        artifact_id="art_018f0000-0000-7000-8000-00000000010d",
        relative_path="assets/preview.webp",
        media_type="image/webp",
        payload=b"webp",
    )
    candidate = BundleAssembler().assemble(
        replace(bundle_input, display_assets=(asset,))
    )
    manifest = _read_json(candidate.absolute_path / "bundle.json")

    assert manifest["outputs"]["display_assets"] == [asset.artifact_id]
    assert (candidate.absolute_path / "assets" / "preview.webp").read_bytes() == b"webp"
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
        payload=b"webp",
    )
    invalid = replace(bundle_input, display_assets=(duplicate,))

    with pytest.raises(DomainError) as raised:
        BundleAssembler().assemble(invalid)

    assert raised.value.code == "video_bundle_reference_invalid"
    assert not bundle_input.location.candidate_path.exists()


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
    assert not bundle_input.location.candidate_path.exists()


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

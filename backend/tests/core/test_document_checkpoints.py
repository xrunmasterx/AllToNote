from __future__ import annotations

import json

from app.core.application.document_checkpoints import DocumentCandidateCheckpoint


def test_legacy_document_candidate_checkpoint_preserves_legacy_identity_mode() -> None:
    payload = json.dumps(
        {
            "schema_version": 1,
            "staging_relative_path": "raw/personal/.staging/candidate",
            "bundle_id": "bnd_0198c000-0000-7000-8000-000000000001",
            "manifest_sha256": "sha256:" + "a" * 64,
            "run_id": "run_0198c000-0000-7000-8000-000000000002",
            "source_id": "src_0198c000-0000-7000-8000-000000000003",
            "source_revision_id": "rev_0198c000-0000-7000-8000-000000000004",
            "artifacts": {"primary_draft": "art_0198c000-0000-7000-8000-000000000005"},
            "quality_overall": "pass",
            "publish_eligible": False,
            "usage": {"pages": 1, "blocks": 1},
            "warnings": [],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    checkpoint = DocumentCandidateCheckpoint.decode(payload)

    assert checkpoint.source_identity_connector_id is None
    assert checkpoint.source_canonical_identity is None
    assert checkpoint.encode() == payload

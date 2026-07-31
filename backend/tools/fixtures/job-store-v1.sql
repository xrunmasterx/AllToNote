PRAGMA foreign_keys = ON;
BEGIN;

CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    request_json TEXT,
    principal TEXT NOT NULL,
    client_request_id TEXT,
    state TEXT NOT NULL,
    cancellation_requested INTEGER NOT NULL
        CHECK (cancellation_requested IN (0, 1)),
    retry_of_job_id TEXT REFERENCES jobs(job_id),
    result_json TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(principal, client_request_id)
);

CREATE TABLE steps (
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    step_id TEXT NOT NULL,
    step_name TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY(job_id, step_id)
);

CREATE TABLE attempts (
    attempt_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    state TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(job_id, attempt_id),
    UNIQUE(job_id, step_id, attempt_id),
    FOREIGN KEY(job_id, step_id) REFERENCES steps(job_id, step_id) ON DELETE CASCADE
);

CREATE TABLE events (
    event_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(job_id, sequence)
);

CREATE TABLE challenges (
    challenge_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    attempt_id TEXT,
    state TEXT NOT NULL,
    prompt_json TEXT NOT NULL,
    response_json TEXT,
    response_hash TEXT,
    response_attempt_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(job_id, attempt_id) REFERENCES attempts(job_id, attempt_id),
    FOREIGN KEY(job_id, response_attempt_id)
        REFERENCES attempts(job_id, attempt_id)
);

CREATE TABLE external_operations (
    operation_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    step_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    operation_idempotency_key TEXT,
    provider_request_id TEXT,
    outcome TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(job_id, step_id, attempt_id)
        REFERENCES attempts(job_id, step_id, attempt_id)
);

CREATE TABLE checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    step_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    schema_id TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    output_hash TEXT NOT NULL,
    byte_length INTEGER NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(job_id, step_id, attempt_id)
        REFERENCES attempts(job_id, step_id, attempt_id)
);

CREATE TABLE leases (
    lease_name TEXT PRIMARY KEY CHECK (lease_name = 'scheduler'),
    job_id TEXT REFERENCES jobs(job_id) ON DELETE CASCADE,
    owner TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
);

CREATE TABLE source_identities (
    connector_id TEXT NOT NULL,
    canonical_identity TEXT NOT NULL,
    source_id TEXT NOT NULL,
    owning_bundle_id TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(connector_id, canonical_identity)
);

INSERT INTO jobs (
    job_id,
    request_hash,
    request_json,
    principal,
    client_request_id,
    state,
    cancellation_requested,
    retry_of_job_id,
    result_json,
    error_json,
    created_at,
    updated_at
) VALUES (
    'job_legacy_release_fixture',
    'sha256:b6cdcfaca0099ffe78c31f3f33e1b98a00b124eb4d5d0126bb5dfcc5f8691e85',
    '{"recipe_id":"alltonote.video-course-note","recipe_version":2}',
    'local',
    'legacy-release-fixture',
    'succeeded',
    0,
    NULL,
    '{"bundle_id":"bnd_018cc251-f400-7000-8000-000000000005","commit_sha256":"sha256:2222222222222222222222222222222222222222222222222222222222222222","display_asset_ids":[],"evidence_set_artifact_id":"art_018cc251-f400-7000-8000-000000000009","idempotent":false,"job_id":"job_legacy_release_fixture","manifest_sha256":"sha256:1111111111111111111111111111111111111111111111111111111111111111","primary_draft_artifact_id":"art_018cc251-f400-7000-8000-000000000010","publish_eligible":true,"quality_overall":"pass","quality_report_artifact_id":"art_018cc251-f400-7000-8000-000000000011","run_id":"run_018cc251-f400-7000-8000-000000000004","source_id":"src_018cc251-f400-7000-8000-000000000006","source_revision_id":"rev_018cc251-f400-7000-8000-000000000007","transcript_artifact_id":"art_018cc251-f400-7000-8000-000000000008","usage":{"model_calls":2},"warnings":["legacy-release-fixture"],"workspace_relative_bundle_path":"raw/personal/bundles/bnd_018cc251-f400-7000-8000-000000000005"}',
    NULL,
    '1',
    '1'
);

INSERT INTO steps (job_id, step_id, step_name, ordinal) VALUES (
    'job_legacy_release_fixture',
    'publish',
    'Publish portable bundle',
    1
);

INSERT INTO attempts (
    attempt_id,
    job_id,
    step_id,
    state,
    fencing_token,
    created_at,
    updated_at
) VALUES (
    'att_legacy_release_fixture',
    'job_legacy_release_fixture',
    'publish',
    'succeeded',
    1,
    '1',
    '1'
);

INSERT INTO events (
    event_id,
    job_id,
    sequence,
    event_type,
    payload_json,
    created_at
) VALUES (
    'evt_legacy_release_fixture',
    'job_legacy_release_fixture',
    1,
    'portable.commit.completed.v1',
    '{"bundle_id":"bnd_018cc251-f400-7000-8000-000000000005"}',
    '1'
);

INSERT INTO checkpoints (
    checkpoint_id,
    job_id,
    step_id,
    attempt_id,
    relative_path,
    schema_id,
    input_hash,
    output_hash,
    byte_length,
    metadata_json,
    created_at
) VALUES (
    'chk_legacy_release_fixture',
    'job_legacy_release_fixture',
    'publish',
    'att_legacy_release_fixture',
    'attempts/att_legacy_release_fixture/checkpoints/portable.json',
    'alltonote.portable-commit.v1',
    'sha256:3333333333333333333333333333333333333333333333333333333333333333',
    'sha256:4444444444444444444444444444444444444444444444444444444444444444',
    42,
    '{"fixture":"legacy-release"}',
    '1'
);

INSERT INTO source_identities (
    connector_id,
    canonical_identity,
    source_id,
    owning_bundle_id,
    manifest_sha256,
    updated_at
) VALUES (
    'fixture',
    'legacy-release-source',
    'src_018cc251-f400-7000-8000-000000000006',
    'bnd_018cc251-f400-7000-8000-000000000005',
    'sha256:1111111111111111111111111111111111111111111111111111111111111111',
    '1'
);

PRAGMA user_version = 1;
COMMIT;

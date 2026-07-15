from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from app.adapters.jobs.file_attempt_storage import FileAttemptStorage
from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.core.application import video_acquisition
from app.core.domain.ids import sha256_digest
from app.core.domain.video import JobState
from app.core.errors import DomainError


class _Token:
    def raise_if_cancelled(self) -> None:
        return None


def _asset_api() -> tuple[type, object]:
    asset_type = getattr(video_acquisition, "AttemptStoredAsset", None)
    role_type = getattr(video_acquisition, "StoredAssetRole", None)
    assert isinstance(asset_type, type), "AttemptStoredAsset is missing"
    assert isinstance(role_type, type), "StoredAssetRole is missing"
    return asset_type, role_type


def _running_attempt(
    repository: SqliteJobRepository,
    *,
    owner: str = "storage-owner",
    ttl_seconds: int = 300,
) -> tuple[object, object, object]:
    job = repository.create_job(
        request_hash=sha256_digest(b"request"),
        principal="local-user",
        client_request_id=None,
    )
    repository.transition_job(job.job_id, JobState.RUNNING)
    authority = repository.acquire_scheduler_lease(owner, ttl_seconds=ttl_seconds)
    pending = repository.create_attempt(job.job_id, "acquire")
    attempt = repository.start_attempt(pending.attempt_id, authority)
    return job, attempt, authority


def _storage(root: Path, repository: SqliteJobRepository) -> FileAttemptStorage:
    return FileAttemptStorage(root, repository, validators={})


def test_snapshot_asset_requires_live_authority_and_binds_owner(tmp_path: Path) -> None:
    now = [1_000]
    repository = SqliteJobRepository.open(tmp_path / "repository", clock=lambda: now[0])
    storage = _storage(tmp_path / "attempts", repository)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"local-media")
    job, attempt, old_authority = _running_attempt(
        repository, owner="old-owner", ttl_seconds=1
    )
    now[0] = 2_001
    repository.acquire_scheduler_lease("new-owner", ttl_seconds=300)
    _, role_type = _asset_api()

    with pytest.raises(DomainError) as caught:
        storage.snapshot_asset(
            source,
            job_id=job.job_id,
            attempt_id=attempt.attempt_id,
            role=role_type.SOURCE_MEDIA,
            expected_sha256=sha256_digest(source.read_bytes()),
            authority=old_authority,
            token=_Token(),
        )

    assert caught.value.code == "attempt_fenced"
    assert not tuple(storage.root.rglob("source_media.*"))


def test_resolve_asset_rejects_cross_job_and_cross_attempt_substitution(
    tmp_path: Path,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "repository")
    storage = _storage(tmp_path / "attempts", repository)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"same-content")
    first_job, first_attempt, authority = _running_attempt(repository)
    _, role_type = _asset_api()
    stored = storage.snapshot_asset(
        source,
        job_id=first_job.job_id,
        attempt_id=first_attempt.attempt_id,
        role=role_type.SOURCE_MEDIA,
        expected_sha256=sha256_digest(source.read_bytes()),
        authority=authority,
        token=_Token(),
    )
    second_job = repository.create_job(
        request_hash=sha256_digest(b"second"),
        principal="local-user",
        client_request_id=None,
    )
    second_attempt = repository.create_attempt(second_job.job_id, "acquire")

    with pytest.raises(DomainError, match="Attempt stored asset is invalid"):
        storage.resolve_asset(
            stored,
            expected_job_id=second_job.job_id,
            expected_attempt_id=first_attempt.attempt_id,
        )
    with pytest.raises(DomainError, match="Attempt stored asset is invalid"):
        storage.resolve_asset(
            stored,
            expected_job_id=first_job.job_id,
            expected_attempt_id=second_attempt.attempt_id,
        )

    mutated = replace(
        stored,
        relative_locator=(
            f"jobs/{second_job.job_id}/attempts/{second_attempt.attempt_id}/"
            "assets/source_media.mp4"
        ),
    )
    with pytest.raises(DomainError, match="Attempt stored asset is invalid"):
        storage.resolve_asset(
            mutated,
            expected_job_id=first_job.job_id,
            expected_attempt_id=first_attempt.attempt_id,
        )


@pytest.mark.parametrize(
    "locator",
    [
        "C:/private/source.mp4",
        "../private/source.mp4",
        "/private/source.mp4",
    ],
)
def test_stored_asset_rejects_absolute_and_parent_locators(locator: str) -> None:
    asset_type, role_type = _asset_api()

    with pytest.raises(DomainError, match="Attempt stored asset is invalid"):
        asset_type(
            relative_locator=locator,
            sha256="sha256:" + "0" * 64,
            byte_length=1,
            role=role_type.SOURCE_MEDIA,
        )


def test_stored_asset_rejects_non_text_locator_with_domain_error() -> None:
    asset_type, role_type = _asset_api()

    with pytest.raises(DomainError, match="Attempt stored asset is invalid"):
        asset_type(
            relative_locator=None,
            sha256="sha256:" + "0" * 64,
            byte_length=1,
            role=role_type.SOURCE_MEDIA,
        )


def test_resolve_asset_revalidates_length_digest_and_reparse_target(tmp_path: Path) -> None:
    repository = SqliteJobRepository.open(tmp_path / "repository")
    storage = _storage(tmp_path / "attempts", repository)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"local-media")
    job, attempt, authority = _running_attempt(repository)
    _, role_type = _asset_api()
    stored = storage.snapshot_asset(
        source,
        job_id=job.job_id,
        attempt_id=attempt.attempt_id,
        role=role_type.SOURCE_MEDIA,
        expected_sha256=sha256_digest(source.read_bytes()),
        authority=authority,
        token=_Token(),
    )

    assert set(vars(stored)) == {
        "relative_locator",
        "sha256",
        "byte_length",
        "role",
    }

    for corrupted in (
        replace(stored, byte_length=stored.byte_length + 1),
        replace(stored, sha256="sha256:" + "f" * 64),
    ):
        with pytest.raises(DomainError, match="Attempt stored asset is invalid"):
            storage.resolve_asset(
                corrupted,
                expected_job_id=job.job_id,
                expected_attempt_id=attempt.attempt_id,
            )

    target = storage.root / Path(stored.relative_locator)
    external = tmp_path / "external.mp4"
    external.write_bytes(source.read_bytes())
    target.unlink()
    try:
        target.symlink_to(external)
    except OSError as error:
        pytest.skip(f"file symlink creation unavailable: {error}")
    try:
        with pytest.raises(DomainError, match="Attempt stored asset is invalid"):
            storage.resolve_asset(
                stored,
                expected_job_id=job.job_id,
                expected_attempt_id=attempt.attempt_id,
            )
    finally:
        if os.path.lexists(target):
            target.unlink()


def test_snapshot_asset_rejects_non_regular_directory_source(tmp_path: Path) -> None:
    repository = SqliteJobRepository.open(tmp_path / "repository")
    storage = _storage(tmp_path / "attempts", repository)
    source = tmp_path / "source-directory"
    source.mkdir()
    job, attempt, authority = _running_attempt(repository)
    _, role_type = _asset_api()

    with pytest.raises(DomainError, match="Attempt stored asset is invalid"):
        storage.snapshot_asset(
            source,
            job_id=job.job_id,
            attempt_id=attempt.attempt_id,
            role=role_type.SOURCE_MEDIA,
            expected_sha256="sha256:" + "0" * 64,
            authority=authority,
            token=_Token(),
        )


def test_windows_junction_is_detected_as_reparse_point(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows reparse assertion")
    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "junction"
    result = subprocess.run(
        [
            os.environ.get("COMSPEC", "cmd.exe"),
            "/d",
            "/c",
            "mklink",
            "/J",
            str(junction),
            str(target),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"directory junction creation unavailable: {result.stderr}")
    try:
        assert FileAttemptStorage._is_reparse_point(junction)
    finally:
        os.rmdir(junction)


class _FailingWriter:
    def __init__(self, delegate: object, failure: str) -> None:
        self._delegate = delegate
        self._failure = failure

    def __enter__(self) -> _FailingWriter:
        self._delegate.__enter__()
        return self

    def __exit__(self, *args: object) -> object:
        return self._delegate.__exit__(*args)

    def write(self, payload: bytes) -> int:
        if self._failure == "write":
            raise OSError("private target write path")
        return self._delegate.write(payload)

    def flush(self) -> None:
        if self._failure == "flush":
            raise OSError("private target flush path")
        self._delegate.flush()

    def fileno(self) -> int:
        return self._delegate.fileno()


@pytest.mark.parametrize(
    "failure",
    [
        "source_open",
        "parent_create",
        "target_open",
        "write",
        "flush",
        "fsync",
        "replace",
    ],
)
def test_snapshot_filesystem_failures_are_stable_and_path_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "repository")
    storage = _storage(tmp_path / "attempts", repository)
    source = tmp_path / "private-source.mp4"
    source.write_bytes(b"local-media")
    expected_sha256 = sha256_digest(source.read_bytes())
    job, attempt, authority = _running_attempt(repository)
    _, role_type = _asset_api()
    original_open = Path.open
    original_mkdir = Path.mkdir
    original_replace = os.replace
    original_fsync = os.fsync

    def patched_open(path: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        if failure == "source_open" and path == source and mode == "rb":
            raise OSError(f"cannot open {source}")
        if failure == "target_open" and mode == "xb" and path.name.endswith(
            ".partial"
        ):
            raise OSError(f"cannot create {path}")
        opened = original_open(path, mode, *args, **kwargs)
        if failure in {"write", "flush"} and mode == "xb" and path.name.endswith(
            ".partial"
        ):
            return _FailingWriter(opened, failure)
        return opened

    def patched_mkdir(path: Path, *args: object, **kwargs: object) -> None:
        if (
            failure == "parent_create"
            and path.name == "jobs"
            and path.parent == storage.root
        ):
            raise OSError(f"cannot create {path}")
        original_mkdir(path, *args, **kwargs)

    def patched_replace(source_path: object, target_path: object) -> None:
        if failure == "replace":
            raise OSError(f"cannot replace {target_path}")
        original_replace(source_path, target_path)

    def patched_fsync(file_descriptor: int) -> None:
        if failure == "fsync":
            raise OSError("private target fsync path")
        original_fsync(file_descriptor)

    monkeypatch.setattr(Path, "open", patched_open)
    monkeypatch.setattr(Path, "mkdir", patched_mkdir)
    monkeypatch.setattr(os, "replace", patched_replace)
    monkeypatch.setattr(os, "fsync", patched_fsync)

    with pytest.raises(DomainError) as caught:
        storage.snapshot_asset(
            source,
            job_id=job.job_id,
            attempt_id=attempt.attempt_id,
            role=role_type.SOURCE_MEDIA,
            expected_sha256=expected_sha256,
            authority=authority,
            token=_Token(),
        )

    assert caught.value.code == "attempt_storage_io_failed"
    serialized = f"{caught.value.message}{dict(caught.value.details)}"
    assert str(source) not in serialized
    assert str(storage.root) not in serialized


def test_cleanup_failure_does_not_replace_active_domain_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "repository")
    storage = _storage(tmp_path / "attempts", repository)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"local-media")
    job, attempt, authority = _running_attempt(repository)
    _, role_type = _asset_api()
    original_unlink = Path.unlink

    def failing_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path.name.endswith(".partial"):
            raise OSError("cleanup private path")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)

    with pytest.raises(DomainError) as caught:
        storage.snapshot_asset(
            source,
            job_id=job.job_id,
            attempt_id=attempt.attempt_id,
            role=role_type.SOURCE_MEDIA,
            expected_sha256="sha256:" + "f" * 64,
            authority=authority,
            token=_Token(),
        )

    assert caught.value.code == "attempt_stored_asset_invalid"


def test_snapshot_reopens_partial_and_verifies_destination_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "repository")
    storage = _storage(tmp_path / "attempts", repository)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"local-media")
    job, attempt, authority = _running_attempt(repository)
    _, role_type = _asset_api()
    original_open = Path.open
    destination_reads = 0

    def observed_open(path: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        nonlocal destination_reads
        if path.name.endswith(".partial") and mode == "rb":
            destination_reads += 1
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observed_open)

    stored = storage.snapshot_asset(
        source,
        job_id=job.job_id,
        attempt_id=attempt.attempt_id,
        role=role_type.SOURCE_MEDIA,
        expected_sha256=sha256_digest(source.read_bytes()),
        authority=authority,
        token=_Token(),
    )

    assert destination_reads == 1
    assert stored.byte_length == len(b"local-media")


@pytest.mark.parametrize(
    "failure",
    [
        "reparse_lstat",
        "inspection_lstat",
        "strict_resolve",
        "nonstrict_resolve",
    ],
)
def test_source_inspection_os_errors_are_stable_and_path_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "repository")
    storage = _storage(tmp_path / "attempts", repository)
    source = tmp_path / "private-source.mp4"
    source.write_bytes(b"local-media")
    job, attempt, authority = _running_attempt(repository)
    _, role_type = _asset_api()
    original_lstat = os.lstat
    original_resolve = Path.resolve

    if failure == "inspection_lstat":
        monkeypatch.setattr(
            FileAttemptStorage,
            "_path_chain_has_reparse_point",
            classmethod(lambda _cls, _path: False),
        )

    def patched_lstat(path: object, *args: object, **kwargs: object) -> object:
        if Path(path) == source and failure in {"reparse_lstat", "inspection_lstat"}:
            raise PermissionError(f"permission denied for {source}")
        return original_lstat(path, *args, **kwargs)

    def patched_resolve(
        path: Path,
        strict: bool = False,
    ) -> Path:
        if path == source and (
            (failure == "strict_resolve" and strict)
            or (failure == "nonstrict_resolve" and not strict)
        ):
            raise OSError(f"cannot resolve {source}")
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(os, "lstat", patched_lstat)
    monkeypatch.setattr(Path, "resolve", patched_resolve)

    with pytest.raises(DomainError) as caught:
        storage.snapshot_asset(
            source,
            job_id=job.job_id,
            attempt_id=attempt.attempt_id,
            role=role_type.SOURCE_MEDIA,
            expected_sha256=sha256_digest(source.read_bytes()),
            authority=authority,
            token=_Token(),
        )

    assert caught.value.code == "attempt_storage_io_failed"
    serialized = f"{caught.value.message}{dict(caught.value.details)}"
    assert str(source) not in serialized
    assert str(storage.root) not in serialized

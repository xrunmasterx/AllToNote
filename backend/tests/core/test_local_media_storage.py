from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.adapters.jobs.file_attempt_storage as storage_module
from app.adapters.jobs.file_attempt_storage import FileAttemptStorage
from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.core.application import video_acquisition
from app.core.domain.ids import sha256_digest
from app.core.domain.video import JobState
from app.core.errors import DomainError
from app.core.jobs.model import AttemptState


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
    authority = repository.claim_job(
        job.job_id,
        owner,
        ttl_seconds=ttl_seconds,
    ).authority
    pending = repository.create_attempt(
        job.job_id,
        "acquire",
        authority=authority,
    )
    attempt = repository.start_attempt(pending.attempt_id, authority)
    return job, attempt, authority


def _storage(root: Path, repository: SqliteJobRepository) -> FileAttemptStorage:
    return FileAttemptStorage(root, repository, validators={})


def _running_screenshot_attempt(
    repository: SqliteJobRepository,
) -> tuple[object, object, object]:
    job, acquire_attempt, authority = _running_attempt(repository)
    repository.transition_attempt(
        acquire_attempt.attempt_id, AttemptState.SUCCEEDED, authority=authority
    )
    attempt = repository.start_attempt(
        repository.create_attempt(
            job.job_id,
            "optional_screenshots",
            authority=authority,
        ).attempt_id,
        authority,
    )
    return job, attempt, authority


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
    repository.claim_job(job.job_id, "new-owner", ttl_seconds=300)
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

    assert caught.value.code == "job_claim_fenced"
    assert not tuple(storage.root.rglob("source_media.*"))


def test_screenshot_output_is_allocated_and_read_only_for_live_attempt(
    tmp_path: Path,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "repository")
    storage = _storage(tmp_path / "attempts", repository)
    job, acquire_attempt, authority = _running_attempt(repository)
    repository.transition_attempt(
        acquire_attempt.attempt_id, AttemptState.SUCCEEDED, authority=authority
    )
    pending = repository.create_attempt(
        job.job_id,
        "optional_screenshots",
        authority=authority,
    )
    screenshot_attempt = repository.start_attempt(pending.attempt_id, authority)
    artifact_id = "art_018cc251-f400-7000-8000-000000000003"

    output = storage.allocate_screenshot_output(
        job_id=job.job_id,
        attempt_id=screenshot_attempt.attempt_id,
        artifact_id=artifact_id,
        authority=authority,
    )
    output_path = storage.validate_screenshot_output(output, authority=authority)
    output_path.write_bytes(b"private-webp")
    payload = storage.read_screenshot_output(
        output,
        job_id=job.job_id,
        attempt_id=screenshot_attempt.attempt_id,
        artifact_id=artifact_id,
        authority=authority,
    )

    assert payload == b"private-webp"
    assert output_path.is_relative_to(storage.root)
    assert f"/{job.job_id}/attempts/{screenshot_attempt.attempt_id}/" in output_path.as_posix()
    storage.cleanup_screenshot_output(output, authority=authority)


@pytest.mark.parametrize("step_id", ("acquire", "transcribe", "draft"))
def test_screenshot_output_rejects_live_non_screenshot_attempts(
    tmp_path: Path, step_id: str
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "repository")
    storage = _storage(tmp_path / "attempts", repository)
    job = repository.create_job(
        request_hash=sha256_digest(b"request"),
        principal="local-user",
        client_request_id=None,
    )
    repository.transition_job(job.job_id, JobState.RUNNING)
    authority = repository.claim_job(
        job.job_id,
        "storage-owner",
        ttl_seconds=300,
    ).authority
    attempt = repository.start_attempt(
        repository.create_attempt(
            job.job_id,
            step_id,
            authority=authority,
        ).attempt_id,
        authority,
    )

    with pytest.raises(DomainError) as caught:
        storage.allocate_screenshot_output(
            job_id=job.job_id,
            attempt_id=attempt.attempt_id,
            artifact_id="art_018cc251-f400-7000-8000-000000000003",
            authority=authority,
        )

    assert caught.value.code == "attempt_fenced"
    assert not tuple(storage.root.rglob("*.webp"))


def test_screenshot_cleanup_rejects_leaf_substitution(tmp_path: Path) -> None:
    repository = SqliteJobRepository.open(tmp_path / "repository")
    storage = _storage(tmp_path / "attempts", repository)
    job, acquire_attempt, authority = _running_attempt(repository)
    repository.transition_attempt(
        acquire_attempt.attempt_id, AttemptState.SUCCEEDED, authority=authority
    )
    attempt = repository.start_attempt(
        repository.create_attempt(
            job.job_id,
            "optional_screenshots",
            authority=authority,
        ).attempt_id,
        authority,
    )
    output = storage.allocate_screenshot_output(
        job_id=job.job_id,
        attempt_id=attempt.attempt_id,
        artifact_id="art_018cc251-f400-7000-8000-000000000003",
        authority=authority,
    )
    path = storage.root / output.relative_locator
    path.unlink()
    path.write_bytes(b"substituted")

    with pytest.raises(DomainError, match="Attempt stored asset is invalid"):
        storage.cleanup_screenshot_output(output, authority=authority)

    assert path.read_bytes() == b"substituted"


def test_screenshot_cleanup_after_lease_loss_removes_only_old_capability(
    tmp_path: Path,
) -> None:
    now = [1_000]
    repository = SqliteJobRepository.open(
        tmp_path / "repository", clock=lambda: now[0]
    )
    storage = _storage(tmp_path / "attempts", repository)
    job, attempt, old_authority = _running_screenshot_attempt(repository)
    old_output = storage.allocate_screenshot_output(
        job_id=job.job_id,
        attempt_id=attempt.attempt_id,
        artifact_id="art_018cc251-f400-7000-8000-000000000003",
        authority=old_authority,
    )
    old_path = storage.root / old_output.relative_locator
    old_path.write_bytes(b"old-partial")
    now[0] = 302_001
    new_authority = repository.claim_job(
        job.job_id,
        "replacement-storage-owner",
        ttl_seconds=300,
    ).authority
    replacement = repository.take_over_running_attempt(
        job.job_id, attempt.attempt_id, new_authority
    )
    repository.transition_attempt(
        replacement.attempt_id, AttemptState.SUCCEEDED, authority=new_authority
    )
    successor = repository.start_attempt(
        repository.create_attempt(
            job.job_id,
            "optional_screenshots",
            authority=new_authority,
        ).attempt_id,
        new_authority,
    )
    successor_output = storage.allocate_screenshot_output(
        job_id=job.job_id,
        attempt_id=successor.attempt_id,
        artifact_id="art_018cc251-f400-7000-8000-000000000004",
        authority=new_authority,
    )
    successor_path = storage.root / successor_output.relative_locator
    successor_path.write_bytes(b"successor")

    with pytest.raises(DomainError) as caught:
        storage.cleanup_screenshot_output(old_output, authority=old_authority)

    assert caught.value.code == "attempt_fenced"
    assert not old_path.exists()
    assert not old_path.parent.exists()
    assert successor_path.read_bytes() == b"successor"


def test_screenshot_cleanup_rejects_successor_authority_without_deleting_old_leaf(
    tmp_path: Path,
) -> None:
    now = [1_000]
    repository = SqliteJobRepository.open(
        tmp_path / "repository", clock=lambda: now[0]
    )
    storage = _storage(tmp_path / "attempts", repository)
    job, attempt, old_authority = _running_screenshot_attempt(repository)
    output = storage.allocate_screenshot_output(
        job_id=job.job_id,
        attempt_id=attempt.attempt_id,
        artifact_id="art_018cc251-f400-7000-8000-000000000003",
        authority=old_authority,
    )
    output_path = storage.root / output.relative_locator
    now[0] = 302_001
    new_authority = repository.claim_job(
        job.job_id,
        "replacement-storage-owner",
        ttl_seconds=300,
    ).authority

    with pytest.raises(DomainError):
        storage.cleanup_screenshot_output(output, authority=new_authority)

    assert output_path.exists()


def test_screenshot_cleanup_rejects_cross_attempt_capability_binding(
    tmp_path: Path,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "repository")
    storage = _storage(tmp_path / "attempts", repository)
    job, attempt, authority = _running_screenshot_attempt(repository)
    output = storage.allocate_screenshot_output(
        job_id=job.job_id,
        attempt_id=attempt.attempt_id,
        artifact_id="art_018cc251-f400-7000-8000-000000000003",
        authority=authority,
    )
    output_path = storage.root / output.relative_locator
    other_attempt = repository.create_attempt(
        job.job_id,
        "optional_screenshots",
        authority=authority,
    )
    forged = replace(output, attempt_id=other_attempt.attempt_id)

    with pytest.raises(DomainError):
        storage.cleanup_screenshot_output(forged, authority=authority)

    assert output_path.exists()


@pytest.mark.parametrize(
    "failure", ("flush", "fsync", "fstat", "lstat", "capability")
)
def test_screenshot_allocation_failure_removes_owned_leaf_and_nonce_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "repository")
    storage = _storage(tmp_path / "attempts", repository)
    job, attempt, authority = _running_screenshot_attempt(repository)
    original_open = Path.open
    original_fsync = os.fsync
    original_fstat = os.fstat
    original_lstat = os.lstat
    lstat_failed = False
    leaf_opened = False

    class FailingFlush:
        def __init__(self, delegate: object) -> None:
            self._delegate = delegate

        def __enter__(self) -> "FailingFlush":
            self._delegate.__enter__()
            return self

        def __exit__(self, *args: object) -> object:
            return self._delegate.__exit__(*args)

        def flush(self) -> None:
            raise OSError("private flush path")

        def fileno(self) -> int:
            return self._delegate.fileno()

    def patched_open(path: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        nonlocal leaf_opened
        opened = original_open(path, mode, *args, **kwargs)
        if mode == "xb" and path.name.endswith(".partial.webp"):
            leaf_opened = True
        if failure == "flush" and mode == "xb" and path.name.endswith(".partial.webp"):
            return FailingFlush(opened)
        return opened

    def patched_fsync(fd: int) -> None:
        if failure == "fsync":
            raise OSError("private fsync path")
        original_fsync(fd)

    def patched_fstat(fd: int) -> object:
        if failure == "fstat":
            raise OSError("private fstat path")
        return original_fstat(fd)

    def patched_lstat(path: object, *args: object, **kwargs: object) -> object:
        nonlocal lstat_failed
        if (
            failure == "lstat"
            and not lstat_failed
            and leaf_opened
            and Path(path).name.endswith(".partial.webp")
        ):
            lstat_failed = True
            raise OSError("private lstat path")
        return original_lstat(path, *args, **kwargs)

    def failing_capability(**_kwargs: object) -> object:
        raise RuntimeError("private post-create path")

    monkeypatch.setattr(Path, "open", patched_open)
    monkeypatch.setattr(os, "fsync", patched_fsync)
    monkeypatch.setattr(os, "fstat", patched_fstat)
    monkeypatch.setattr(os, "lstat", patched_lstat)
    if failure == "capability":
        monkeypatch.setattr(storage_module, "ScreenshotOutputCapability", failing_capability)

    with pytest.raises(DomainError) as caught:
        storage.allocate_screenshot_output(
            job_id=job.job_id,
            attempt_id=attempt.attempt_id,
            artifact_id="art_018cc251-f400-7000-8000-000000000003",
            authority=authority,
        )

    assert caught.value.code == "attempt_storage_io_failed"
    assert not tuple(storage.root.rglob("*.partial.webp"))
    assert not tuple(
        path for path in storage.root.rglob("*") if path.is_dir() and len(path.name) == 32
    )


def test_screenshot_allocation_failure_never_deletes_substituted_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "repository")
    storage = _storage(tmp_path / "attempts", repository)
    job, attempt, authority = _running_screenshot_attempt(repository)
    substituted: list[Path] = []

    def substitute_then_fail(**kwargs: object) -> object:
        path = storage.root / Path(str(kwargs["relative_locator"]))
        path.unlink()
        path.write_bytes(b"substitute")
        substituted.append(path)
        raise RuntimeError("private post-create path")

    monkeypatch.setattr(
        storage_module, "ScreenshotOutputCapability", substitute_then_fail
    )

    with pytest.raises(DomainError) as caught:
        storage.allocate_screenshot_output(
            job_id=job.job_id,
            attempt_id=attempt.attempt_id,
            artifact_id="art_018cc251-f400-7000-8000-000000000003",
            authority=authority,
        )

    assert caught.value.code == "attempt_storage_io_failed"
    assert len(substituted) == 1
    assert substituted[0].read_bytes() == b"substitute"


def test_screenshot_allocation_uses_opened_leaf_identity_for_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "repository")
    storage = _storage(tmp_path / "attempts", repository)
    job, attempt, authority = _running_screenshot_attempt(repository)
    original_open = Path.open
    original_lstat = os.lstat
    opened_stream: list[object] = []
    substituted: list[Path] = []

    def patched_open(
        path: Path, mode: str = "r", *args: object, **kwargs: object
    ) -> object:
        stream = original_open(path, mode, *args, **kwargs)
        if mode == "xb" and path.name.endswith(".partial.webp"):
            opened_stream.append(stream)
        return stream

    def patched_lstat(path: object, *args: object, **kwargs: object) -> object:
        candidate = Path(path)
        if (
            opened_stream
            and not substituted
            and candidate.name.endswith(".partial.webp")
        ):
            opened_stream[0].close()
            candidate.unlink()
            candidate.write_bytes(b"substitute")
            substituted.append(candidate)
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", patched_open)
    monkeypatch.setattr(os, "lstat", patched_lstat)

    with pytest.raises(DomainError) as caught:
        storage.allocate_screenshot_output(
            job_id=job.job_id,
            attempt_id=attempt.attempt_id,
            artifact_id="art_018cc251-f400-7000-8000-000000000003",
            authority=authority,
        )

    assert caught.value.code == "attempt_stored_asset_invalid"
    assert len(substituted) == 1
    assert substituted[0].read_bytes() == b"substitute"


@pytest.mark.parametrize("failure", ("flush", "capability"))
def test_screenshot_allocation_base_exception_rolls_back_owned_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "repository")
    storage = _storage(tmp_path / "attempts", repository)
    job, attempt, authority = _running_screenshot_attempt(repository)
    original_open = Path.open
    primary: BaseException = (
        KeyboardInterrupt("private flush interrupt")
        if failure == "flush"
        else SystemExit("private capability exit")
    )

    class InterruptingFlush:
        def __init__(self, delegate: object) -> None:
            self._delegate = delegate

        def __enter__(self) -> "InterruptingFlush":
            self._delegate.__enter__()
            return self

        def __exit__(self, *args: object) -> object:
            return self._delegate.__exit__(*args)

        def flush(self) -> None:
            raise primary

        def fileno(self) -> int:
            return self._delegate.fileno()

    def patched_open(
        path: Path, mode: str = "r", *args: object, **kwargs: object
    ) -> object:
        stream = original_open(path, mode, *args, **kwargs)
        if failure == "flush" and mode == "xb" and path.name.endswith(
            ".partial.webp"
        ):
            return InterruptingFlush(stream)
        return stream

    def interrupting_capability(**_kwargs: object) -> object:
        raise primary

    monkeypatch.setattr(Path, "open", patched_open)
    if failure == "capability":
        monkeypatch.setattr(
            storage_module, "ScreenshotOutputCapability", interrupting_capability
        )

    with pytest.raises(BaseException) as caught:
        storage.allocate_screenshot_output(
            job_id=job.job_id,
            attempt_id=attempt.attempt_id,
            artifact_id="art_018cc251-f400-7000-8000-000000000003",
            authority=authority,
        )

    assert caught.value is primary
    assert not tuple(storage.root.rglob("*.partial.webp"))
    assert not tuple(
        path for path in storage.root.rglob("*") if path.is_dir() and len(path.name) == 32
    )


def test_screenshot_allocation_base_exception_never_deletes_substituted_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "repository")
    storage = _storage(tmp_path / "attempts", repository)
    job, attempt, authority = _running_screenshot_attempt(repository)
    primary = GeneratorExit("private capability exit")
    substituted: list[Path] = []

    def substitute_then_exit(**kwargs: object) -> object:
        path = storage.root / Path(str(kwargs["relative_locator"]))
        path.unlink()
        path.write_bytes(b"substitute")
        substituted.append(path)
        raise primary

    monkeypatch.setattr(
        storage_module, "ScreenshotOutputCapability", substitute_then_exit
    )

    with pytest.raises(BaseException) as caught:
        storage.allocate_screenshot_output(
            job_id=job.job_id,
            attempt_id=attempt.attempt_id,
            artifact_id="art_018cc251-f400-7000-8000-000000000003",
            authority=authority,
        )

    assert caught.value is primary
    assert len(substituted) == 1
    assert substituted[0].read_bytes() == b"substitute"


def test_screenshot_allocation_rollback_base_exception_does_not_mask_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "repository")
    storage = _storage(tmp_path / "attempts", repository)
    job, attempt, authority = _running_screenshot_attempt(repository)
    original_open = Path.open
    primary = KeyboardInterrupt("private flush interrupt")
    rollback_called = False

    class InterruptingFlush:
        def __init__(self, delegate: object) -> None:
            self._delegate = delegate

        def __enter__(self) -> "InterruptingFlush":
            self._delegate.__enter__()
            return self

        def __exit__(self, *args: object) -> object:
            return self._delegate.__exit__(*args)

        def flush(self) -> None:
            raise primary

        def fileno(self) -> int:
            return self._delegate.fileno()

    def patched_open(
        path: Path, mode: str = "r", *args: object, **kwargs: object
    ) -> object:
        stream = original_open(path, mode, *args, **kwargs)
        if mode == "xb" and path.name.endswith(".partial.webp"):
            return InterruptingFlush(stream)
        return stream

    def interrupting_rollback(*_args: object, **_kwargs: object) -> None:
        nonlocal rollback_called
        rollback_called = True
        raise SystemExit("private rollback exit")

    monkeypatch.setattr(Path, "open", patched_open)
    monkeypatch.setattr(
        storage, "_cleanup_created_screenshot_output", interrupting_rollback
    )

    with pytest.raises(BaseException) as caught:
        storage.allocate_screenshot_output(
            job_id=job.job_id,
            attempt_id=attempt.attempt_id,
            artifact_id="art_018cc251-f400-7000-8000-000000000003",
            authority=authority,
        )

    assert caught.value is primary
    assert rollback_called


def test_screenshot_read_rejects_leaf_replaced_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "repository")
    storage = _storage(tmp_path / "attempts", repository)
    job, attempt, authority = _running_screenshot_attempt(repository)
    output = storage.allocate_screenshot_output(
        job_id=job.job_id,
        attempt_id=attempt.attempt_id,
        artifact_id="art_018cc251-f400-7000-8000-000000000003",
        authority=authority,
    )
    output_path = storage.root / output.relative_locator
    output_path.write_bytes(b"owned")
    original_open = Path.open
    replaced = False

    def patched_open(
        path: Path, mode: str = "r", *args: object, **kwargs: object
    ) -> object:
        nonlocal replaced
        if path == output_path and mode == "rb" and not replaced:
            path.unlink()
            path.write_bytes(b"substitute")
            replaced = True
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", patched_open)

    with pytest.raises(DomainError) as caught:
        storage.read_screenshot_output(
            output,
            job_id=job.job_id,
            attempt_id=attempt.attempt_id,
            artifact_id=output.artifact_id,
            authority=authority,
        )

    assert caught.value.code == "attempt_stored_asset_invalid"
    assert output_path.read_bytes() == b"substitute"


def test_screenshot_cleanup_rejects_non_regular_output(tmp_path: Path) -> None:
    repository = SqliteJobRepository.open(tmp_path / "repository")
    storage = _storage(tmp_path / "attempts", repository)
    job, attempt, authority = _running_screenshot_attempt(repository)
    output = storage.allocate_screenshot_output(
        job_id=job.job_id,
        attempt_id=attempt.attempt_id,
        artifact_id="art_018cc251-f400-7000-8000-000000000003",
        authority=authority,
    )
    output_path = storage.root / output.relative_locator
    output_path.unlink()
    output_path.mkdir()

    with pytest.raises(DomainError, match="Attempt stored asset is invalid"):
        storage.cleanup_screenshot_output(output, authority=authority)

    assert output_path.is_dir()


def test_screenshot_cleanup_rejects_substituted_parent(
    tmp_path: Path,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "repository")
    storage = _storage(tmp_path / "attempts", repository)
    job, attempt, authority = _running_screenshot_attempt(repository)
    output = storage.allocate_screenshot_output(
        job_id=job.job_id,
        attempt_id=attempt.attempt_id,
        artifact_id="art_018cc251-f400-7000-8000-000000000003",
        authority=authority,
    )
    output_path = storage.root / output.relative_locator
    original_parent = output_path.parent
    saved_parent = original_parent.with_name(f"{original_parent.name}.saved")
    original_parent.rename(saved_parent)
    try:
        os.symlink(saved_parent, original_parent, target_is_directory=True)
    except OSError as error:
        saved_parent.rename(original_parent)
        pytest.skip(f"directory symlink unavailable on this OS: {error}")

    with pytest.raises(DomainError):
        storage.cleanup_screenshot_output(output, authority=authority)

    assert (saved_parent / output_path.name).exists()
    assert original_parent.is_symlink()


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


def test_snapshot_asset_checks_machine_state_capacity_before_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "repository")
    storage = _storage(tmp_path / "machine-state" / "attempts", repository)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"local-media")
    job, attempt, authority = _running_attempt(repository)
    _, role_type = _asset_api()
    monkeypatch.setattr(
        storage_module.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(total=100, used=90, free=10),
    )

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

    assert caught.value.code == "attempt_storage_capacity_insufficient"
    assert caught.value.category.value == "retryable_runtime"
    assert caught.value.details == {"required_bytes": 11, "available_bytes": 10}
    serialized = f"{caught.value.message}{dict(caught.value.details)}"
    assert str(source) not in serialized
    assert str(storage.root) not in serialized
    assert not tuple(storage.root.rglob("source_media.*"))
    assert not tuple(storage.root.rglob("*.partial"))


def test_snapshot_asset_rejects_source_growth_beyond_opened_length(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "repository")
    storage = _storage(tmp_path / "attempts", repository)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"local-media")
    job, attempt, authority = _running_attempt(repository)
    _, role_type = _asset_api()
    original_fstat = os.fstat

    def shorter_source_stat(file_descriptor: int) -> object:
        result = original_fstat(file_descriptor)
        return SimpleNamespace(
            st_mode=result.st_mode,
            st_dev=result.st_dev,
            st_ino=result.st_ino,
            st_size=result.st_size - 1,
        )

    monkeypatch.setattr(storage_module.os, "fstat", shorter_source_stat)

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

    assert caught.value.code == "attempt_stored_asset_invalid"
    assert not tuple(storage.root.rglob("source_media.*"))
    assert not tuple(storage.root.rglob("*.partial"))


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

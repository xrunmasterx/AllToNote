from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.errors import DomainError
from app.engine.client import LocalEngineClient
import app.engine.instance as engine_instance_module
from app.engine.contracts import ENGINE_PROTOCOL_VERSION, EngineDescriptor
from app.engine.instance import (
    EngineInstancePaths,
    current_user_identity,
    process_start_identity,
    publish_descriptor,
    remove_owned_descriptor,
)
from app.engine.transport import create_engine_listener
from app.runtime_paths import resolve_runtime_paths


def _paths(tmp_path: Path):
    return resolve_runtime_paths(machine_state_root=tmp_path / "machine")


def _descriptor(instance: EngineInstancePaths, *, engine_id: str, nonce: str):
    return EngineDescriptor(
        descriptor_version=1,
        engine_protocol_version=ENGINE_PROTOCOL_VERSION,
        runtime_major=0,
        scope_id=instance.scope_id,
        engine_id=engine_id,
        pid=os.getpid(),
        process_start_identity=process_start_identity(os.getpid()) or "",
        endpoint_kind=instance.endpoint_kind,
        endpoint_name=instance.endpoint_name,
        nonce=nonce,
        started_at="2026-08-01T00:00:00Z",
    )


def test_hardlinked_descriptor_is_rejected_without_mutating_target(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    instance = EngineInstancePaths.from_runtime_paths(paths)
    instance.root.mkdir(parents=True)
    canary = tmp_path / "outside-canary.json"
    canary.write_text("do not touch", encoding="utf-8")
    try:
        os.link(canary, instance.descriptor)
    except OSError:
        pytest.skip("Hard links are unavailable on this filesystem")

    with pytest.raises(DomainError) as raised:
        LocalEngineClient(paths).ensure()

    assert raised.value.code == "engine_state_root_unsafe"
    assert canary.read_text(encoding="utf-8") == "do not touch"
    assert instance.descriptor.samefile(canary)


def test_hardlinked_launch_lock_is_rejected_without_truncating_target(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    instance = EngineInstancePaths.from_runtime_paths(paths)
    instance.root.mkdir(parents=True)
    canary = tmp_path / "outside-lock-canary"
    canary.write_bytes(b"preserve")
    try:
        os.link(canary, instance.launch_lock)
    except OSError:
        pytest.skip("Hard links are unavailable on this filesystem")

    with pytest.raises(DomainError) as raised:
        LocalEngineClient(paths).ensure()

    assert raised.value.code == "engine_state_root_unsafe"
    assert canary.read_bytes() == b"preserve"


def test_linked_engine_root_is_rejected_without_mutating_target(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    instance = EngineInstancePaths.from_runtime_paths(paths)
    instance.root.parent.mkdir(parents=True)
    outside = tmp_path / "outside-engine-state"
    outside.mkdir()
    canary = outside / "canary.txt"
    canary.write_text("preserve", encoding="utf-8")
    try:
        instance.root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory links are unavailable on this filesystem")

    with pytest.raises(DomainError) as raised:
        LocalEngineClient(paths).ensure()

    assert raised.value.code == "engine_state_root_unsafe"
    assert canary.read_text(encoding="utf-8") == "preserve"
    assert set(outside.iterdir()) == {canary}


def test_pid_reuse_descriptor_is_never_authority_or_kill_permission(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    instance = EngineInstancePaths.from_runtime_paths(paths)
    instance.root.mkdir(parents=True)
    payload = {
        "descriptor_version": 1,
        "engine_protocol_version": ENGINE_PROTOCOL_VERSION,
        "runtime_major": 0,
        "scope_id": instance.scope_id,
        "engine_id": "018f0000-0000-7000-8000-000000000099",
        "pid": os.getpid(),
        "process_start_identity": "windows-filetime:1",
        "endpoint_kind": instance.endpoint_kind,
        "endpoint_name": instance.endpoint_name,
        "nonce": "b" * 43,
        "started_at": "2026-08-01T00:00:00Z",
    }
    instance.descriptor.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    client = LocalEngineClient(paths)
    try:
        status = client.ensure()

        assert status.running is True
        assert status.engine_id != payload["engine_id"]
        assert os.getpid() == payload["pid"]
    finally:
        client.stop()


def test_engine_scope_is_namespaced_by_current_user_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    monkeypatch.setattr(
        engine_instance_module,
        "current_user_identity",
        lambda: "sid:S-1-5-21-user-a",
    )
    first = EngineInstancePaths.from_runtime_paths(paths)
    monkeypatch.setattr(
        engine_instance_module,
        "current_user_identity",
        lambda: "sid:S-1-5-21-user-b",
    )
    second = EngineInstancePaths.from_runtime_paths(paths)

    assert first.scope_id != second.scope_id
    assert first.root != second.root
    assert first.launch_lock != second.launch_lock
    assert first.endpoint_name != second.endpoint_name


@pytest.mark.skipif(os.name != "nt", reason="Windows SID retrieval only")
def test_windows_user_identity_failure_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_to_load(_name: str, **_kwargs):
        raise OSError("identity unavailable")

    monkeypatch.setattr(engine_instance_module.ctypes, "WinDLL", fail_to_load)

    with pytest.raises(DomainError) as raised:
        current_user_identity()

    assert raised.value.code == "engine_user_identity_unavailable"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process identity only")
def test_posix_process_identity_has_no_pid_only_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_proc(self, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(Path, "read_text", missing_proc)

    assert process_start_identity(os.getpid()) is None


@pytest.mark.skipif(os.name != "nt", reason="Named-pipe timeout coverage")
def test_silent_named_pipe_peer_is_bounded_and_reported_stale(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    instance = EngineInstancePaths.from_runtime_paths(paths)
    instance.root.mkdir(parents=True)
    listener = create_engine_listener(instance.endpoint_name)
    accepted = threading.Event()

    def accept_without_authentication() -> None:
        connection = listener.accept()
        accepted.set()
        try:
            time.sleep(0.5)
        finally:
            connection.close()

    server = threading.Thread(target=accept_without_authentication, daemon=True)
    server.start()
    publish_descriptor(
        instance.descriptor,
        _descriptor(instance, engine_id=str(uuid4()), nonce="b" * 43),
    )
    client = LocalEngineClient(paths, ipc_timeout_seconds=0.1)

    started_at = time.monotonic()
    status = client.status()
    elapsed = time.monotonic() - started_at

    assert accepted.wait(timeout=1)
    assert status.state.value == "stale"
    assert status.running is False
    assert elapsed < 1
    server.join(timeout=2)
    listener.close()


def test_descriptor_cleanup_refuses_changed_path_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    instance = EngineInstancePaths.from_runtime_paths(paths)
    instance.root.mkdir(parents=True)
    owner = _descriptor(instance, engine_id=str(uuid4()), nonce="a" * 43)
    publish_descriptor(instance.descriptor, owner)
    open_matches = engine_instance_module._open_matches
    comparisons = 0

    def reject_changed_path(path: Path, descriptor: int) -> bool:
        nonlocal comparisons
        comparisons += 1
        if comparisons == 2:
            return False
        return open_matches(path, descriptor)

    monkeypatch.setattr(
        engine_instance_module,
        "_open_matches",
        reject_changed_path,
    )

    remove_owned_descriptor(
        instance.descriptor,
        engine_id=owner.engine_id,
        nonce=owner.nonce,
    )

    assert comparisons == 2
    assert instance.descriptor.read_bytes() == owner.to_bytes()

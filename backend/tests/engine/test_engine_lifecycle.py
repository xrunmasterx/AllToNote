from __future__ import annotations

import base64
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from multiprocessing import get_context
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.errors import DomainError
from app.core.jobs.model import JobExecutionOwner
from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.adapters.jobs.workspace_instance_registry import WorkspaceInstanceRegistry
from app.engine.client import LocalEngineClient
from app.engine.contracts import (
    ENGINE_PROTOCOL_VERSION,
    EngineDescriptor,
    EngineJobReference,
    encode_request,
)
from app.engine.host import run_engine_host
from app.engine.instance import (
    EngineInstancePaths,
    EngineState,
    EngineStatus,
    ensure_instance_root,
    publish_descriptor,
)
from app.engine.transport import connect_engine
from app.runtime_paths import resolve_runtime_paths


def _client(tmp_path: Path) -> LocalEngineClient:
    return LocalEngineClient(
        resolve_runtime_paths(machine_state_root=tmp_path / "machine state"),
        startup_timeout_seconds=10,
        shutdown_timeout_seconds=10,
    )


def _ensure_in_spawned_process(machine_root: str, barrier, results) -> None:
    client = LocalEngineClient(
        resolve_runtime_paths(machine_state_root=Path(machine_root)),
        startup_timeout_seconds=20,
    )
    barrier.wait()
    status = client.ensure()
    results.put((status.engine_id, status.started))


def _run_short_idle_host(runtime_paths) -> None:
    run_engine_host(
        paths=runtime_paths,
        idle_seconds=0.3,
    )


def test_engine_status_absent_is_read_only(tmp_path: Path) -> None:
    client = _client(tmp_path)

    status = client.status()

    assert status.state.value == "stopped"
    assert status.running is False
    assert not (tmp_path / "machine state").exists()


def test_engine_ensure_is_idempotent_and_stop_is_bounded(tmp_path: Path) -> None:
    client = _client(tmp_path)
    try:
        first = client.ensure()
        second = client.ensure()

        assert first.running is True
        assert first.started is True
        assert second.running is True
        assert second.started is False
        assert second.engine_id == first.engine_id
        assert client.status().engine_id == first.engine_id
    finally:
        stopped = client.stop()

    assert stopped.state.value == "stopped"
    assert stopped.stopped is True
    assert client.status().running is False


def test_stop_waits_for_lifetime_release_after_identity_disappears(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = _client(tmp_path)
    ensure_instance_root(client._paths)
    descriptor = EngineDescriptor(
        descriptor_version=1,
        engine_protocol_version=ENGINE_PROTOCOL_VERSION,
        runtime_major=0,
        scope_id=client._paths.scope_id,
        engine_id=str(uuid4()),
        pid=os.getpid(),
        process_start_identity="test-process-start",
        endpoint_kind=client._paths.endpoint_kind,
        endpoint_name=client._paths.endpoint_name,
        nonce="A" * 43,
        started_at="2026-08-01T00:00:00Z",
    )
    identity_checks = iter((True, False, False, False))
    lifetime_checks = iter((False, False, True))
    monkeypatch.setattr(
        client,
        "status",
        lambda: EngineStatus(
            EngineState.RUNNING,
            True,
            engine_id=descriptor.engine_id,
            started_at=descriptor.started_at,
        ),
    )
    monkeypatch.setattr(
        "app.engine.client.read_descriptor",
        lambda _path: descriptor,
    )
    monkeypatch.setattr(
        client,
        "_identity_matches",
        lambda _descriptor: next(identity_checks),
    )
    monkeypatch.setattr(client, "_request", lambda _descriptor, _method: {})
    monkeypatch.setattr(
        client,
        "_lifetime_lock_is_free",
        lambda: next(lifetime_checks),
    )

    stopped = client.stop()

    assert stopped.state is EngineState.STOPPED
    assert stopped.stopped is True


def test_concurrent_ensure_calls_converge_on_one_engine(tmp_path: Path) -> None:
    client = _client(tmp_path)
    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = tuple(executor.map(lambda _index: client.ensure(), range(8)))

        assert len({result.engine_id for result in results}) == 1
        assert sum(result.started for result in results) == 1
    finally:
        client.stop()


def test_shutdown_commits_before_best_effort_ack(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client.ensure()
    descriptor = EngineDescriptor.from_bytes(client.descriptor_path.read_bytes())
    connection = connect_engine(
        descriptor.endpoint_name,
        authkey=base64.urlsafe_b64decode(descriptor.nonce + "="),
        expected_pid=descriptor.pid,
        timeout_seconds=5,
    )
    try:
        connection.send_bytes(
            encode_request(
                request_id=f"req_{uuid4().hex}",
                method="shutdown",
                nonce=descriptor.nonce,
                params={},
            )
        )
    finally:
        connection.close()

    stopped = False
    deadline = time.monotonic() + 10
    try:
        while time.monotonic() < deadline:
            try:
                status = client.status()
            except DomainError as error:
                if error.code != "engine_state_root_unsafe":
                    raise
            else:
                if status.state is EngineState.STOPPED:
                    stopped = True
                    break
            time.sleep(0.02)
        assert stopped is True
    finally:
        if not stopped:
            client.stop()


def test_spawned_concurrent_ensure_calls_converge_on_one_engine(tmp_path: Path) -> None:
    machine_root = tmp_path / "spawned machine state"
    client = LocalEngineClient(
        resolve_runtime_paths(machine_state_root=machine_root),
        startup_timeout_seconds=20,
        shutdown_timeout_seconds=10,
    )
    context = get_context("spawn")
    barrier = context.Barrier(32)
    results = context.Queue()
    processes = tuple(
        context.Process(
            target=_ensure_in_spawned_process,
            args=(str(machine_root), barrier, results),
        )
        for _index in range(32)
    )
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=30)
            assert process.exitcode == 0
        statuses = tuple(results.get(timeout=2) for _process in processes)
        assert len({engine_id for engine_id, _started in statuses}) == 1
        assert sum(started for _engine_id, started in statuses) == 1
    finally:
        client.stop()


def test_idle_engine_exits_and_removes_only_owned_descriptor(tmp_path: Path) -> None:
    paths = resolve_runtime_paths(machine_state_root=tmp_path / "idle machine")
    context = get_context("spawn")
    process = context.Process(
        target=_run_short_idle_host,
        args=(paths,),
    )
    process.start()
    client = LocalEngineClient(paths)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not client.status().running:
        time.sleep(0.02)
    assert client.status().running is True

    process.join(timeout=10)

    assert process.exitcode == 0
    assert client.status().running is False
    assert not client.descriptor_path.exists()


def test_idle_deadline_does_not_reconcile_while_dispatcher_has_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = resolve_runtime_paths(machine_state_root=tmp_path / "busy idle machine")
    listener_wakeup = threading.Event()
    observed: dict[str, object] = {}

    class FakeLock:
        def __init__(self, _path: Path, *, timeout: int) -> None:
            assert timeout == 0

        def acquire(self) -> None:
            return None

        def release(self) -> None:
            return None

    class FakeListener:
        def accept(self):
            listener_wakeup.wait(timeout=0.1)
            raise OSError("listener closed")

        def close(self) -> None:
            listener_wakeup.set()

    class FakeConnection:
        def close(self) -> None:
            listener_wakeup.set()

    class FakeDispatcher:
        def __init__(self, _paths) -> None:
            self.has_work_checks = 0
            self.reconciled_while_busy = False
            self.reconcile_calls = 0
            observed["dispatcher"] = self

        def start(self) -> None:
            return None

        @property
        def has_work(self) -> bool:
            self.has_work_checks += 1
            return self.has_work_checks <= 3

        def reconcile(self) -> int:
            self.reconcile_calls += 1
            self.reconciled_while_busy = self.has_work_checks <= 3
            return 0

        def try_begin_draining(self) -> bool:
            return True

        def close(self, *, force: bool = False) -> None:
            assert force is False

    monkeypatch.setattr("app.engine.host.EngineFileLock", FakeLock)
    monkeypatch.setattr("app.engine.host.EngineJobDispatcher", FakeDispatcher)
    monkeypatch.setattr("app.engine.host.ensure_instance_root", lambda _paths: None)
    monkeypatch.setattr(
        "app.engine.host.process_start_identity",
        lambda _pid: "test-process-start",
    )
    monkeypatch.setattr(
        "app.engine.host.create_engine_listener",
        lambda _endpoint: FakeListener(),
    )
    monkeypatch.setattr("app.engine.host.publish_descriptor", lambda *_args: None)
    monkeypatch.setattr("app.engine.host.remove_owned_descriptor", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.engine.host.connect_engine",
        lambda *_args, **_kwargs: FakeConnection(),
    )

    assert run_engine_host(paths=paths, idle_seconds=0.0) == 0

    dispatcher = observed["dispatcher"]
    assert isinstance(dispatcher, FakeDispatcher)
    assert dispatcher.has_work_checks >= 4
    assert dispatcher.reconcile_calls == 1
    assert dispatcher.reconciled_while_busy is False


def test_forced_engine_death_can_be_reensured_without_pid_trust(tmp_path: Path) -> None:
    client = _client(tmp_path)
    first = client.ensure()
    descriptor = EngineDescriptor.from_bytes(client.descriptor_path.read_bytes())
    try:
        os.kill(descriptor.pid, signal.SIGTERM)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and client.status().running:
            time.sleep(0.05)

        replacement = client.ensure()

        assert replacement.running is True
        assert replacement.engine_id != first.engine_id
        assert replacement.started is True
    finally:
        client.stop()


def test_stop_serializes_against_successor_ensure(tmp_path: Path) -> None:
    client = _client(tmp_path)
    first = client.ensure()
    shutdown_replied = threading.Event()
    release_stop = threading.Event()
    original_request = client._request

    def controlled_request(descriptor, method):
        response = original_request(descriptor, method)
        if method == "shutdown":
            shutdown_replied.set()
            assert release_stop.wait(timeout=5)
        return response

    client._request = controlled_request
    with ThreadPoolExecutor(max_workers=2) as executor:
        stopping = executor.submit(client.stop)
        assert shutdown_replied.wait(timeout=5)
        ensuring = executor.submit(client.ensure)
        time.sleep(0.1)
        assert not ensuring.done()
        release_stop.set()
        stopped = stopping.result(timeout=10)
        replacement = ensuring.result(timeout=10)
    try:
        assert stopped.stopped is True
        assert replacement.started is True
        assert replacement.engine_id != first.engine_id
    finally:
        client.stop()


def test_ensure_heartbeat_prevents_idle_exit(tmp_path: Path) -> None:
    paths = resolve_runtime_paths(machine_state_root=tmp_path / "idle heartbeat")
    context = get_context("spawn")
    process = context.Process(
        target=_run_short_idle_host,
        args=(paths,),
    )
    process.start()
    client = LocalEngineClient(paths, startup_timeout_seconds=10)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not client.status().running:
        time.sleep(0.02)
    first = client.status()
    assert first.running is True

    for _index in range(5):
        time.sleep(0.12)
        refreshed = client.ensure()
        assert refreshed.engine_id == first.engine_id
        assert refreshed.started is False

    assert process.is_alive()
    process.join(timeout=10)
    assert process.exitcode == 0
    assert client.status().state.value == "stopped"


def test_job_notify_round_trips_persisted_terminal_state(tmp_path: Path) -> None:
    paths = resolve_runtime_paths(machine_state_root=tmp_path / "notify machine")
    local_parent = paths.workspace_registry_parent
    local_parent.mkdir(parents=True)
    workspace = tmp_path / "notify workspace"
    workspace.mkdir()
    identity = "notify-workspace-id"
    registry = WorkspaceInstanceRegistry(
        local_parent,
        inspect_workspace=lambda _root: identity,
    )
    instance = registry.resolve(workspace)
    repository = SqliteJobRepository.open(instance.machine_root / "job-store")
    job = repository.create_job(
        request_hash="sha256:" + "a" * 64,
        principal="local-user",
        client_request_id="terminal-notify",
        execution_owner=JobExecutionOwner.ENGINE,
    )
    repository.cancel_job(job.job_id)
    client = LocalEngineClient(paths, startup_timeout_seconds=10)
    try:
        receipt = client.notify_job(
            EngineJobReference(instance.instance_id, job.job_id)
        )

        assert receipt == {
            "engine_id": client.status().engine_id,
            "workspace_instance_id": instance.instance_id,
            "job_id": job.job_id,
            "state": "cancelled",
            "scheduled": False,
        }
    finally:
        client.stop()


def test_job_notify_preserves_remote_authority_error(tmp_path: Path) -> None:
    paths = resolve_runtime_paths(machine_state_root=tmp_path / "denied machine")
    local_parent = paths.workspace_registry_parent
    local_parent.mkdir(parents=True)
    workspace = tmp_path / "denied workspace"
    workspace.mkdir()
    registry = WorkspaceInstanceRegistry(
        local_parent,
        inspect_workspace=lambda _root: "denied-workspace-id",
    )
    instance = registry.resolve(workspace)
    repository = SqliteJobRepository.open(instance.machine_root / "job-store")
    job = repository.create_job(
        request_hash="sha256:" + "b" * 64,
        principal="remote-agent",
        client_request_id="denied-notify",
        execution_owner=JobExecutionOwner.ENGINE,
    )
    client = LocalEngineClient(paths, startup_timeout_seconds=10)
    try:
        with pytest.raises(DomainError) as raised:
            client.notify_job(
                EngineJobReference(instance.instance_id, job.job_id)
            )

        assert raised.value.code == "engine_job_authority_denied"
        assert raised.value.category.value == "policy_denied"
    finally:
        client.stop()


def test_stop_does_not_claim_success_for_a_live_unauthenticated_engine(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    client.ensure()
    descriptor = EngineDescriptor.from_bytes(client.descriptor_path.read_bytes())
    publish_descriptor(
        client.descriptor_path,
        replace(descriptor, nonce="b" * 43),
    )
    try:
        with pytest.raises(DomainError) as raised:
            client.stop()

        assert raised.value.code == "engine_stop_unavailable"
        assert client.status().state.value == "stale"
    finally:
        publish_descriptor(client.descriptor_path, descriptor)
        client.stop()

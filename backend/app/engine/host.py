from __future__ import annotations

import base64
import os
import secrets
import threading
import time
from datetime import UTC, datetime
from multiprocessing import AuthenticationError
from pathlib import Path
from uuid import uuid4

from filelock import Timeout

from app.core.errors import DomainError
from app.engine.contracts import (
    DESCRIPTOR_VERSION,
    ENGINE_PROTOCOL_VERSION,
    MAX_FRAME_BYTES,
    RUNTIME_COMPATIBILITY_MAJOR,
    EngineDescriptor,
    EngineProtocolError,
    decode_request,
    encode_response,
)
from app.engine.job_dispatcher import EngineJobDispatcher
from app.engine.errors import engine_remote_error_code
from app.engine.instance import (
    EngineFileLock,
    EngineInstancePaths,
    ensure_instance_root,
    process_start_identity,
    publish_descriptor,
    remove_owned_descriptor,
)
from app.engine.transport import (
    AUTHENTICATION_TIMEOUT_SECONDS,
    EngineListener,
    authenticate_server,
    connect_engine,
    create_engine_listener,
    receive_bytes,
)
from app.runtime_paths import RuntimePaths


DEFAULT_IDLE_SECONDS = 600.0
MAX_ACTIVE_CLIENTS = 8


def run_engine_host(
    *,
    paths: RuntimePaths,
    idle_seconds: float = DEFAULT_IDLE_SECONDS,
) -> int:
    engine_paths = EngineInstancePaths.from_runtime_paths(paths)
    ensure_instance_root(engine_paths)
    lifetime_lock = EngineFileLock(engine_paths.lifetime_lock, timeout=0)
    try:
        lifetime_lock.acquire()
    except Timeout:
        return 20

    engine_id = str(uuid4())
    nonce = secrets.token_urlsafe(32)
    authkey = base64.urlsafe_b64decode(nonce + "=")
    started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    start_identity = process_start_identity(os.getpid())
    if start_identity is None:
        lifetime_lock.release()
        return 30
    listener: EngineListener | None = None
    server: threading.Thread | None = None
    stop_event = threading.Event()
    shutdown_requested = threading.Event()
    active_lock = threading.Lock()
    active_requests = 0
    last_ensure = time.monotonic()
    client_slots = threading.BoundedSemaphore(MAX_ACTIVE_CLIENTS)
    client_threads: set[threading.Thread] = set()
    idle_lock: EngineFileLock | None = None
    dispatcher = EngineJobDispatcher(paths)
    dispatcher_drained = False

    def serve_connection(connection) -> None:
        nonlocal active_requests, dispatcher_drained, last_ensure
        with active_lock:
            active_requests += 1
        try:
            authenticate_server(
                connection,
                authkey,
                timeout_seconds=AUTHENTICATION_TIMEOUT_SECONDS,
            )
            payload = receive_bytes(
                connection,
                MAX_FRAME_BYTES,
                deadline=time.monotonic() + AUTHENTICATION_TIMEOUT_SECONDS,
            )
            request = decode_request(payload)
            if request.nonce != nonce:
                raise EngineProtocolError("engine_authentication_failed")
            try:
                if request.method == "job.notify":
                    reference = request.job_reference
                    assert reference is not None
                    receipt = dispatcher.notify(reference)
                    data = {
                        "engine_id": engine_id,
                        "workspace_instance_id": reference.workspace_instance_id,
                        "job_id": reference.job_id,
                        "state": receipt.state.value,
                        "scheduled": receipt.scheduled,
                    }
                elif request.method == "shutdown":
                    dispatcher.begin_graceful_shutdown()
                    shutdown_requested.set()
                    data = {
                        "engine_id": engine_id,
                        "state": "stopping",
                    }
                else:
                    if request.method == "hello":
                        with active_lock:
                            last_ensure = time.monotonic()
                    data = {
                        "engine_id": engine_id,
                        "engine_protocol_version": ENGINE_PROTOCOL_VERSION,
                        "started_at": started_at,
                        "state": "running",
                    }
                response = encode_response(
                    request_id=request.request_id,
                    data=data,
                )
            except DomainError as error:
                response = encode_response(
                    request_id=request.request_id,
                    error={"code": engine_remote_error_code(error)},
                )
            except Exception:
                response = encode_response(
                    request_id=request.request_id,
                    error={"code": "engine_internal_error"},
                )
            connection.send_bytes(response)
        except (AuthenticationError, EngineProtocolError, EOFError, OSError, TimeoutError):
            pass
        finally:
            connection.close()
            with active_lock:
                active_requests -= 1
                client_threads.discard(threading.current_thread())
            client_slots.release()

    def serve() -> None:
        assert listener is not None
        while not stop_event.is_set():
            try:
                connection = listener.accept()
            except (EOFError, OSError):
                return
            if not client_slots.acquire(blocking=False):
                connection.close()
                continue
            worker = threading.Thread(
                target=serve_connection,
                args=(connection,),
                name="alltonote-engine-client",
                daemon=True,
            )
            with active_lock:
                client_threads.add(worker)
            worker.start()

    try:
        listener = create_engine_listener(engine_paths.endpoint_name)
        descriptor = EngineDescriptor(
            descriptor_version=DESCRIPTOR_VERSION,
            engine_protocol_version=ENGINE_PROTOCOL_VERSION,
            runtime_major=RUNTIME_COMPATIBILITY_MAJOR,
            scope_id=engine_paths.scope_id,
            engine_id=engine_id,
            pid=os.getpid(),
            process_start_identity=start_identity,
            endpoint_kind=engine_paths.endpoint_kind,
            endpoint_name=engine_paths.endpoint_name,
            nonce=nonce,
            started_at=started_at,
        )
        publish_descriptor(engine_paths.descriptor, descriptor)
        server = threading.Thread(
            target=serve,
            name="alltonote-engine-listener",
            daemon=True,
        )
        server.start()
        dispatcher.start()
        while not stop_event.wait(0.05):
            if shutdown_requested.is_set():
                if dispatcher.ready_to_close:
                    dispatcher_drained = True
                    stop_event.set()
                continue
            with active_lock:
                active = active_requests
                expired = time.monotonic() >= last_ensure + idle_seconds
            if not active and expired:
                if dispatcher.has_work:
                    continue
                candidate = EngineFileLock(engine_paths.launch_lock, timeout=0)
                try:
                    candidate.acquire()
                except Timeout:
                    continue
                with active_lock:
                    active = active_requests
                    expired = time.monotonic() >= last_ensure + idle_seconds
                if active or not expired or dispatcher.has_work:
                    candidate.release()
                    continue
                try:
                    dispatcher.reconcile()
                except (DomainError, OSError, ValueError):
                    candidate.release()
                    continue
                if not dispatcher.try_begin_draining():
                    candidate.release()
                    continue
                idle_lock = candidate
                dispatcher_drained = True
                stop_event.set()
    finally:
        stop_event.set()
        if server is not None and server.is_alive():
            wake_connection = None
            try:
                wake_connection = connect_engine(
                    engine_paths.endpoint_name,
                    authkey=authkey,
                    expected_pid=os.getpid(),
                    timeout_seconds=AUTHENTICATION_TIMEOUT_SECONDS,
                )
            except (AuthenticationError, EOFError, OSError, TimeoutError):
                pass
            finally:
                if wake_connection is not None:
                    wake_connection.close()
        if server is not None:
            server.join(timeout=AUTHENTICATION_TIMEOUT_SECONDS + 0.5)
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        with active_lock:
            remaining_clients = tuple(client_threads)
        for worker in remaining_clients:
            worker.join(timeout=AUTHENTICATION_TIMEOUT_SECONDS + 0.5)
        dispatcher.close(force=not dispatcher_drained)
        remove_owned_descriptor(
            engine_paths.descriptor,
            engine_id=engine_id,
            nonce=nonce,
        )
        if os.name != "nt":
            try:
                Path(engine_paths.endpoint_name).unlink()
            except FileNotFoundError:
                pass
        lifetime_lock.release()
        if idle_lock is not None:
            idle_lock.release()
    return 0


__all__ = ["DEFAULT_IDLE_SECONDS", "run_engine_host"]

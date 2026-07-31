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
    create_engine_listener,
    receive_bytes,
)


DEFAULT_IDLE_SECONDS = 600.0
MAX_ACTIVE_CLIENTS = 8


def run_engine_host(
    *,
    engine_root: Path,
    log_root: Path,
    scope_id: str,
    idle_seconds: float = DEFAULT_IDLE_SECONDS,
) -> int:
    paths = EngineInstancePaths.from_roots(engine_root, log_root, scope_id)
    ensure_instance_root(paths)
    lifetime_lock = EngineFileLock(paths.lifetime_lock, timeout=0)
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
    active_lock = threading.Lock()
    active_requests = 0
    last_ensure = time.monotonic()
    client_slots = threading.BoundedSemaphore(MAX_ACTIVE_CLIENTS)
    client_threads: set[threading.Thread] = set()
    idle_lock: EngineFileLock | None = None

    def serve_connection(connection) -> None:
        nonlocal active_requests, last_ensure
        should_stop = False
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
            if request.method == "shutdown":
                data = {
                    "engine_id": engine_id,
                    "state": "stopping",
                }
                should_stop = True
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
            connection.send_bytes(
                encode_response(request_id=request.request_id, data=data)
            )
            if should_stop:
                stop_event.set()
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
        listener = create_engine_listener(paths.endpoint_name)
        descriptor = EngineDescriptor(
            descriptor_version=DESCRIPTOR_VERSION,
            engine_protocol_version=ENGINE_PROTOCOL_VERSION,
            runtime_major=RUNTIME_COMPATIBILITY_MAJOR,
            scope_id=scope_id,
            engine_id=engine_id,
            pid=os.getpid(),
            process_start_identity=start_identity,
            endpoint_kind=paths.endpoint_kind,
            endpoint_name=paths.endpoint_name,
            nonce=nonce,
            started_at=started_at,
        )
        publish_descriptor(paths.descriptor, descriptor)
        server = threading.Thread(
            target=serve,
            name="alltonote-engine-listener",
            daemon=True,
        )
        server.start()
        while not stop_event.wait(0.05):
            with active_lock:
                active = active_requests
                expired = time.monotonic() >= last_ensure + idle_seconds
            if not active and expired:
                candidate = EngineFileLock(paths.launch_lock, timeout=0)
                try:
                    candidate.acquire()
                except Timeout:
                    continue
                with active_lock:
                    active = active_requests
                    expired = time.monotonic() >= last_ensure + idle_seconds
                if active or not expired:
                    candidate.release()
                    continue
                idle_lock = candidate
                stop_event.set()
    finally:
        stop_event.set()
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        if server is not None:
            server.join(timeout=AUTHENTICATION_TIMEOUT_SECONDS + 0.5)
        with active_lock:
            remaining_clients = tuple(client_threads)
        for worker in remaining_clients:
            worker.join(timeout=AUTHENTICATION_TIMEOUT_SECONDS + 0.5)
        remove_owned_descriptor(
            paths.descriptor,
            engine_id=engine_id,
            nonce=nonce,
        )
        if os.name != "nt":
            try:
                Path(paths.endpoint_name).unlink()
            except FileNotFoundError:
                pass
        lifetime_lock.release()
        if idle_lock is not None:
            idle_lock.release()
    return 0


__all__ = ["DEFAULT_IDLE_SECONDS", "run_engine_host"]

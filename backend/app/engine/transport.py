from __future__ import annotations

import ctypes
import hashlib
import hmac
import os
import secrets
import socket
import time
from multiprocessing import AuthenticationError
from multiprocessing.connection import Connection, Listener
from typing import Protocol


AUTHENTICATION_TIMEOUT_SECONDS = 1.0
_CHALLENGE_PREFIX = b"alltonote-engine-challenge-v1:"


class EngineListener(Protocol):
    def accept(self) -> Connection: ...

    def close(self) -> None: ...


def receive_bytes(
    connection: Connection,
    maximum: int,
    *,
    deadline: float,
) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not connection.poll(remaining):
        raise TimeoutError("engine_ipc_timeout")
    return connection.recv_bytes(maximum)


def authenticate_server(
    connection: Connection,
    authkey: bytes,
    *,
    timeout_seconds: float = AUTHENTICATION_TIMEOUT_SECONDS,
) -> None:
    challenge = secrets.token_bytes(32)
    connection.send_bytes(_CHALLENGE_PREFIX + challenge)
    response = receive_bytes(
        connection,
        64,
        deadline=time.monotonic() + timeout_seconds,
    )
    expected = hmac.new(authkey, b"client:" + challenge, hashlib.sha256).digest()
    if not hmac.compare_digest(response, expected):
        raise AuthenticationError("engine authentication failed")
    proof = hmac.new(authkey, b"server:" + challenge, hashlib.sha256).digest()
    connection.send_bytes(proof)


def authenticate_client(
    connection: Connection,
    authkey: bytes,
    *,
    timeout_seconds: float = AUTHENTICATION_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    payload = receive_bytes(connection, 128, deadline=deadline)
    if not payload.startswith(_CHALLENGE_PREFIX):
        raise AuthenticationError("engine authentication failed")
    challenge = payload[len(_CHALLENGE_PREFIX) :]
    if len(challenge) != 32:
        raise AuthenticationError("engine authentication failed")
    response = hmac.new(authkey, b"client:" + challenge, hashlib.sha256).digest()
    connection.send_bytes(response)
    proof = receive_bytes(connection, 64, deadline=deadline)
    expected = hmac.new(authkey, b"server:" + challenge, hashlib.sha256).digest()
    if not hmac.compare_digest(proof, expected):
        raise AuthenticationError("engine authentication failed")


def create_engine_listener(endpoint_name: str) -> EngineListener:
    if os.name == "nt":
        return _WindowsPipeListener(endpoint_name)
    return Listener(endpoint_name, family="AF_UNIX", authkey=None)


def connect_engine(
    endpoint_name: str,
    *,
    authkey: bytes,
    expected_pid: int,
    timeout_seconds: float,
) -> Connection:
    if os.name == "nt":
        connection = _connect_windows_pipe(
            endpoint_name,
            expected_pid=expected_pid,
            timeout_seconds=timeout_seconds,
        )
    else:
        unix_socket = socket.socket(socket.AF_UNIX)
        try:
            unix_socket.settimeout(timeout_seconds)
            unix_socket.connect(endpoint_name)
            unix_socket.setblocking(True)
            connection = Connection(unix_socket.detach())
        except BaseException:
            unix_socket.close()
            raise
    try:
        authenticate_client(
            connection,
            authkey,
            timeout_seconds=timeout_seconds,
        )
        return connection
    except BaseException:
        connection.close()
        raise


if os.name == "nt":
    import _winapi
    from ctypes import wintypes
    from multiprocessing.connection import PipeConnection

    _PIPE_ACCESS_DUPLEX = 0x00000003
    _FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
    _FILE_FLAG_OVERLAPPED = 0x40000000
    _PIPE_TYPE_MESSAGE = 0x00000004
    _PIPE_READMODE_MESSAGE = 0x00000002
    _PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
    _PIPE_UNLIMITED_INSTANCES = 255
    _SDDL_REVISION_1 = 1
    _BUFSIZE = 65536

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = (
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        )

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.ULONG),
    )
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = (
        wintypes.BOOL
    )
    _kernel32.CreateNamedPipeW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_SecurityAttributes),
    )
    _kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
    _kernel32.GetNamedPipeServerProcessId.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.ULONG),
    )
    _kernel32.GetNamedPipeServerProcessId.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
    _kernel32.LocalFree.restype = wintypes.HLOCAL

    def _current_user_sid() -> str:
        from app.engine.instance import current_user_identity

        identity = current_user_identity()
        if not identity.startswith("sid:"):
            raise OSError("engine_user_identity_unavailable")
        return identity.removeprefix("sid:")

    def _create_windows_pipe_handle(endpoint_name: str, *, first: bool) -> int:
        security_descriptor = wintypes.LPVOID()
        descriptor_size = wintypes.ULONG()
        sddl = f"D:P(A;;GA;;;SY)(A;;GA;;;{_current_user_sid()})"
        if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl,
            _SDDL_REVISION_1,
            ctypes.byref(security_descriptor),
            ctypes.byref(descriptor_size),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = _SecurityAttributes(
            ctypes.sizeof(_SecurityAttributes),
            security_descriptor,
            False,
        )
        open_mode = _PIPE_ACCESS_DUPLEX | _FILE_FLAG_OVERLAPPED
        if first:
            open_mode |= _FILE_FLAG_FIRST_PIPE_INSTANCE
        pipe_mode = (
            _PIPE_TYPE_MESSAGE
            | _PIPE_READMODE_MESSAGE
            | _PIPE_REJECT_REMOTE_CLIENTS
        )
        try:
            handle = _kernel32.CreateNamedPipeW(
                endpoint_name,
                open_mode,
                pipe_mode,
                _PIPE_UNLIMITED_INSTANCES,
                _BUFSIZE,
                _BUFSIZE,
                1000,
                ctypes.byref(attributes),
            )
        finally:
            _kernel32.LocalFree(security_descriptor)
        invalid = ctypes.c_void_p(-1).value
        if handle in {None, invalid}:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(handle)

    class _WindowsPipeListener:
        def __init__(self, endpoint_name: str) -> None:
            self._endpoint_name = endpoint_name
            self._queued: list[int] = [
                _create_windows_pipe_handle(endpoint_name, first=True)
            ]
            self._connecting: int | None = None
            self._closed = False

        def accept(self) -> Connection:
            if self._closed:
                raise OSError("engine listener is closed")
            self._queued.append(
                _create_windows_pipe_handle(self._endpoint_name, first=False)
            )
            handle = self._queued.pop(0)
            self._connecting = handle
            try:
                overlapped = _winapi.ConnectNamedPipe(handle, overlapped=True)
                try:
                    _winapi.WaitForMultipleObjects(
                        [overlapped.event],
                        False,
                        _winapi.INFINITE,
                    )
                finally:
                    overlapped.GetOverlappedResult(True)
            except OSError as error:
                if error.winerror != _winapi.ERROR_NO_DATA:
                    _winapi.CloseHandle(handle)
                    raise
            finally:
                self._connecting = None
            return PipeConnection(handle)

        def close(self) -> None:
            if self._closed:
                return
            self._closed = True
            handles = tuple(self._queued)
            self._queued.clear()
            if self._connecting is not None:
                handles += (self._connecting,)
                self._connecting = None
            for handle in handles:
                try:
                    _winapi.CloseHandle(handle)
                except OSError:
                    pass

    def _connect_windows_pipe(
        endpoint_name: str,
        *,
        expected_pid: int,
        timeout_seconds: float,
    ) -> Connection:
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("engine_ipc_timeout")
            try:
                _winapi.WaitNamedPipe(
                    endpoint_name,
                    max(1, min(100, int(remaining * 1000))),
                )
                handle = _winapi.CreateFile(
                    endpoint_name,
                    _winapi.GENERIC_READ | _winapi.GENERIC_WRITE,
                    0,
                    _winapi.NULL,
                    _winapi.OPEN_EXISTING,
                    _winapi.FILE_FLAG_OVERLAPPED,
                    _winapi.NULL,
                )
                break
            except OSError as error:
                if error.winerror not in {
                    _winapi.ERROR_SEM_TIMEOUT,
                    _winapi.ERROR_PIPE_BUSY,
                    2,
                }:
                    raise
        try:
            server_pid = wintypes.ULONG()
            if not _kernel32.GetNamedPipeServerProcessId(
                handle,
                ctypes.byref(server_pid),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if server_pid.value != expected_pid:
                raise AuthenticationError("engine process identity mismatch")
            _winapi.SetNamedPipeHandleState(
                handle,
                _winapi.PIPE_READMODE_MESSAGE,
                None,
                None,
            )
            connection = PipeConnection(handle)
            handle = None
            return connection
        finally:
            if handle is not None:
                _winapi.CloseHandle(handle)

else:
    _WindowsPipeListener = object


__all__ = [
    "AUTHENTICATION_TIMEOUT_SECONDS",
    "EngineListener",
    "authenticate_server",
    "connect_engine",
    "create_engine_listener",
    "receive_bytes",
]

from __future__ import annotations

import os
import socket
import stat
import struct
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from model_deck.adapters.transport.framing import FrameError, decode_frame, encode_frame

ConnectionHandler = Callable[[dict[str, Any], int, threading.Event], dict[str, Any] | None]
DisconnectHandler = Callable[[int], None]
PeerCredentialChecker = Callable[[socket.socket], bool]


def peer_uid_from_socket(conn: socket.socket) -> int | None:
    getpeereid = getattr(conn, "getpeereid", None)
    if getpeereid is not None:
        try:
            uid, _gid = getpeereid()
            return int(uid)
        except OSError:
            return None
    if hasattr(os, "getpeereid"):
        try:
            uid, _gid = os.getpeereid(conn.fileno())
            return int(uid)
        except OSError:
            return None
    if hasattr(socket, "SO_PEERCRED"):
        try:
            cred = conn.getsockopt(
                socket.SOL_SOCKET,
                socket.SO_PEERCRED,
                struct.calcsize("3i"),
            )
            _pid, uid, _gid = struct.unpack("3i", cred)
            return int(uid)
        except OSError:
            return None
    return None


def default_peer_credential_checker(conn: socket.socket) -> bool:
    peer_uid = peer_uid_from_socket(conn)
    if peer_uid is None:
        return True
    return peer_uid == os.getuid()


class UnixSocketEngineServer:
    def __init__(
        self,
        socket_path: Path,
        handler: ConnectionHandler,
        on_disconnect: DisconnectHandler | None = None,
        peer_credential_checker: PeerCredentialChecker | None = None,
    ) -> None:
        self._socket_path = socket_path
        self._handler = handler
        self._on_disconnect = on_disconnect
        self._peer_checker = (
            peer_credential_checker
            if peer_credential_checker is not None
            else default_peer_credential_checker
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._server: socket.socket | None = None
        self._next_connection_id = 1
        self._connection_id_lock = threading.Lock()

    def _allocate_connection_id(self) -> int:
        with self._connection_id_lock:
            connection_id = self._next_connection_id
            self._next_connection_id += 1
            return connection_id

    def start(self) -> None:
        if self._socket_path.exists():
            mode = self._socket_path.stat().st_mode
            if not stat.S_ISSOCK(mode):
                raise OSError(
                    f"socket path exists and is not a socket: {self._socket_path}"
                )
            self._socket_path.unlink()
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self._socket_path.parent, 0o700)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self._socket_path))
            os.chmod(self._socket_path, 0o600)
            server.listen(8)
        except OSError:
            try:
                server.close()
            except OSError:
                pass
            if self._socket_path.exists():
                try:
                    self._socket_path.unlink()
                except OSError:
                    pass
            raise
        self._server = server
        self._thread = threading.Thread(target=self._serve, name="model-deck-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._socket_path.exists():
            try:
                self._socket_path.unlink()
            except OSError:
                pass

    def _serve(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                self._server.settimeout(0.5)
                conn, _addr = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            connection_id = self._allocate_connection_id()
            threading.Thread(
                target=self._handle_client,
                args=(conn, connection_id),
                daemon=True,
            ).start()

    def _handle_client(self, conn: socket.socket, connection_id: int) -> None:
        if not self._peer_checker(conn):
            conn.close()
            return
        buffer = bytearray()
        stop = threading.Event()
        try:
            while not stop.is_set() and not self._stop.is_set():
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buffer.extend(chunk)
                while True:
                    try:
                        frame = decode_frame(buffer)
                    except FrameError:
                        response = {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {"code": -32700, "message": "parse error"},
                        }
                        conn.sendall(encode_frame(response))
                        return
                    if frame is None:
                        break
                    response = self._handler(frame, connection_id, stop)
                    if response is not None:
                        conn.sendall(encode_frame(response))
        finally:
            conn.close()
            if self._on_disconnect is not None:
                self._on_disconnect(connection_id)

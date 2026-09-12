from __future__ import annotations

import math
import socket
from pathlib import Path
from typing import Any

from model_deck.adapters.transport.framing import decode_frame, encode_frame

DEFAULT_SESSION_TIMEOUT_SECONDS = 10.0
ENGINE_CALL_TIMEOUT_MESSAGE = "engine call timed out"


def _require_timeout_seconds(timeout_seconds: float) -> float:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be a finite number greater than zero")
    return float(timeout_seconds)


class UnixSocketEngineSession:
    def __init__(self, socket_path: Path, timeout_seconds: float) -> None:
        self._socket_path = socket_path
        self._timeout_seconds = _require_timeout_seconds(timeout_seconds)
        self._conn: socket.socket | None = None
        self._buffer = bytearray()

    def __enter__(self) -> UnixSocketEngineSession:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(self._timeout_seconds)
        try:
            conn.connect(str(self._socket_path))
        except OSError:
            conn.close()
            raise
        self._conn = conn
        self._buffer = bytearray()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def call(self, request: dict[str, Any]) -> dict[str, Any]:
        if self._conn is None:
            raise RuntimeError("session is not connected")
        self._conn.sendall(encode_frame(request))
        while True:
            try:
                chunk = self._conn.recv(65536)
            except (TimeoutError, socket.timeout) as exc:
                raise TimeoutError(ENGINE_CALL_TIMEOUT_MESSAGE) from exc
            if not chunk:
                raise ConnectionError("engine closed connection")
            self._buffer.extend(chunk)
            frame = decode_frame(self._buffer)
            if frame is None:
                continue
            return frame


class UnixSocketEngineClient:
    def __init__(
        self,
        socket_path: Path,
        *,
        timeout_seconds: float = DEFAULT_SESSION_TIMEOUT_SECONDS,
    ) -> None:
        self._socket_path = socket_path
        self._timeout_seconds = _require_timeout_seconds(timeout_seconds)

    def session(self) -> UnixSocketEngineSession:
        return UnixSocketEngineSession(self._socket_path, self._timeout_seconds)

    def call(self, request: dict[str, Any]) -> dict[str, Any]:
        with self.session() as session:
            return session.call(request)

"""Low-level HTTP POST transport adapter (B13 slice B).

Owns exactly one stdlib connection per call and returns an owned streaming
response handle. Performs no parsing, fallback, request translation,
credential lookup, logging, or event policy. Callers supply exact bytes and
headers so existing upstream behavior is preserved.
"""
from __future__ import annotations

import http.client
import threading
from collections.abc import Callable, Mapping
from typing import Any, BinaryIO, Optional


DEFAULT_TIMEOUT = 600

ConnectionFactory = Callable[..., Any]


def _default_connection_factory(host: str, port: int, secure: bool, timeout: float) -> Any:
    """Create a stdlib HTTP or HTTPS connection."""
    cls = http.client.HTTPSConnection if secure else http.client.HTTPConnection
    return cls(host, port, timeout=timeout)


class HttpStreamResponse:
    """Owned streaming response handle over one HTTP connection."""

    def __init__(self, connection: Any, raw: Any) -> None:
        self._connection = connection
        self._raw = raw
        self._closed = False
        self._close_lock = threading.Lock()
        self._status = int(raw.status)

    @property
    def status(self) -> int:
        """HTTP status code reported by the peer."""
        return self._status

    def read(self, amt: Optional[int] = None) -> bytes:
        """Read up to ``amt`` bytes, or the remainder when omitted."""
        if amt is None:
            return bytes(self._raw.read())
        return bytes(self._raw.read(amt))

    def read1(self, amt: int = -1) -> bytes:
        """Read one available chunk for streaming without extra buffering."""
        read1 = getattr(self._raw, "read1", None)
        if read1 is None:
            return self.read() if amt == -1 else self.read(amt)
        if amt == -1:
            return bytes(read1())
        return bytes(read1(amt))

    def close(self) -> None:
        """Release the owned connection; idempotent on success or error.

        A lock guards close ownership only and is never held across
        blocking reads or the underlying close call, so concurrent
        ``close`` calls resolve to exactly one underlying close. Reads
        may race a local close; close is local-only and never confirms
        remote cancellation.
        """
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            connection = self._connection
        try:
            connection.close()
        except Exception:
            pass

    def __repr__(self) -> str:
        return f"{type(self).__name__}(status={self._status}, closed={self._closed})"

    def __enter__(self) -> HttpStreamResponse:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def post_stream(
    *,
    host: str,
    port: int,
    secure: bool,
    path: str,
    payload: bytes,
    headers: Mapping[str, str],
    timeout: float = DEFAULT_TIMEOUT,
    connection_factory: ConnectionFactory = _default_connection_factory,
) -> HttpStreamResponse:
    """POST exact ``payload`` bytes and return the owned response handle.

    Closes a created connection when ``request`` or ``getresponse`` raises.
    Closing the handle releases the connection locally only; it does not
    confirm remote cancellation. No retry is performed.
    """
    connection = connection_factory(host, port, secure, timeout)
    try:
        connection.request("POST", path, body=bytes(payload), headers=dict(headers))
        raw = connection.getresponse()
    except Exception:
        try:
            connection.close()
        except Exception:
            pass
        raise
    return HttpStreamResponse(connection, raw)


__all__ = ["DEFAULT_TIMEOUT", "HttpStreamResponse", "post_stream"]

"""Stable error codes for the bounded process runtime."""
from __future__ import annotations

from dataclasses import dataclass


class ProcessRuntimeErrorCode:
    """Stable string codes raised on ``ProcessRuntimeError``."""

    SPAWN_FAILED = "spawn_failed"
    TIMEOUT = "timeout"
    FRAME_LIMIT = "frame_limit"
    ID_MISMATCH = "id_mismatch"
    MALFORMED_EOF = "malformed_eof"
    TRANSPORT = "transport"
    PROTOCOL = "protocol"


@dataclass(frozen=True)
class ProcessRuntimeError(Exception):
    """Raised when bounded subprocess lifecycle exchange fails.

    Attributes:
        code: Stable error code from ``ProcessRuntimeErrorCode``.
        detail: Human-readable detail safe for logs. Never carries the
            activation token or child stderr bytes.
    """

    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"

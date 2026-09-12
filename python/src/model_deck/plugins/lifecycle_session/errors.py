"""Stable error codes for the plugin lifecycle session."""
from __future__ import annotations

from dataclasses import dataclass


class SessionErrorCode:
    """Stable string codes raised on ``SessionError``."""

    SCHEMA_INVALID = "schema_invalid"
    OUT_OF_ORDER = "out_of_order"
    IDENTITY_MISMATCH = "identity_mismatch"
    REPLAY = "replay"
    CALL_NOT_ADMITTED = "call_not_admitted"
    UNKNOWN_HANDLE = "unknown_handle"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class SessionError(Exception):
    """Raised when a lifecycle operation is rejected.

    Attributes:
        code: Stable error code from ``SessionErrorCode``.
        detail: Human-readable detail safe for logs. Never carries the
            activation token.
        state: Session state at raise time.
    """

    code: str
    detail: str
    state: str | None = None

    def __str__(self) -> str:
        if self.state is None:
            return f"{self.code}: {self.detail}"
        return f"{self.code} ({self.state}): {self.detail}"

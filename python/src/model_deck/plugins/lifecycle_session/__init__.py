"""Pure in-memory plugin lifecycle session state.

Public surface is intentionally small:

- :class:`LifecycleSession` and :class:`SessionState` for the activation
  state machine.
- :class:`SessionError` and :class:`SessionErrorCode` for stable error
  reporting.

The session validates outbound and inbound ``plugin.v1`` lifecycle
payloads against the frozen ``contracts/plugin.v1/lifecycle`` schemas
through the bundled :mod:`model_deck_contracts` validator. It performs
no execution, network access, filesystem access or global-state access,
and it never exposes the activation token in ``repr``, errors or events.
"""
from __future__ import annotations

from .errors import SessionError, SessionErrorCode
from .session import LifecycleSession, SessionState

__all__ = [
    "LifecycleSession",
    "SessionError",
    "SessionErrorCode",
    "SessionState",
]

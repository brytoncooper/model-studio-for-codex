from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = [
    "HostIntegrationPort",
    "HostIntegrationError",
    "HostVersionMismatchError",
    "HostConflictError",
    "HostNotFoundError",
    "HostUnsupportedError",
]


@runtime_checkable
class HostIntegrationPort(Protocol):
    """App-owned adapter exposing frozen host enumeration and pure launch preparation.

    The engine owns the JSON-RPC envelopes, schema validation, and frozen
    operation discovery; the adapter owns the actual host enumeration,
    compatibility probe, and pure launch preparation. Adapters must
    never spawn processes or write global configuration; ``prepare``
    validates and captures the launch argv without launching.

    The engine service layer translates any
    :class:`HostIntegrationError` raised here into a domain error with
    the matching ``code`` attribute. ``list_hosts`` descriptors carry
    ``host_id`` (reverse-domain id) and ``api_profile`` (≤64 chars).
    ``prepare`` returns ``True`` when launch preparation succeeds; it
    may return ``False`` for unknown host_ids or raise
    :class:`HostNotFoundError` instead, both surface as ``not_found``.
    """

    def list_hosts(self) -> list[dict[str, str]]:
        ...

    def prepare(self, host_id: str) -> bool:
        ...


class HostIntegrationError(Exception):
    """Base error raised by ``HostIntegrationPort`` implementations.

    Carries a stable ``code`` that maps directly to a JSON-RPC domain
    error code; the engine service layer copies the attribute onto the
    translated engine-side exception.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class HostVersionMismatchError(HostIntegrationError):
    """Maps to ``version_mismatch``: observed protocol is unknown or unsupported."""

    def __init__(self, message: str = "host protocol version mismatch") -> None:
        super().__init__(message, code="version_mismatch")


class HostConflictError(HostIntegrationError):
    """Maps to ``conflict``: host is already running and refuses relaunch."""

    def __init__(self, message: str = "host conflict") -> None:
        super().__init__(message, code="conflict")


class HostNotFoundError(HostIntegrationError):
    """Maps to ``not_found``: unknown host_id."""

    def __init__(self, message: str = "host not found") -> None:
        super().__init__(message, code="not_found")


class HostUnsupportedError(HostIntegrationError):
    """Maps to ``unsupported_capability``: malformed host_id or absent capability."""

    def __init__(self, message: str = "host unsupported") -> None:
        super().__init__(message, code="unsupported_capability")

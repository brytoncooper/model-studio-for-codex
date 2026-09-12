from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

ConnectionKind = Literal["endpoint", "subscription"]


@dataclass(frozen=True, slots=True)
class ResolvedConnection:
    """Caller-resolved connection snapshot for one materialization."""

    connection_id: str
    kind: ConnectionKind
    revision: int
    endpoint_name: str | None = None
    base_url: str | None = None
    credential_account_id: str | None = None
    billing_description: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    """Caller-resolved model snapshot for one materialization."""

    registration_id: str
    connection_id: str
    provider_model_id: str
    display_name: str
    revision: int


@runtime_checkable
class ConnectionSnapshot(Protocol):
    """Read-only connection lookup owned by the caller (root resolver)."""

    def lookup(self, connection_id: str) -> ResolvedConnection | None:
        """Return the connection snapshot, or None when unknown."""
        ...


@runtime_checkable
class ModelSnapshot(Protocol):
    """Read-only model lookup owned by the caller (root resolver)."""

    def lookup(self, registration_id: str) -> ResolvedModel | None:
        """Return the model snapshot, or None when unknown."""
        ...

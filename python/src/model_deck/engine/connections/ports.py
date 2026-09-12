from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ConnectionRecord:
    connection_id: str
    provider_id: str
    revision: int
    endpoint_config_ref: str | None = None
    credential_ref: str | None = None


@dataclass(frozen=True, slots=True)
class SaveConnectionCommand:
    connection_id: str
    provider_id: str
    expected_revision: int
    idempotency_key: str
    endpoint_config_ref: str | None = None
    credential_ref: str | None = None


@runtime_checkable
class ConnectionRepository(Protocol):
    def list_connections(self) -> list[ConnectionRecord]: ...

    def save(self, command: SaveConnectionCommand) -> ConnectionRecord: ...


class ConnectionRevisionConflictError(ValueError):
    pass


class ConnectionIdempotencyConflictError(ValueError):
    pass

"""Engine-backed resolvers for connection ids and registered models.

Both classes call into the engine JSON-RPC service through the supplied
transport. Connection and model lookups query committed engine state on every
operation so a long-lived MCP process does not retain stale revisions.
"""
from __future__ import annotations

import re
from typing import Any

from model_deck.integrations.clients.mcp.errors import McpWriteError


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def _looks_like_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value))


class EngineConnectionResolver:
    """Resolve an MCP ``endpoint`` parameter to a canonical connection UUID.

    When ``endpoint`` is ``None`` and exactly one connection is available
    the resolver returns it. Multiple connections without an explicit
    endpoint raise :class:`McpWriteError`. Explicit endpoints match
    either the saved ``connection_id`` (UUID) or any stable identifier
    the engine returns alongside it (``provider_id``, ``endpoint_name``,
    or the saved ``endpoint_config_ref``).
    """

    def __init__(self, transport: Any) -> None:
        self._transport = transport

    def _connections(self) -> list[dict[str, Any]]:
        response = self._transport.call_engine("engine.v1.connections.list", {})
        connections = response.get("connections", []) if isinstance(response, dict) else []
        if not isinstance(connections, list):
            raise McpWriteError("engine.v1.connections.list returned an invalid payload")
        return [row for row in connections if isinstance(row, dict)]

    def resolve(self, endpoint: str | None) -> str:
        connections = self._connections()
        if endpoint is None or (isinstance(endpoint, str) and not endpoint.strip()):
            if len(connections) == 1:
                return str(connections[0]["connection_id"])
            names = ", ".join(self._format_label(row) for row in connections) or "<none>"
            raise McpWriteError(
                "more than one connection; pass an explicit endpoint. Saved connections: " + names
            )
        wanted = str(endpoint).strip()
        if _looks_like_uuid(wanted):
            for row in connections:
                if str(row.get("connection_id", "")).lower() == wanted.lower():
                    return str(row["connection_id"])
            raise McpWriteError(f"unknown connection_id: {wanted}")
        wanted_lower = wanted.lower()
        candidates = [
            row for row in connections
            if str(row.get("provider_id", "")).lower() == wanted_lower
            or str(row.get("endpoint_name", "")).lower() == wanted_lower
            or str(row.get("endpoint_config_ref", "")).lower() == wanted_lower
        ]
        if len(candidates) == 1:
            return str(candidates[0]["connection_id"])
        if not candidates:
            names = ", ".join(self._format_label(row) for row in connections) or "<none>"
            raise McpWriteError(f"unknown endpoint {endpoint!r}. Saved connections: {names}")
        raise McpWriteError(
            f"endpoint {endpoint!r} matches multiple connections; pass an exact connection_id"
        )

    @staticmethod
    def _format_label(row: dict[str, Any]) -> str:
        return str(row.get("endpoint_name") or row.get("provider_id") or row.get("connection_id") or "<unknown>")


class EngineRegisteredModelLocator:
    """Authoritative lookup of an existing registration's id and revision."""

    def __init__(self, transport: Any) -> None:
        self._transport = transport

    def find(self, provider_model_id: str) -> tuple[str, int] | None:
        if not isinstance(provider_model_id, str) or not provider_model_id:
            raise McpWriteError("provider_model_id must be a non-empty string")
        response = self._transport.call_engine(
            "engine.v1.models.list", {"collection": "registered"}
        )
        items = response.get("items", []) if isinstance(response, dict) else []
        if not isinstance(items, list):
            raise McpWriteError("engine.v1.models.list returned an invalid payload")
        for row in items:
            if not isinstance(row, dict):
                continue
            if str(row.get("provider_model_id", "")) != provider_model_id:
                continue
            registration_id = row.get("registration_id")
            revision = row.get("revision")
            if not isinstance(registration_id, str) or not isinstance(revision, int):
                continue
            return (registration_id, revision)
        return None


class EngineRegisteredModelPresentation:
    """Truthful fallback presentation derived from the authoritative engine state."""

    def __init__(self, transport: Any) -> None:
        self._transport = transport

    def _connection(self, connection_id: str) -> dict[str, Any]:
        response = self._transport.call_engine("engine.v1.connections.list", {})
        connections = response.get("connections", []) if isinstance(response, dict) else []
        for connection in connections:
            if isinstance(connection, dict) and connection.get("connection_id") == connection_id:
                return connection
        return {"connection_id": connection_id}

    def endpoint_name(self, connection_id: str) -> str:
        connection = self._connection(connection_id)
        return str(connection.get("provider_id") or connection_id)

    def billing_for(self, connection_id: str) -> tuple[str, str | None]:
        self._connection(connection_id)
        return (
            "saved provider connection",
            "Billing follows the provider account referenced by the saved connection.",
        )

    def price_for(self, provider_model_id: str, connection_id: str) -> str:
        return "not listed"

    def host_role(self, provider_model_id: str) -> None:
        return None

"""Read-only request snapshot bridging existing MCP rows to public model ports.

The host supplies rows it already formats and opaque connection identities.
This module does not parse registry files, resolve endpoints, or load pricing.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence
import uuid

from model_deck.engine.model_library.ports import RegisteredModelRecord


class McpRegistrySnapshot:
    def __init__(self, rows: Sequence[Mapping[str, Any]], connection_ids: Mapping[str, str]) -> None:
        self._rows = {row["id"]: deepcopy(dict(row)) for row in rows}
        self._connection_ids = dict(connection_ids)
        self._connections = {self._connection_ids[model_id]: row for model_id, row in self._rows.items()}

    def list_registered(self, *, connection_id: str | None = None) -> list[RegisteredModelRecord]:
        return [RegisteredModelRecord(
            registration_id=str(uuid.uuid5(uuid.NAMESPACE_URL, "model-deck:mcp:registration:" + row["role"])),
            provider_model_id=model_id,
            connection_id=self._connection_ids[model_id],
            display_name=row["name"],
            revision=1,
        ) for model_id, row in self._rows.items()
            if connection_id is None or self._connection_ids[model_id] == connection_id]

    def endpoint_name(self, connection_id: str) -> str:
        return self._connections[connection_id]["endpoint"]

    def billing_for(self, connection_id: str) -> tuple[str, str | None]:
        row = self._connections[connection_id]
        return row["billing"], row["billing_note"]

    def price_for(self, provider_model_id: str, connection_id: str) -> str:
        if self._connection_ids[provider_model_id] != connection_id:
            raise ValueError("registered model connection mismatch")
        return self._rows[provider_model_id]["price"]

    def host_role(self, provider_model_id: str) -> str:
        return self._rows[provider_model_id]["role"]

from __future__ import annotations

from typing import Any

from model_deck.integrations.clients.mcp.defaults import (
    UNVERIFIED_CACHE_CATALOG_NOTE,
    UNVERIFIED_CACHE_CATALOG_SOURCE,
)
from model_deck.integrations.clients.mcp.ports import McpRegisteredModelPresentation

_LISTED_HOW_TO_USE = (
    "Pick a model in Codex's picker, or spawn_agent with model set to the exact id."
)
_MAX_MCP_SEARCH_LIMIT = 50


class UnverifiedCacheCatalogProvenance:
    """Default cache-backed catalog metadata: never claims live provider verification."""

    def catalog_search_metadata(self, connection_id: str) -> tuple[str, bool, str]:
        return (UNVERIFIED_CACHE_CATALOG_SOURCE, False, UNVERIFIED_CACHE_CATALOG_NOTE)


def clamp_mcp_limit(limit: int | None) -> int:
    if limit is None:
        return 20
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return 20
    return max(1, min(value, _MAX_MCP_SEARCH_LIMIT))


def present_list_added_models(
    engine_result: dict[str, Any],
    presentation: McpRegisteredModelPresentation,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for item in engine_result.get("items", []):
        if item.get("kind") != "registered":
            continue
        provider_model_id = item["provider_model_id"]
        connection_id = item["connection_id"]
        billing, billing_note = presentation.billing_for(connection_id)
        row: dict[str, Any] = {
            "id": provider_model_id,
            "name": item["display_name"],
            "endpoint": presentation.endpoint_name(connection_id),
            "billing": billing,
            "price": presentation.price_for(provider_model_id, connection_id),
        }
        role = presentation.host_role(provider_model_id)
        if role is not None:
            row["role"] = role
        if billing_note is not None:
            row["billing_note"] = billing_note
        rows.append(row)
    rows.sort(key=lambda entry: entry["id"])
    return {"models": rows, "how_to_use": _LISTED_HOW_TO_USE}


def present_catalog_search(
    engine_result: dict[str, Any],
    *,
    endpoint_name: str,
    billing: str,
    billing_note: str | None = None,
    source: str,
    verified: bool,
    note: str,
) -> dict[str, Any]:
    models: list[dict[str, Any]] = []
    for item in engine_result.get("items", []):
        if item.get("kind") != "catalog":
            continue
        models.append(
            {
                "id": item["provider_model_id"],
                "name": item["display_name"],
                "endpoint": endpoint_name,
                "price": "not listed",
            }
        )
    payload: dict[str, Any] = {
        "endpoint": endpoint_name,
        "matches": len(models),
        "models": models,
        "billing": billing,
        "source": source,
        "verified": verified,
        "note": note,
    }
    if billing_note is not None:
        payload["billing_note"] = billing_note
    return payload

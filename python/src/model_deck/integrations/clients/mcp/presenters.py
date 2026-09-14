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
            "billing_note": billing_note,
            "price": presentation.price_for(provider_model_id, connection_id),
        }
        role = presentation.host_role(provider_model_id)
        if role is not None:
            row["role"] = role
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


# ---------------------------------------------------------------------------
# B06 write presenters: shape engine results into legacy MCP envelopes.
# The legacy envelopes carry the same top-level keys the unconverted
# application code emitted before the write seam moved to the engine, so
# downstream agents see no breaking change.
# ---------------------------------------------------------------------------

_REGISTER_MESSAGE_TEMPLATE = (
    "Added {model} on {endpoint}. It appears in Codex's picker on the next turn; "
    "spawn it with spawn_agent model=\"{model}\"."
)
_REGISTER_ALREADY_MESSAGE_TEMPLATE = (
    "{model} is already added on {endpoint}. It appears in Codex's picker on the next turn; "
    "spawn it with spawn_agent model=\"{model}\"."
)
_RENAME_MESSAGE_TEMPLATE = (
    "Renamed {model} to {name}. The picker reflects the new name on the next turn."
)
_REMOVE_MESSAGE_TEMPLATE = (
    "Removed {model}. It leaves Codex's picker on the next turn."
)


def present_register_result(
    engine_result: dict[str, Any],
    *,
    endpoint: str,
    billing: str,
    billing_note: str | None,
    price: str,
    already_registered: bool = False,
    role: str | None = None,
) -> dict[str, Any]:
    model_record = engine_result.get("model", {}) if isinstance(engine_result, dict) else {}
    provider_model_id = str(model_record.get("provider_model_id", ""))
    display_name = str(model_record.get("display_name") or "")
    message_template = _REGISTER_ALREADY_MESSAGE_TEMPLATE if already_registered else _REGISTER_MESSAGE_TEMPLATE
    payload: dict[str, Any] = {
        "ok": True,
        "model": provider_model_id,
        "endpoint": endpoint,
        "already_registered": already_registered,
        "name": display_name,
        "price": price,
        "billing": billing,
        "message": message_template.format(model=provider_model_id, endpoint=endpoint),
    }
    if role is not None:
        payload["role"] = role
    if billing_note is not None:
        payload["billing_note"] = billing_note
    return payload


def present_rename_result(
    engine_result: dict[str, Any],
    *,
    requested_name: str,
) -> dict[str, Any]:
    model_record = engine_result.get("model", {}) if isinstance(engine_result, dict) else {}
    provider_model_id = str(model_record.get("provider_model_id", ""))
    display_name = str(model_record.get("display_name") or requested_name)
    return {
        "ok": True,
        "model": provider_model_id,
        "name": display_name,
        "message": _RENAME_MESSAGE_TEMPLATE.format(model=provider_model_id, name=display_name),
    }


def present_remove_result(
    engine_result: dict[str, Any],
    *,
    provider_model_id: str,
) -> dict[str, Any]:
    removed = bool(engine_result.get("removed")) if isinstance(engine_result, dict) else False
    return {
        "ok": True,
        "removed": removed,
        "model": provider_model_id,
        "message": _REMOVE_MESSAGE_TEMPLATE.format(model=provider_model_id),
    }

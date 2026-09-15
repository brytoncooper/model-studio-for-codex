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


# ---------------------------------------------------------------------------
# B16/C8e evidence presenters: render engine-backed price_record and
# benchmark_record rows (contracts/engine.v1/vocabulary.schema.json) into the
# legacy model_pricing/model_benchmarks/refresh_benchmarks MCP envelopes.
#
# unit_price is priced per single token; the legacy envelope's
# *_per_million fields and price line are per-million-token, matching the
# convention model_pricing/search_models callers already expect.
# ---------------------------------------------------------------------------

_NOT_LISTED = "not listed"
_NO_CACHED_PRICE_NOTE = "No cached price is known for this model id yet."
_NO_CACHED_SCORES_NOTE = "No published scores are cached for this model id yet."
_REFRESH_STARTED_MESSAGE = (
    "Refresh started. Cached results are unchanged until it completes; "
    "poll engine.v1.jobs.get with this job_id for status."
)


def _per_million(unit_price: float | None) -> float | None:
    """unit_price is dollars per single token; scale to the legacy per-million convention."""
    if unit_price is None:
        return None
    return round(unit_price * 1_000_000, 6)


def _format_money(value: float | None) -> str | None:
    if value is None:
        return None
    if value == 0:
        return "$0"
    if value < 0.01:
        return f"${value:.4f}".rstrip("0").rstrip(".")
    if value < 1:
        return f"${value:.3f}".rstrip("0").rstrip(".")
    return f"${value:.2f}".rstrip("0").rstrip(".")


def _price_line(unit_prices: dict[str, Any], currency: str | None) -> str:
    if currency is None:
        return _NOT_LISTED
    parts: list[str] = []
    input_per_m = _per_million(unit_prices.get("input_tokens"))
    output_per_m = _per_million(unit_prices.get("output_tokens"))
    cached_per_m = _per_million(unit_prices.get("cached_tokens"))
    if input_per_m is not None:
        parts.append(f"in {_format_money(input_per_m)}/M")
    if output_per_m is not None:
        parts.append(f"out {_format_money(output_per_m)}/M")
    if cached_per_m is not None:
        parts.append(f"cached {_format_money(cached_per_m)}/M")
    return " · ".join(parts) if parts else _NOT_LISTED


def present_provenance(provenance: dict[str, Any]) -> dict[str, Any]:
    """Render a ``source_provenance`` record for an MCP envelope: source, age, staleness."""
    rendered: dict[str, Any] = {
        "source": provenance.get("source_id"),
        "fetched_at": provenance.get("fetched_at"),
        "stale": bool(provenance.get("stale")),
    }
    for optional_key in ("source_url", "citation", "as_of", "last_refresh_error"):
        value = provenance.get(optional_key)
        if value is not None:
            rendered[optional_key] = value
    return rendered


def present_price_query(engine_result: dict[str, Any], *, provider_model_id: str) -> dict[str, Any]:
    """Render an ``engine.v1.prices.query`` result for the ``model_pricing`` MCP tool.

    Unknown price keeps the exact ``"not listed"`` wording used elsewhere in
    this package (see :data:`_NOT_LISTED` and ``price_for`` above), rather
    than the legacy ``"not listed on OpenRouter"`` text -- the engine cache
    is not scoped to one provider, so that wording would be misleading here.
    """
    records = engine_result.get("records", []) if isinstance(engine_result, dict) else []
    matching = [
        record
        for record in records
        if isinstance(record, dict) and record.get("provider_model_id") == provider_model_id
    ]
    snapshot = engine_result.get("snapshot") if isinstance(engine_result, dict) else None
    if not matching:
        payload: dict[str, Any] = {"id": provider_model_id, "price": _NOT_LISTED, "note": _NO_CACHED_PRICE_NOTE}
        if isinstance(snapshot, dict):
            payload["snapshot"] = present_provenance(snapshot)
        return payload
    record = matching[0]
    unit_prices = record.get("unit_prices") or {}
    currency = record.get("currency")
    return {
        "id": provider_model_id,
        "price": _price_line(unit_prices, currency),
        "currency": currency,
        "input_per_million": _per_million(unit_prices.get("input_tokens")),
        "output_per_million": _per_million(unit_prices.get("output_tokens")),
        "cache_read_per_million": _per_million(unit_prices.get("cached_tokens")),
        "provenance": present_provenance(record.get("provenance") or {}),
    }


def present_benchmark_query(
    engine_result: dict[str, Any],
    *,
    model_id: str,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Render an ``engine.v1.benchmarks.query`` result for the ``model_benchmarks`` MCP tool."""
    records = engine_result.get("benchmarks", []) if isinstance(engine_result, dict) else []
    matching = [
        record
        for record in records
        if isinstance(record, dict)
        and (record.get("provider_model_id") == model_id or record.get("source_model_ref") == model_id)
    ]
    page = matching[offset : offset + limit]
    scores = [
        {
            "source": record.get("source_id"),
            "feed": record.get("feed"),
            "metric": record.get("metric"),
            "score": record.get("score"),
            "source_model_ref": record.get("source_model_ref"),
            "provider_model_id": record.get("provider_model_id"),
            "provenance": present_provenance(record.get("provenance") or {}),
        }
        for record in page
    ]
    payload: dict[str, Any] = {
        "model": model_id,
        "score_count": len(matching),
        "scores": scores,
        "next_offset": offset + limit if offset + limit < len(matching) else None,
        "missing_reason": None if matching else _NO_CACHED_SCORES_NOTE,
    }
    snapshot = engine_result.get("snapshot") if isinstance(engine_result, dict) else None
    if isinstance(snapshot, dict):
        payload["snapshot"] = present_provenance(snapshot)
    return payload


def present_refresh_started(job_result: dict[str, Any]) -> dict[str, Any]:
    """Render a ``prices.refresh``/``benchmarks.refresh`` job start for an MCP tool.

    The engine refresh is asynchronous (a job, observable through
    ``engine.v1.jobs.get``); the legacy ``refresh_benchmarks`` tool was
    synchronous and returned the full post-refresh status. Callers that
    read only ``ok``/``job_id`` keep working; callers that expected the
    full legacy status envelope see this note explaining the shift.
    """
    payload: dict[str, Any] = {"ok": True, "job_id": job_result.get("job_id")}
    if "job_kind" in job_result:
        payload["job_kind"] = job_result["job_kind"]
    if "explicit_network" in job_result:
        payload["explicit_network"] = job_result["explicit_network"]
    payload["message"] = _REFRESH_STARTED_MESSAGE
    return payload

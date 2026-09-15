"""B16/C8e MCP evidence seam: engine-backed prices/benchmarks reads with legacy fallback.

``McpEvidenceReadService`` answers the ``model_pricing``, ``model_benchmarks``
and ``refresh_benchmarks`` MCP tools (root ``model_deck_mcp.py`` lines 409,
445, 436). When an :class:`~model_deck.integrations.clients.mcp.ports.McpEvidenceEngine`
is supplied and the engine has the corresponding ``engine.v1.prices.*`` /
``engine.v1.benchmarks.*`` operation wired up, the read is served from the
engine's cached, provenance-carrying evidence (contracts/engine.v1 C8a
seam: ``prices.query``, ``prices.refresh``, ``benchmarks.query``,
``benchmarks.refresh``).

When an operation is not yet implemented server-side, ``call_engine`` raises
``McpEngineError`` with ``code == "unsupported_capability"`` (see
``EngineDispatch.handle`` in ``model_deck/engine/dispatch.py``, which returns
exactly that code for any method outside its implemented set). This service
catches that *per operation* and falls back to the legacy root ``pricing.py``
/ ``model_benchmarks.py`` path through the generic
``LegacyMcpApplicationAdapter.call`` seam, so a partially-landed engine
(prices wired, benchmarks not, or vice versa) still serves both tools
correctly. Any other ``McpEngineError`` is surfaced as an ``McpReadError``
rather than silently falling back, so a real engine failure is not masked as
"not listed".

``benchmark_status``, ``compare_models`` and ``rank_models`` stay fully
legacy in this unit: the C8a evidence seam only defines a per-model query and
a refresh job, not an aggregate feed-status, comparison, or ranking shape.
Call ``legacy.call(name, arguments)`` directly for those, exactly as before.
"""
from __future__ import annotations

import uuid
from typing import Any

from model_deck.integrations.clients.mcp.errors import McpEngineError, McpReadError
from model_deck.integrations.clients.mcp.ports import (
    LegacyMcpApplicationAdapter,
    McpEngineTransport,
    McpEvidenceEngine,
)
from model_deck.integrations.clients.mcp.presenters import (
    present_benchmark_query,
    present_price_query,
    present_refresh_started,
)

_PRICES_QUERY = "engine.v1.prices.query"
_PRICES_REFRESH = "engine.v1.prices.refresh"
_BENCHMARKS_QUERY = "engine.v1.benchmarks.query"
_BENCHMARKS_REFRESH = "engine.v1.benchmarks.refresh"

_UNSUPPORTED_CAPABILITY = "unsupported_capability"


class EngineEvidenceReader:
    """``McpEvidenceEngine`` implementation calling the authenticated engine transport.

    Mirrors ``registry_resolver.py``'s ``Engine*`` adapters: a thin wrapper
    around :class:`McpEngineTransport.call_engine` with no caching or retry
    of its own. The engine side owns caching (``cached: true`` on every
    query result) and staleness.
    """

    def __init__(self, transport: McpEngineTransport) -> None:
        self._transport = transport

    def query_prices(
        self,
        *,
        provider_model_id: str | None = None,
        registration_id: str | None = None,
        include_stale: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"include_stale": include_stale}
        if provider_model_id is not None:
            params["provider_model_id"] = provider_model_id
        if registration_id is not None:
            params["registration_id"] = registration_id
        return self._transport.call_engine(_PRICES_QUERY, params)

    def refresh_prices(self, *, idempotency_key: str) -> dict[str, Any]:
        return self._transport.call_engine(_PRICES_REFRESH, {"idempotency_key": idempotency_key})

    def query_benchmarks(self, *, model_id: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if model_id is not None:
            params["model_id"] = model_id
        return self._transport.call_engine(_BENCHMARKS_QUERY, params)

    def refresh_benchmarks(self, *, idempotency_key: str) -> dict[str, Any]:
        return self._transport.call_engine(_BENCHMARKS_REFRESH, {"idempotency_key": idempotency_key})


def _operation_absent(exc: McpEngineError) -> bool:
    return exc.code == _UNSUPPORTED_CAPABILITY


def _new_idempotency_key() -> str:
    return str(uuid.uuid4())


class McpEvidenceReadService:
    """Engine-first, legacy-fallback reads for the price/benchmark MCP tools."""

    def __init__(
        self,
        *,
        legacy: LegacyMcpApplicationAdapter,
        evidence: McpEvidenceEngine | None = None,
    ) -> None:
        self._legacy = legacy
        self._evidence = evidence

    def model_pricing(self, model: str) -> dict[str, Any]:
        if not isinstance(model, str) or not model.strip():
            raise McpReadError("Say which model id to price.")
        provider_model_id = model.strip()
        if self._evidence is not None:
            try:
                engine_result = self._evidence.query_prices(provider_model_id=provider_model_id)
            except McpEngineError as exc:
                if not _operation_absent(exc):
                    raise McpReadError(exc.message) from exc
            else:
                return present_price_query(engine_result, provider_model_id=provider_model_id)
        return self._legacy_call("model_pricing", {"model": provider_model_id})

    def model_benchmarks(self, model: str, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        if not isinstance(model, str) or not model.strip():
            raise McpReadError("Say which model id to profile.")
        model_id = model.strip()
        if self._evidence is not None:
            try:
                engine_result = self._evidence.query_benchmarks(model_id=model_id)
            except McpEngineError as exc:
                if not _operation_absent(exc):
                    raise McpReadError(exc.message) from exc
            else:
                return present_benchmark_query(engine_result, model_id=model_id, limit=limit, offset=offset)
        return self._legacy_call("model_benchmarks", {"model": model_id, "limit": limit, "offset": offset})

    def refresh_benchmarks(self, authenticated: bool = False, endpoint: str | None = None) -> dict[str, Any]:
        if self._evidence is not None:
            try:
                job_result = self._evidence.refresh_benchmarks(idempotency_key=_new_idempotency_key())
            except McpEngineError as exc:
                if not _operation_absent(exc):
                    raise McpReadError(exc.message) from exc
            else:
                return present_refresh_started(job_result)
        arguments: dict[str, Any] = {}
        if authenticated:
            arguments["authenticated"] = authenticated
        if endpoint is not None:
            arguments["endpoint"] = endpoint
        return self._legacy_call("refresh_benchmarks", arguments)

    def refresh_prices(self) -> dict[str, Any]:
        """Start an engine price-refresh job.

        No legacy MCP tool exposes a price refresh (``model_pricing`` only
        reads); this method exists so the ``prices.refresh`` half of the
        C8a evidence seam has a caller once a host wires it to a tool or to
        the V2 usage view's refresh action. Without an engine evidence port
        there is no legacy equivalent to fall back to, so this raises
        rather than silently no-op'ing.
        """
        if self._evidence is None:
            raise McpReadError("Price refresh requires the engine evidence path; no legacy equivalent exists.")
        try:
            job_result = self._evidence.refresh_prices(idempotency_key=_new_idempotency_key())
        except McpEngineError as exc:
            raise McpReadError(exc.message) from exc
        return present_refresh_started(job_result)

    def _legacy_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._legacy.call(name, arguments)
        except McpReadError:
            raise
        except Exception as exc:  # legacy DeckError and friends
            raise McpReadError(str(exc)) from exc

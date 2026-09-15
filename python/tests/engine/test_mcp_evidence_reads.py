"""B16/C8e MCP evidence seam tests.

Exercises ``McpEvidenceReadService`` with a fake ``McpEngineTransport`` (same
``_RecordingTransport``/``_StaticTransport`` shape as
``test_mcp_model_writes.py``) and a fake legacy adapter, so the seam is
observable without a live engine or the root ``model_deck_mcp.py`` module.

Per the C8e brief: written but not run in this unit.
"""
import unittest
from typing import Any

from model_deck.integrations.clients.mcp.errors import McpEngineError, McpReadError
from model_deck.integrations.clients.mcp.evidence_reads import (
    EngineEvidenceReader,
    McpEvidenceReadService,
)

MODEL_ID = "deepseek/deepseek-v4.1-flash"


class _StaticTransport:
    """Returns canned responses keyed by method; never raises unless queued."""

    def __init__(self, responses: dict[str, dict[str, Any]] | None = None) -> None:
        self.responses = responses or {}
        self.errors: dict[str, McpEngineError] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_engine(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, dict(params)))
        if method in self.errors:
            raise self.errors[method]
        if method not in self.responses:
            raise AssertionError(f"unexpected method {method} with {params}")
        return self.responses[method]


def _unsupported_capability_error(method: str) -> McpEngineError:
    return McpEngineError(
        {
            "error": {
                "code": "internal",
                "data": {"code": "unsupported_capability", "message": f"method not implemented: {method}"},
            }
        }
    )


def _conflict_error(message: str) -> McpEngineError:
    return McpEngineError({"error": {"code": "conflict", "message": message}})


class _RecordingLegacy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_endpoints(self) -> dict[str, Any]:
        raise NotImplementedError

    def search_models(self, query: str, endpoint: str | None = None, limit: int = 20) -> dict[str, Any]:
        raise NotImplementedError

    def list_added_models(self) -> dict[str, Any]:
        raise NotImplementedError

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, dict(arguments)))
        if name == "model_pricing":
            return {"id": arguments["model"], "price": "not listed on OpenRouter", "note": "legacy fallback"}
        if name == "model_benchmarks":
            return {"model": arguments["model"], "scores": [], "score_count": 0}
        if name == "refresh_benchmarks":
            return {"catalog_models": 0, "score_count": 0}
        raise AssertionError(f"unexpected legacy tool {name}")


_PRICE_RECORD = {
    "provider_model_id": MODEL_ID,
    "currency": "USD",
    "unit_prices": {"input_tokens": 0.00000015, "output_tokens": 0.0000006, "cached_tokens": None},
    "provenance": {
        "source_id": "com.modeldeck.openrouter",
        "fetched_at": "2026-09-14T00:00:00Z",
        "stale": False,
        "as_of": "2026-09-14",
    },
}

_BENCHMARK_RECORD = {
    "source_model_ref": MODEL_ID,
    "provider_model_id": MODEL_ID,
    "source_id": "com.modeldeck.artificial-analysis",
    "feed": "artificial-analysis",
    "metric": "coding_index",
    "score": 61.5,
    "provenance": {
        "source_id": "com.modeldeck.artificial-analysis",
        "fetched_at": "2026-09-14T00:00:00Z",
        "stale": False,
    },
}


class EngineEvidenceReaderTests(unittest.TestCase):
    def test_query_prices_forwards_provider_model_id_and_default_include_stale(self) -> None:
        transport = _StaticTransport({"engine.v1.prices.query": {"records": [], "snapshot": {}, "cached": True}})
        reader = EngineEvidenceReader(transport)
        reader.query_prices(provider_model_id=MODEL_ID)
        self.assertEqual(transport.calls, [("engine.v1.prices.query", {"include_stale": True, "provider_model_id": MODEL_ID})])

    def test_refresh_benchmarks_forwards_idempotency_key(self) -> None:
        transport = _StaticTransport({"engine.v1.benchmarks.refresh": {"job_id": "job-1"}})
        reader = EngineEvidenceReader(transport)
        reader.refresh_benchmarks(idempotency_key="key-1")
        self.assertEqual(transport.calls, [("engine.v1.benchmarks.refresh", {"idempotency_key": "key-1"})])


class McpEvidenceReadServiceTests(unittest.TestCase):
    def test_model_pricing_uses_engine_when_available(self) -> None:
        transport = _StaticTransport(
            {"engine.v1.prices.query": {"records": [_PRICE_RECORD], "snapshot": {}, "cached": True}}
        )
        legacy = _RecordingLegacy()
        service = McpEvidenceReadService(legacy=legacy, evidence=EngineEvidenceReader(transport))
        result = service.model_pricing(MODEL_ID)
        self.assertEqual(result["id"], MODEL_ID)
        self.assertEqual(result["currency"], "USD")
        self.assertAlmostEqual(result["input_per_million"], 0.15)
        self.assertAlmostEqual(result["output_per_million"], 0.6)
        self.assertIsNone(result["cache_read_per_million"])
        self.assertIn("in $0.15/M", result["price"])
        self.assertEqual(result["provenance"]["source"], "com.modeldeck.openrouter")
        self.assertFalse(result["provenance"]["stale"])
        self.assertEqual(legacy.calls, [])

    def test_model_pricing_unknown_stays_not_listed(self) -> None:
        transport = _StaticTransport({"engine.v1.prices.query": {"records": [], "snapshot": {}, "cached": True}})
        legacy = _RecordingLegacy()
        service = McpEvidenceReadService(legacy=legacy, evidence=EngineEvidenceReader(transport))
        result = service.model_pricing("unknown/model")
        self.assertEqual(result["price"], "not listed")
        self.assertEqual(legacy.calls, [])

    def test_model_pricing_falls_back_to_legacy_when_operation_absent(self) -> None:
        transport = _StaticTransport()
        transport.errors["engine.v1.prices.query"] = _unsupported_capability_error("engine.v1.prices.query")
        legacy = _RecordingLegacy()
        service = McpEvidenceReadService(legacy=legacy, evidence=EngineEvidenceReader(transport))
        result = service.model_pricing(MODEL_ID)
        self.assertEqual(legacy.calls, [("model_pricing", {"model": MODEL_ID})])
        self.assertEqual(result["price"], "not listed on OpenRouter")

    def test_model_pricing_without_evidence_port_uses_legacy(self) -> None:
        legacy = _RecordingLegacy()
        service = McpEvidenceReadService(legacy=legacy, evidence=None)
        service.model_pricing(MODEL_ID)
        self.assertEqual(legacy.calls, [("model_pricing", {"model": MODEL_ID})])

    def test_model_pricing_surfaces_real_engine_error_without_falling_back(self) -> None:
        transport = _StaticTransport()
        transport.errors["engine.v1.prices.query"] = _conflict_error("cache locked")
        legacy = _RecordingLegacy()
        service = McpEvidenceReadService(legacy=legacy, evidence=EngineEvidenceReader(transport))
        with self.assertRaises(McpReadError):
            service.model_pricing(MODEL_ID)
        self.assertEqual(legacy.calls, [])

    def test_model_benchmarks_uses_engine_and_renders_provenance(self) -> None:
        transport = _StaticTransport(
            {"engine.v1.benchmarks.query": {"benchmarks": [_BENCHMARK_RECORD], "snapshot": {}, "cached": True}}
        )
        legacy = _RecordingLegacy()
        service = McpEvidenceReadService(legacy=legacy, evidence=EngineEvidenceReader(transport))
        result = service.model_benchmarks(MODEL_ID)
        self.assertEqual(result["score_count"], 1)
        self.assertEqual(result["scores"][0]["metric"], "coding_index")
        self.assertEqual(result["scores"][0]["provenance"]["source"], "com.modeldeck.artificial-analysis")
        self.assertIsNone(result["missing_reason"])
        self.assertEqual(legacy.calls, [])

    def test_model_benchmarks_falls_back_to_legacy_when_operation_absent(self) -> None:
        transport = _StaticTransport()
        transport.errors["engine.v1.benchmarks.query"] = _unsupported_capability_error("engine.v1.benchmarks.query")
        legacy = _RecordingLegacy()
        service = McpEvidenceReadService(legacy=legacy, evidence=EngineEvidenceReader(transport))
        service.model_benchmarks(MODEL_ID)
        self.assertEqual(legacy.calls, [("model_benchmarks", {"model": MODEL_ID, "limit": 100, "offset": 0})])

    def test_refresh_benchmarks_uses_engine_job_when_available(self) -> None:
        transport = _StaticTransport(
            {"engine.v1.benchmarks.refresh": {"job_id": "job-1", "job_kind": "com.modeldeck.engine.benchmarks.refresh"}}
        )
        legacy = _RecordingLegacy()
        service = McpEvidenceReadService(legacy=legacy, evidence=EngineEvidenceReader(transport))
        result = service.refresh_benchmarks()
        self.assertEqual(result["job_id"], "job-1")
        self.assertEqual(result["job_kind"], "com.modeldeck.engine.benchmarks.refresh")
        self.assertEqual(legacy.calls, [])

    def test_refresh_benchmarks_falls_back_to_legacy_when_operation_absent(self) -> None:
        transport = _StaticTransport()
        transport.errors["engine.v1.benchmarks.refresh"] = _unsupported_capability_error("engine.v1.benchmarks.refresh")
        legacy = _RecordingLegacy()
        service = McpEvidenceReadService(legacy=legacy, evidence=EngineEvidenceReader(transport))
        result = service.refresh_benchmarks(authenticated=True, endpoint="OpenRouter")
        self.assertEqual(
            legacy.calls,
            [("refresh_benchmarks", {"authenticated": True, "endpoint": "OpenRouter"})],
        )
        self.assertIn("catalog_models", result)

    def test_refresh_prices_without_evidence_port_raises(self) -> None:
        service = McpEvidenceReadService(legacy=_RecordingLegacy(), evidence=None)
        with self.assertRaises(McpReadError):
            service.refresh_prices()

    def test_refresh_prices_uses_engine_job(self) -> None:
        transport = _StaticTransport({"engine.v1.prices.refresh": {"job_id": "job-2"}})
        service = McpEvidenceReadService(legacy=_RecordingLegacy(), evidence=EngineEvidenceReader(transport))
        result = service.refresh_prices()
        self.assertEqual(result["job_id"], "job-2")

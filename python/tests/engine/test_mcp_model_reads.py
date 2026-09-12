import json
import unittest
from pathlib import Path
from typing import Any

from model_deck.adapters.storage.json_catalog_cache import JsonFixtureCatalogCacheRepository
from model_deck.engine.model_library.use_cases import ListModelsUseCase
from model_deck.integrations.clients.mcp.errors import McpReadError
from model_deck.integrations.clients.mcp.model_reads import McpModelReadService
from model_deck.integrations.clients.mcp.presenters import UnverifiedCacheCatalogProvenance
from model_deck.integrations.hosts.codex.legacy_models import LegacyCodexModelRepository

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MCP_FIXTURES = FIXTURES / "mcp"
CONNECTION = "550e8400-e29b-41d4-a716-446655440002"
CATALOG_FIXTURE = FIXTURES / "catalog" / "openrouter_sample.json"
LEGACY_REMOTE_SEARCH = MCP_FIXTURES / "search_models_legacy_remote_envelope.json"
CATALOG_CACHE_SEARCH = MCP_FIXTURES / "search_models_catalog_cache_envelope.json"


class _FakePresentation:
    def endpoint_name(self, connection_id: str) -> str:
        if connection_id == CONNECTION:
            return "Fixture OpenRouter"
        return connection_id

    def billing_for(self, connection_id: str) -> tuple[str, str | None]:
        return ("OpenRouter credits", None)

    def price_for(self, provider_model_id: str, connection_id: str) -> str:
        if provider_model_id == "qwen/test":
            return "in $0.1/M · out $0.2/M"
        return "n/a"

    def host_role(self, provider_model_id: str) -> str | None:
        if provider_model_id == "qwen/test":
            return "openrouter_test"
        return None


class _NoRolePresentation(_FakePresentation):
    def host_role(self, provider_model_id: str) -> str | None:
        return None


class _FakeCatalogResolver:
    def catalog_connection_for_endpoint(self, endpoint: str | None) -> str | None:
        if endpoint is None or endpoint.casefold() in {"openrouter", "fixture openrouter"}:
            return CONNECTION
        return None

    def default_catalog_endpoint_name(self) -> str | None:
        return "Fixture OpenRouter"


class _RecordingLegacy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_endpoints(self) -> dict[str, Any]:
        self.calls.append(("list_endpoints", {}))
        return {"endpoints": [{"name": "Legacy", "base_url": "https://example.test"}]}

    def search_models(self, query: str, endpoint: str | None = None, limit: int = 20) -> dict[str, Any]:
        self.calls.append(("search_models", {"query": query, "endpoint": endpoint, "limit": limit}))
        return {"endpoint": endpoint or "Legacy", "matches": 0, "models": []}

    def list_added_models(self) -> dict[str, Any]:
        self.calls.append(("list_added_models", {}))
        return {"models": [], "how_to_use": "legacy"}

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("call", {"name": name, "arguments": arguments}))
        return {"ok": True}


class _FixtureLegacySearch(_RecordingLegacy):
    def __init__(self, envelope_path: Path) -> None:
        super().__init__()
        self._envelope = json.loads(envelope_path.read_text(encoding="utf-8"))

    def search_models(self, query: str, endpoint: str | None = None, limit: int = 20) -> dict[str, Any]:
        self.calls.append(("search_models", {"query": query, "endpoint": endpoint, "limit": limit}))
        return dict(self._envelope)


class _CustomProvenance:
    def catalog_search_metadata(self, connection_id: str) -> tuple[str, bool, str]:
        return (
            "remote",
            True,
            "Listed by this endpoint. Inference and native agent compatibility have not been verified.",
        )


class McpModelReadTests(unittest.TestCase):
    def _service(
        self,
        legacy: _RecordingLegacy | None = None,
        *,
        with_catalog: bool = True,
        presentation: _FakePresentation | None = None,
        catalog_provenance: Any = None,
    ) -> McpModelReadService:
        repo = LegacyCodexModelRepository(
            FIXTURES / "legacy_agent",
            default_connection_id=CONNECTION,
        )
        catalog = JsonFixtureCatalogCacheRepository(CATALOG_FIXTURE) if with_catalog else None
        list_models = ListModelsUseCase(repo, catalog_reader=catalog)
        return McpModelReadService(
            list_models=list_models,
            legacy=legacy or _RecordingLegacy(),
            presentation=presentation or _FakePresentation(),
            catalog_resolver=_FakeCatalogResolver() if with_catalog else None,
            catalog_provenance=catalog_provenance,
        )

    def test_list_added_models_matches_fixture_envelope(self) -> None:
        service = self._service()
        result = service.list_added_models()
        expected = json.loads((MCP_FIXTURES / "list_added_registered.json").read_text(encoding="utf-8"))
        self.assertEqual(result["how_to_use"], expected["how_to_use"])
        self.assertEqual(len(result["models"]), len(expected["models"]))
        row = result["models"][0]
        expected_row = expected["models"][0]
        for key in ("id", "name", "endpoint", "billing", "price", "role"):
            self.assertEqual(row[key], expected_row[key])

    def test_list_added_models_omits_role_when_host_has_none(self) -> None:
        service = self._service(presentation=_NoRolePresentation())
        result = service.list_added_models()
        self.assertEqual(len(result["models"]), 1)
        self.assertNotIn("role", result["models"][0])

    def test_list_added_models_does_not_call_legacy(self) -> None:
        legacy = _RecordingLegacy()
        service = self._service(legacy)
        service.list_added_models()
        self.assertEqual(legacy.calls, [])

    def test_list_endpoints_delegates_to_legacy_adapter(self) -> None:
        legacy = _RecordingLegacy()
        service = self._service(legacy)
        result = service.list_endpoints()
        self.assertEqual(legacy.calls, [("list_endpoints", {})])
        self.assertEqual(result["endpoints"][0]["name"], "Legacy")

    def test_search_models_catalog_matches_legacy_derived_fixture(self) -> None:
        legacy = _RecordingLegacy()
        service = self._service(legacy)
        result = service.search_models("beta", endpoint="Fixture OpenRouter", limit=10)
        expected = json.loads(CATALOG_CACHE_SEARCH.read_text(encoding="utf-8"))
        self.assertEqual(legacy.calls, [])
        for key in ("endpoint", "matches", "billing", "source", "verified", "note", "models"):
            self.assertEqual(result[key], expected[key])

    def test_search_models_default_provenance_is_unverified_cache(self) -> None:
        service = self._service()
        result = service.search_models("beta", endpoint="Fixture OpenRouter")
        self.assertFalse(result["verified"])
        self.assertEqual(result["source"], "cache")
        self.assertEqual(
            result["note"],
            UnverifiedCacheCatalogProvenance().catalog_search_metadata(CONNECTION)[2],
        )

    def test_search_models_uses_injected_provenance_when_host_verifies(self) -> None:
        service = self._service(catalog_provenance=_CustomProvenance())
        result = service.search_models("beta", endpoint="Fixture OpenRouter")
        self.assertEqual(result["source"], "remote")
        self.assertTrue(result["verified"])
        self.assertIn("have not been verified", result["note"])

    def test_search_models_falls_back_to_legacy_envelope_fixture(self) -> None:
        legacy = _FixtureLegacySearch(LEGACY_REMOTE_SEARCH)
        service = self._service(legacy)
        result = service.search_models("qwen", endpoint="LM Studio", limit=3)
        expected = json.loads(LEGACY_REMOTE_SEARCH.read_text(encoding="utf-8"))
        self.assertEqual(len(legacy.calls), 1)
        self.assertEqual(result, expected)

    def test_search_models_without_resolver_always_uses_legacy(self) -> None:
        legacy = _RecordingLegacy()
        service = self._service(legacy, with_catalog=False)
        service.search_models("anything")
        self.assertEqual(len(legacy.calls), 1)

    def test_search_models_catalog_unavailable_surfaces_mcp_read_error(self) -> None:
        repo = LegacyCodexModelRepository(
            FIXTURES / "legacy_agent",
            default_connection_id=CONNECTION,
        )
        catalog = JsonFixtureCatalogCacheRepository(FIXTURES / "catalog" / "missing.json")
        list_models = ListModelsUseCase(repo, catalog_reader=catalog)
        service = McpModelReadService(
            list_models=list_models,
            legacy=_RecordingLegacy(),
            presentation=_FakePresentation(),
            catalog_resolver=_FakeCatalogResolver(),
        )
        with self.assertRaises(McpReadError) as ctx:
            service.search_models("alpha", endpoint="OpenRouter")
        self.assertIn("catalog", str(ctx.exception).casefold())

    def test_search_models_clamps_limit(self) -> None:
        service = self._service()
        result = service.search_models("openrouter", limit=200)
        self.assertLessEqual(len(result["models"]), 50)

    def test_engine_registered_list_has_no_live_io(self) -> None:
        service = self._service()
        result = service.list_added_models()
        self.assertIn("models", result)
        self.assertNotIn("Keychain", json.dumps(result))


class DelegatingLegacyAdapterTests(unittest.TestCase):
    def test_delegating_adapter_wraps_deck_error(self) -> None:
        from model_deck.integrations.clients.mcp.legacy_application_adapter import (
            DelegatingLegacyMcpApplicationAdapter,
        )

        class DeckError(Exception):
            pass

        class _Deck:
            def list_endpoints(self):
                raise DeckError("no endpoints")

        adapter = DelegatingLegacyMcpApplicationAdapter(_Deck())
        with self.assertRaises(McpReadError) as ctx:
            adapter.list_endpoints()
        self.assertEqual(str(ctx.exception), "no endpoints")

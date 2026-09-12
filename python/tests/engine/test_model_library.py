import tempfile
import unittest
from pathlib import Path

from model_deck.adapters.storage.json_catalog_cache import JsonFixtureCatalogCacheRepository
from model_deck.engine.model_library.ports import CatalogUnavailableError
from model_deck.engine.model_library.use_cases import ListModelsUseCase, UnsupportedCollectionError

from model_deck.integrations.hosts.codex.legacy_models import LegacyCodexModelRepository

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CONNECTION = "550e8400-e29b-41d4-a716-446655440002"
OTHER_CONNECTION = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
CATALOG_FIXTURE = FIXTURES / "catalog" / "openrouter_sample.json"


class ModelLibraryTests(unittest.TestCase):
    def _registered_use_case(self) -> ListModelsUseCase:
        repo = LegacyCodexModelRepository(
            FIXTURES / "legacy_agent",
            default_connection_id=CONNECTION,
        )
        return ListModelsUseCase(repo)

    def _catalog_use_case(self) -> ListModelsUseCase:
        repo = LegacyCodexModelRepository(
            FIXTURES / "legacy_agent",
            default_connection_id=CONNECTION,
        )
        catalog = JsonFixtureCatalogCacheRepository(CATALOG_FIXTURE)
        return ListModelsUseCase(repo, catalog_reader=catalog)

    def test_list_registered_from_legacy_fixture(self) -> None:
        result = self._registered_use_case().execute({"collection": "registered"})
        self.assertEqual(result["collection"], "registered")
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual(item["kind"], "registered")
        self.assertEqual(item["provider_model_id"], "qwen/test")
        self.assertEqual(item["connection_id"], CONNECTION)

    def test_catalog_collection_requires_connection_id(self) -> None:
        use_case = self._catalog_use_case()
        with self.assertRaises(ValueError) as ctx:
            use_case.execute({"collection": "catalog"})
        self.assertIn("connection_id", str(ctx.exception))

    def test_catalog_unavailable_without_reader(self) -> None:
        use_case = self._registered_use_case()
        with self.assertRaises(CatalogUnavailableError):
            use_case.execute({"collection": "catalog", "connection_id": CONNECTION})

    def test_catalog_result_shape(self) -> None:
        result = self._catalog_use_case().execute(
            {"collection": "catalog", "connection_id": CONNECTION, "limit": 2}
        )
        self.assertEqual(result["collection"], "catalog")
        self.assertTrue(result["cache_only"])
        self.assertEqual(len(result["items"]), 2)
        item = result["items"][0]
        self.assertEqual(item["kind"], "catalog")
        self.assertEqual(item["connection_id"], CONNECTION)
        self.assertIn("provider_model_id", item)
        self.assertIn("display_name", item)
        self.assertEqual(item["catalog_revision"], "fixture-rev-1")
        self.assertIn("next_cursor", result)

    def test_catalog_query_filters_display_name(self) -> None:
        result = self._catalog_use_case().execute(
            {
                "collection": "catalog",
                "connection_id": CONNECTION,
                "query": "beta",
            }
        )
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["provider_model_id"], "openrouter/beta/two")

    def test_catalog_pagination_with_limit_and_cursor(self) -> None:
        use_case = self._catalog_use_case()
        first = use_case.execute(
            {"collection": "catalog", "connection_id": CONNECTION, "limit": 2}
        )
        self.assertEqual(len(first["items"]), 2)
        cursor = first["next_cursor"]
        self.assertIsNotNone(cursor)
        second = use_case.execute(
            {
                "collection": "catalog",
                "connection_id": CONNECTION,
                "limit": 2,
                "cursor": cursor,
            }
        )
        self.assertEqual(len(second["items"]), 2)
        ids_first = {item["provider_model_id"] for item in first["items"]}
        ids_second = {item["provider_model_id"] for item in second["items"]}
        self.assertTrue(ids_first.isdisjoint(ids_second))

    def test_catalog_missing_cache_for_connection(self) -> None:
        use_case = self._catalog_use_case()
        with self.assertRaises(CatalogUnavailableError):
            use_case.execute({"collection": "catalog", "connection_id": OTHER_CONNECTION})

    def test_catalog_missing_fixture_path(self) -> None:
        repo = LegacyCodexModelRepository(FIXTURES / "legacy_agent", default_connection_id=CONNECTION)
        catalog = JsonFixtureCatalogCacheRepository(FIXTURES / "catalog" / "missing.json")
        use_case = ListModelsUseCase(repo, catalog_reader=catalog)
        with self.assertRaises(CatalogUnavailableError):
            use_case.execute({"collection": "catalog", "connection_id": CONNECTION})

    def test_catalog_does_not_fall_back_to_registered(self) -> None:
        use_case = self._catalog_use_case()
        result = use_case.execute(
            {"collection": "catalog", "connection_id": CONNECTION, "query": "qwen"}
        )
        self.assertEqual(result["items"], [])
        registered = self._registered_use_case().execute({"collection": "registered"})
        self.assertEqual(len(registered["items"]), 1)
        self.assertEqual(registered["items"][0]["provider_model_id"], "qwen/test")

    def test_unsupported_collection_is_explicit(self) -> None:
        use_case = self._registered_use_case()
        with self.assertRaises(UnsupportedCollectionError):
            use_case.execute({"collection": "unknown"})

import unittest
from unittest.mock import patch

import model_catalog as catalog
from provider_connections import load_provider_presets


def native(model="gpt-example", hidden=False):
    return {"model": model, "displayName": "Native display", "description": "Native description", "hidden": hidden}


class ModelCatalogTests(unittest.TestCase):
    @patch.object(catalog, "collect_openrouter_models")
    @patch.object(catalog, "collect_openai_models")
    def test_cached_injected_models_use_current_registry_and_drop_removed_models(self, native_models, registered_models):
        openai = {"model": "gpt-example", "description": "Native model", "provider": "openai"}
        native_models.return_value = ({"ok": True}, [openai,
            {"model": "cursor/composer-2.5", "description": "Routed through Cursor SDK by Model Deck. Old name"},
            {"model": "qwen/removed", "description": "Routed to OpenRouter by Model Deck. Uses OpenRouter credits."}])
        cursor = {"model": "cursor/composer-2.5", "provider": "cursor", "display_name": "Composer 2.5"}
        registered_models.return_value = ({"ok": True}, [cursor])
        result = catalog.collect_catalog()
        self.assertTrue(result["openrouter"]["ok"])
        self.assertEqual(result["models"], [openai, cursor])

    @patch.object(catalog, "collect_openrouter_models", return_value=({"ok": True}, [{"model": "native/model"}]))
    @patch.object(catalog, "collect_openai_models", return_value=({"ok": True}, [{"model": "native/model", "description": "Native model"}]))
    def test_actual_native_registration_conflict_still_fails(self, native_models, registered_models):
        result = catalog.collect_catalog()
        self.assertFalse(result["openrouter"]["ok"])
        self.assertEqual(len(result["models"]), 1)

    @patch.object(catalog.pricing, "load")
    @patch.object(catalog, "RoutingRegistry")
    def test_cursor_catalog_has_billing_without_invented_zero_price(self, registry_type, price_loader):
        registry = registry_type.return_value
        registry.load_models.return_value = {"cursor/auto": {"role": "openrouter_cursor_auto", "endpoint": {
            "name": "Cursor", "cursor": True, "openrouter": False, "has_key": True}}}
        registry.load_display_names.return_value = {}
        registry.display_name_for.return_value = "Cursor Auto"
        status, rows = catalog.collect_openrouter_models()
        self.assertTrue(status["ok"])
        self.assertEqual(rows[0]["provider"], "cursor")
        self.assertTrue(rows[0]["cursor"])
        self.assertTrue(rows[0]["billed"])
        self.assertEqual(rows[0]["pricing"], "")
        self.assertIn("overages apply", rows[0]["description"])
        price_loader.assert_not_called()

    @patch.object(catalog.pricing, "load")
    @patch.object(catalog, "RoutingRegistry")
    def test_registered_direct_models_expose_provider_identity_and_authoritative_route(self, registry_type, price_loader):
        registry = registry_type.return_value
        presets = load_provider_presets()
        registry.load_models.return_value = {
            preset["default_models"][0]: {"role": "fixture_" + preset["id"], "endpoint": {
                "name": "My " + preset["name"], "base_url": preset["base_url"], "wire": preset["wire"],
                "account": "12345678-1234-1234-1234-123456789abc", "has_key": True, "openrouter": False}}
            for preset in presets}
        registry.load_display_names.return_value = {}
        registry.display_name_for.side_effect = lambda model: model
        status, rows = catalog.collect_openrouter_models()
        self.assertTrue(status["ok"])
        by_provider = {row["provider"]: row for row in rows}
        self.assertEqual(set(by_provider), {"kimi", "zai", "minimax", "google", "deepseek"})
        for preset in presets:
            row = by_provider[preset["id"]]
            with self.subTest(provider=preset["id"]):
                self.assertEqual(row["provider_name"], preset["name"])
                self.assertEqual(row["billing"], preset["billing"])
                self.assertEqual(row["billing_note"], preset["billing_note"])
                self.assertEqual(row["endpoint_base_url"], preset["base_url"])
                self.assertEqual(row["endpoint_wire"], preset["wire"])
                self.assertEqual(row["endpoint_account"], "12345678-1234-1234-1234-123456789abc")
                self.assertEqual(row["pricing"], "")
                self.assertIsNone(row["context"])
                self.assertFalse(row["openrouter"])
                self.assertFalse(row["cursor"])
        price_loader.assert_not_called()

    @patch.object(catalog.pricing, "load")
    @patch.object(catalog, "RoutingRegistry")
    def test_custom_endpoint_does_not_claim_openrouter_or_known_provider(self, registry_type, price_loader):
        registry = registry_type.return_value
        registry.load_models.return_value = {"deepseek-local": {"role": "fixture_local", "endpoint": {
            "name": "Local Server", "base_url": "http://localhost:1234/v1", "wire": "chat", "has_key": False, "openrouter": False}}}
        registry.load_display_names.return_value = {}
        registry.display_name_for.return_value = "DeepSeek Local"
        status, rows = catalog.collect_openrouter_models()
        self.assertTrue(status["ok"])
        self.assertEqual(rows[0]["provider"], "custom")
        self.assertEqual(rows[0]["provider_name"], "Local Server")
        self.assertFalse(rows[0]["billed"])
        self.assertNotIn("OpenRouter", rows[0]["billing_note"])
        price_loader.assert_not_called()

    def test_native_metadata_preserved_hidden_filtered_null_cursor(self):
        rows, cursor, identifiers = catalog.parse_native_page({"data": [native(), native("hidden", True)], "nextCursor": None})
        self.assertIsNone(cursor)
        self.assertEqual(rows, [{"model": "gpt-example", "display_name": "Native display",
                                "description": "Native description", "provider": "openai", "role": None}])
        self.assertEqual(identifiers, {"gpt-example", "hidden"})

    def test_malformed_duplicate_entries_and_cursor_fail(self):
        for page in ({"data": [native(), native()]}, {"data": [dict(native(), hidden=None)]},
                     {"data": [dict(native(), model="bad model")]}, {"data": [], "nextCursor": 1},
                     {"data": [dict(native(), description=None)]}, {"data": None}):
            with self.subTest(page=page), self.assertRaises(ValueError):
                catalog.parse_native_page(page)

    @patch.object(catalog, "NativeUsageClient")
    def test_native_pagination_and_process_cleanup(self, client_type):
        client = client_type.return_value
        client.request.side_effect = [{}, {"data": [native("one")], "nextCursor": "next"},
                                      {"data": [native("two")], "nextCursor": None}]
        status, rows = catalog.collect_openai_models()
        self.assertTrue(status["ok"])
        self.assertEqual([row["model"] for row in rows], ["one", "two"])
        self.assertEqual(client.request.call_args_list[-1].args, ("model/list", {"cursor": "next", "limit": 100, "includeHidden": False}))
        client.close.assert_called_once()

    @patch.object(catalog, "NativeUsageClient")
    def test_duplicate_across_pages_and_cycle_fail_without_partial_rows(self, client_type):
        client = client_type.return_value
        for second in ({"data": [native("one")], "nextCursor": None}, {"data": [], "nextCursor": "next"}):
            client.request.side_effect = [{}, {"data": [native("one")], "nextCursor": "next"}, second]
            status, rows = catalog.collect_openai_models()
            self.assertFalse(status["ok"])
            self.assertEqual(rows, [])

    @patch.object(catalog.pricing, "load", return_value={})
    @patch.object(catalog, "RoutingRegistry")
    @patch.object(catalog, "collect_openai_models", return_value=({"ok": False, "error": "Unavailable"}, []))
    def test_openrouter_independent_and_registry_sanitized(self, native_models, registry_type, price_loader):
        registry_type.return_value.load_models.return_value = {"qwen/example": {"role": "openrouter_example", "config": {"private": "not exposed"}}}
        registry_type.return_value.load_display_names.return_value = {"qwen/example": "Example"}
        registry_type.return_value.display_name_for.side_effect = lambda model: "Example"
        result = catalog.collect_catalog()
        self.assertFalse(result["openai"]["ok"])
        self.assertTrue(result["openrouter"]["ok"])
        self.assertEqual(result["models"][0], {"model": "qwen/example", "display_name": "Example",
                                              "description": "Routed to OpenRouter by Model Deck. Uses OpenRouter credits.",
                                              "provider": "openrouter", "role": "openrouter_example", "custom_name": True,
                                              "provider_name": "OpenRouter", "billing": "OpenRouter credits", "billing_note": "Uses OpenRouter credits.",
                                              "endpoint": "OpenRouter", "billed": True, "pricing": "", "context": None,
                                              "endpoint_account": None, "endpoint_base_url": None, "endpoint_wire": "auto",
                                              "openrouter": True, "cursor": False})
        self.assertNotIn("not exposed", str(result))

    @patch.object(catalog, "RoutingRegistry", side_effect=ValueError("private error"))
    @patch.object(catalog, "collect_openai_models", return_value=({"ok": True}, [{"model": "gpt-example"}]))
    def test_native_independent_on_registry_failure(self, native_models, registry_type):
        result = catalog.collect_catalog()
        self.assertTrue(result["openai"]["ok"])
        self.assertFalse(result["openrouter"]["ok"])
        self.assertEqual(result["models"], [{"model": "gpt-example"}])
        self.assertNotIn("private error", str(result))

    @patch.object(catalog, "NativeUsageClient", side_effect=OSError("private error"))
    def test_native_failure_sanitized(self, client_type):
        status, rows = catalog.collect_openai_models()
        self.assertFalse(status["ok"])
        self.assertEqual(rows, [])
        self.assertNotIn("private error", str(status))


if __name__ == "__main__":
    unittest.main()

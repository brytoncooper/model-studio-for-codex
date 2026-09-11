import unittest
from unittest.mock import patch

import model_catalog as catalog


def native(model="gpt-example", hidden=False):
    return {"model": model, "displayName": "Native display", "description": "Native description", "hidden": hidden}


class ModelCatalogTests(unittest.TestCase):
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

    @patch.object(catalog, "RoutingRegistry")
    @patch.object(catalog, "collect_openai_models", return_value=({"ok": False, "error": "Unavailable"}, []))
    def test_openrouter_independent_and_registry_sanitized(self, native_models, registry_type):
        registry_type.return_value.load_models.return_value = {"qwen/example": {"role": "openrouter_example", "config": {"private": "not exposed"}}}
        result = catalog.collect_catalog()
        self.assertFalse(result["openai"]["ok"])
        self.assertTrue(result["openrouter"]["ok"])
        self.assertEqual(result["models"][0], {"model": "qwen/example", "display_name": "qwen/example · OpenRouter",
                                              "description": "Native agent model billed to OpenRouter API credits.",
                                              "provider": "openrouter", "role": "openrouter_example"})
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

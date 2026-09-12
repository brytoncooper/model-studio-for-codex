"""Fixture coverage for provider attribution and credential-scoped model discovery."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error

import provider_connections as connections


class ProviderPresetTests(unittest.TestCase):
    def test_five_direct_provider_presets_have_exact_routes(self):
        presets = connections.load_provider_presets()
        self.assertEqual({entry["id"] for entry in presets}, {"kimi", "zai", "minimax", "google", "deepseek"})
        for entry in presets:
            with self.subTest(provider=entry["id"]):
                self.assertEqual(connections.provider_for_base_url(entry["base_url"] + "/"), entry)
                self.assertIn(entry["wire"], ("chat", "responses"))
                self.assertTrue(entry["default_models"])
                self.assertIn(entry["billing_note"], connections.provider_billing_description(entry["base_url"]))

    def test_provider_matching_cannot_confuse_lookalike_urls_or_credentials(self):
        for entry in connections.load_provider_presets():
            scheme, rest = entry["base_url"].split("://", 1)
            host, separator, path = rest.partition("/")
            fake_urls = [f"{scheme}://{host}.evil.test/{path}", f"{scheme}://evil.{host}/{path}",
                         f"{scheme}://{host}@evil.test/{path}", f"{scheme}://user:secret@{rest}",
                         entry["base_url"] + "/extra", entry["base_url"] + "?key=secret",
                         entry["base_url"] + "#fragment", f"http://{rest}",
                         f"{scheme}://{host}:8443/{path}"]
            for url in fake_urls:
                with self.subTest(url=url):
                    self.assertIsNone(connections.provider_for_base_url(url))
        self.assertEqual(connections.provider_for_base_url("https://API.DEEPSEEK.COM:443/")["id"], "deepseek")

    def test_billing_notes_do_not_invent_subscription_coverage(self):
        presets = {entry["id"]: entry for entry in connections.load_provider_presets()}
        self.assertIn("membership", connections.provider_billing_description(presets["kimi"]["base_url"]))
        self.assertIn("Coding Plan", connections.provider_billing_description(presets["zai"]["base_url"]))
        self.assertIn("pay-as-you-go", connections.provider_billing_description(presets["minimax"]["base_url"]))
        self.assertIn("API", presets["minimax"]["billing"])
        google = connections.provider_billing_description(presets["google"]["base_url"])
        self.assertIn("API billing", google)
        self.assertIn("activate", google)
        self.assertIn("API balance", connections.provider_billing_description(presets["deepseek"]["base_url"]))
        self.assertIn("No provider key", connections.provider_billing_description(presets["kimi"]["base_url"], False))
        self.assertNotIn("free", connections.provider_billing_description("https://example.com/v1", False))

    def test_corrupt_or_duplicate_bundled_presets_fail(self):
        valid = {"version": 1, "providers": connections.load_provider_presets()}
        bad_documents = [{"version": 2, "providers": valid["providers"]},
                         {"version": True, "providers": valid["providers"]},
                         {"version": 1, "providers": [valid["providers"][0]] * 2},
                         {"version": 1, "providers": [{**valid["providers"][0], "wire": "cursor"}]},
                         {"version": 1, "providers": [{**valid["providers"][0], "default_models": ["bad id"]}]}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "providers.json"
            for document in bad_documents:
                path.write_text(json.dumps(document))
                with self.subTest(document=document), mock.patch.object(connections, "PRESETS_PATH", path):
                    with self.assertRaises(connections.ProviderConnectionError):
                        connections.load_provider_presets()


class ProviderCatalogTests(unittest.TestCase):
    def setUp(self):
        self.base_url = "https://api.kimi.com/coding/v1"

    def test_remote_discovery_sends_only_this_routes_key_and_preserves_declared_capabilities(self):
        fetch = mock.Mock(return_value={"data": [{"id": "kimi-for-coding", "name": "Kimi Coding",
            "context_length": 100000, "tools": True, "architecture": {"input_modalities": ["text", "image"]},
            "description": "First line.\nSecond line."}, {"id": "plain-model"}]})
        result = connections.fetch_endpoint_models(self.base_url, "fixture-key", fetch)
        fetch.assert_called_once_with(self.base_url + "/models", {"Authorization": "Bearer fixture-key"})
        self.assertEqual(result["source"], "remote")
        self.assertTrue(result["verified"])
        self.assertIn("have not been verified", result["note"])
        self.assertEqual(result["models"][0]["context"], 100000)
        self.assertEqual(result["models"][0]["modalities"], ["text", "image"])
        self.assertEqual(result["models"][0]["description"], "First line. Second line.")
        self.assertNotIn("reasoning", result["models"][0])
        self.assertEqual(result["models"][1], {"id": "plain-model", "name": "plain-model"})
        self.assertNotIn("fixture-key", json.dumps(result))

    def test_each_provider_uses_its_own_models_url_without_rerouting(self):
        for preset in connections.load_provider_presets():
            with self.subTest(provider=preset["id"]):
                fetch = mock.Mock(return_value={"data": [{"id": "fixture-model"}]})
                connections.fetch_endpoint_models(preset["base_url"], preset["id"] + "-key", fetch)
                fetch.assert_called_once_with(preset["base_url"] + "/models", {"Authorization": "Bearer " + preset["id"] + "-key"})

    def test_unsupported_catalog_returns_explicitly_unverified_suggestions(self):
        for status in (404, 405, 501):
            fetch = mock.Mock(side_effect=urllib.error.HTTPError(self.base_url + "/models", status, "private upstream body", {}, io.BytesIO()))
            with self.subTest(status=status):
                result = connections.fetch_endpoint_models(self.base_url, "fixture-key", fetch)
                self.assertEqual(result["source"], "suggested")
                self.assertFalse(result["verified"])
                self.assertIn("unverified", result["note"])
                self.assertEqual([row["id"] for row in result["models"]], connections.provider_for_base_url(self.base_url)["default_models"])
                self.assertNotIn("context", result["models"][0])

    def test_access_rate_limit_server_and_network_errors_never_fall_back(self):
        failures = [urllib.error.HTTPError(self.base_url, status, "private token", {}, io.BytesIO())
                    for status in (401, 403, 429, 500, 502, 302)]
        failures.extend([TimeoutError("private token"), urllib.error.URLError("private token"), ValueError("private token")])
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), self.assertRaises(connections.ProviderConnectionError) as caught:
                connections.fetch_endpoint_models(self.base_url, "fixture-key", mock.Mock(side_effect=failure))
            self.assertNotIn("private token", str(caught.exception))
            self.assertNotIn("fixture-key", str(caught.exception))

    def test_unknown_endpoint_404_never_borrows_another_providers_suggestions(self):
        fetch = mock.Mock(side_effect=urllib.error.HTTPError("https://example.com/v1/models", 404, "missing", {}, io.BytesIO()))
        with self.assertRaises(connections.ProviderConnectionError):
            connections.fetch_endpoint_models("https://example.com/v1", "fixture-key", fetch)

    def test_google_catalog_names_limits_and_non_generation_models(self):
        fetch = mock.Mock(return_value={"models": [
            {"name": "models/gemini-fixture", "displayName": "Gemini Fixture", "inputTokenLimit": 900000,
             "outputTokenLimit": 50000, "supportedGenerationMethods": ["generateContent", "countTokens"]},
            {"name": "models/embedding-fixture", "supportedGenerationMethods": ["embedContent"]}]})
        result = connections.fetch_endpoint_models("https://generativelanguage.googleapis.com/v1beta/openai", "fixture-key", fetch)
        self.assertEqual(result["models"], [{"id": "gemini-fixture", "name": "Gemini Fixture", "context": 900000, "output_limit": 50000}])

    def test_malformed_duplicate_and_oversized_catalogs_fail_without_partial_success(self):
        documents = [None, [], {}, {"data": None}, {"data": [{"id": "valid"}, {"id": 42}]},
                     {"data": [{"id": "repeated"}, {"id": "repeated"}]}, {"data": [{"id": "bad id"}]},
                     {"data": [{"id": "a" * 257}]}, {"data": [{"id": "x", "tools": "true"}]},
                     {"data": [{"id": "x", "context_length": True}]}, {"data": [{"id": "x", "modalities": "text"}]},
                     {"data": [{"id": str(index)} for index in range(connections.MAX_CATALOG_MODELS + 1)]},
                     {"data": [], "has_more": True}, {"models": [], "nextPageToken": "next"}]
        for document in documents:
            with self.subTest(document_type=type(document).__name__), self.assertRaises(connections.ProviderConnectionError):
                connections.fetch_endpoint_models(self.base_url, "fixture-key", mock.Mock(return_value=document))
        empty = connections.fetch_endpoint_models(self.base_url, "fixture-key", mock.Mock(return_value={"data": []}))
        self.assertEqual(empty["models"], [])
        self.assertEqual(empty["source"], "remote")

    def test_invalid_url_or_key_stops_before_fetch(self):
        fetch = mock.Mock()
        for url, key in [(self.base_url + "?key=secret", "fixture"), ("https://user:pass@api.kimi.com/coding/v1", "fixture"),
                         (self.base_url, "line\nbreak"), (self.base_url, ""), ("http://example.com/v1", "fixture")]:
            with self.subTest(url=url), self.assertRaises(connections.ProviderConnectionError):
                connections.fetch_endpoint_models(url, key, fetch)
        fetch.assert_not_called()

    @mock.patch.object(connections.urllib.request, "build_opener")
    def test_default_transport_is_bounded_and_rejects_redirects(self, build_opener):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"data": []}'
        opener = build_opener.return_value
        opener.open.return_value = response
        result = connections.fetch_endpoint_models(self.base_url, "fixture-key")
        self.assertEqual(result["models"], [])
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, self.base_url + "/models")
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-key")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], connections.CATALOG_TIMEOUT_SECONDS)
        response.read.assert_called_once_with(connections.MAX_CATALOG_BYTES + 1)
        redirect_handler = build_opener.call_args.args[0]
        self.assertIsNone(redirect_handler.redirect_request(request, None, 302, "found", {}, "https://other.example/models"))

    @mock.patch.object(connections.urllib.request, "build_opener")
    def test_transport_rejects_oversized_or_invalid_json(self, build_opener):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        build_opener.return_value.open.return_value = response
        for body in (b"x" * (connections.MAX_CATALOG_BYTES + 1), b"not-json", b"\xff"):
            response.read.return_value = body
            with self.subTest(size=len(body)), self.assertRaises(connections.ProviderConnectionError):
                connections.fetch_endpoint_models(self.base_url, "fixture-key")


if __name__ == "__main__":
    unittest.main()

"""The Model Deck MCP server: discovery, adding, removing, and the JSON-RPC surface."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error
import os
import shutil
import subprocess
import sys

import model_benchmarks as bm
from test_model_benchmarks import CATALOG

import model_deck_mcp as mcp
from model_deck.engine.model_library.use_cases import ListModelsUseCase
from provider_connections import load_provider_presets
from routing_registry import AGENT_MARKER, RoutingRegistry

ACCOUNT = "12345678-1234-1234-1234-123456789abc"
PRICING = {
    "deepseek/deepseek-v4.1-flash": {"name": "DeepSeek: V4.1 Flash", "input": 0.15, "output": 0.6, "cache_read": 0.003, "cache_write": None,
                                     "context": 1048576, "modalities": ["text"], "tools": True, "reasoning": True, "description": "Fast coder"},
    "moonshotai/kimi-k2.5": {"name": "Kimi K2.5", "input": 0.5, "output": 2.0, "cache_read": None, "cache_write": None,
                             "context": 262144, "modalities": ["text", "image"], "tools": True, "reasoning": False, "description": "Big"},
}


class DeckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "agents").mkdir()
        self.registry = RoutingRegistry(root / "agents", preferences_path=root / "preferences.json",
                                        selection_path=root / "selection.json", display_names_path=root / "names.json")
        self.registry.endpoints_path = root / "endpoints.json"
        self.registry.endpoints_path.write_text(json.dumps({
            ACCOUNT: {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "wire": "auto"},
            "http://localhost:1234/v1": {"name": "LM Studio", "base_url": "http://localhost:1234/v1", "wire": "chat"},
            "bad": {"name": "Bad", "base_url": "ftp://nope"},
        }))
        self.fetched = []
        self.deck = mcp.Deck(self.registry, pricing_loader=lambda: PRICING, executable="/Applications/Model Deck.app/Contents/MacOS/ModelDeck",
                             fetch=self.fake_fetch, settings={"config_path": str(root / "config.toml"), "state_dir": str(root / "state"),
                                                              "agents_dir": str(root / "agents")})

    def fake_fetch(self, url, headers):
        self.fetched.append((url, headers))
        return {"data": [{"id": "qwen2.5-0.5b-instruct-mlx"}, {"id": "minicpm5-2b"}]}

    def test_endpoints_are_listed_and_invalid_ones_skipped(self):
        listed = self.deck.list_endpoints()["endpoints"]
        self.assertEqual([(e["name"], e["billing"], e["format"]) for e in listed], [("OpenRouter", "OpenRouter credits", "auto"), ("LM Studio", "none (local)", "chat")])
        self.assertEqual(self.deck.endpoint_named()["name"], "OpenRouter")
        self.assertEqual(self.deck.endpoint_named("lm studio")["base_url"], "http://localhost:1234/v1")
        with self.assertRaises(mcp.DeckError):
            self.deck.endpoint_named("DeepSeek")

    def test_rpc_registered_reads_use_application_and_preserve_envelope(self):
        self.deck.add_model("deepseek/deepseek-v4.1-flash", endpoint="OpenRouter")
        expected = self.deck.list_added_models()
        original = ListModelsUseCase.execute
        calls = []

        def record(use_case, params):
            calls.append(params)
            return original(use_case, params)

        with mock.patch.object(ListModelsUseCase, "execute", record):
            result = mcp.handle_message(self.deck, {"jsonrpc": "2.0", "id": 1,
                "method": "tools/call", "params": {"name": "list_added_models", "arguments": {}}})
        self.assertFalse(result["result"]["isError"])
        self.assertEqual(json.loads(result["result"]["content"][0]["text"]), expected)
        self.assertEqual(calls, [{"collection": "registered"}])

    def test_read_composition_refreshes_mixed_routes_after_each_mutation(self):
        self.deck.call("add_model", {"model": "deepseek/deepseek-v4.1-flash", "endpoint": "OpenRouter"})
        self.deck.call("add_model", {"model": "local-fixture", "endpoint": "LM Studio"})
        with mock.patch.object(self.deck, "list_added_models", side_effect=AssertionError("recursive legacy read")):
            first = self.deck.call("list_added_models", {})
        self.assertEqual(first, self.deck.list_added_models())
        self.assertIsNone(first["models"][0]["billing_note"])
        self.deck.call("set_display_name", {"model": "local-fixture", "name": "Renamed Local"})
        self.assertEqual(self.deck.call("list_added_models", {}), self.deck.list_added_models())
        self.deck.call("remove_model", {"model": "local-fixture"})
        self.assertEqual(self.deck.call("list_added_models", {}), self.deck.list_added_models())

    def test_read_composition_keeps_account_and_unkeyed_connection_scope(self):
        models = {
            "a": {"role": "role_a", "endpoint": {"account": ACCOUNT, "base_url": "https://one.test", "has_key": True}},
            "b": {"role": "role_b", "endpoint": {"account": "22345678-1234-1234-1234-123456789abc", "base_url": "https://one.test", "has_key": True}},
            "c": {"role": "role_c", "endpoint": {"base_url": "http://local.test", "has_key": False}},
        }
        captured = []
        original = ListModelsUseCase.execute

        def capture(use_case, params):
            result = original(use_case, params)
            captured.append(result)
            return result

        with mock.patch.object(self.registry, "load_models", return_value=models), \
                mock.patch.object(ListModelsUseCase, "execute", capture):
            self.assertEqual(self.deck.call("list_added_models", {}), self.deck.list_added_models())
            self.deck.call("list_added_models", {})
        identities = [row["connection_id"] for row in captured[0]["items"]]
        self.assertEqual(len(set(identities)), 3)
        self.assertEqual(captured[0], captured[1])
        self.assertNotIn("https://", json.dumps(identities))

    def test_composed_search_preserves_legacy_output_and_validation(self):
        for arguments in ({"query": "kimi", "endpoint": "openrouter"},
                          {"query": "qwen", "endpoint": "LM Studio"}):
            with self.subTest(arguments=arguments):
                expected = self.deck.search_models(**arguments)
                self.assertEqual(self.deck.call("search_models", arguments), expected)
        with mock.patch.object(self.deck, "_model_read_service", side_effect=AssertionError("validation bypassed")):
            with self.assertRaisesRegex(mcp.DeckError, "Unknown arguments"):
                self.deck.call("list_added_models", {"unexpected": True})

    def test_same_account_captured_urls_keep_exact_legacy_billing(self):
        self.deck.add_model("deepseek/deepseek-v4.1-flash", endpoint="OpenRouter")
        self.registry.endpoints_path.write_text(json.dumps({ACCOUNT: {
            "name": "Private endpoint", "base_url": "https://private.test/v1/", "wire": "chat"}}))
        self.deck.add_model("private-model", endpoint="Private endpoint")
        expected = self.deck.list_added_models()
        self.assertEqual([row["billing"] for row in expected["models"]], ["OpenRouter credits", "API key"])
        self.assertEqual(self.deck.call("list_added_models", {}), expected)

    def test_same_account_and_url_with_different_wire_get_distinct_routes(self):
        models = {model_id: {"role": "role_" + model_id, "endpoint": {
            "account": ACCOUNT, "base_url": "https://private.test/v1", "wire": wire,
            "has_key": True, "openrouter": False}}
            for model_id, wire in (("a", "chat"), ("b", "responses"))}
        captured = []
        original = ListModelsUseCase.execute

        def capture(use_case, params):
            result = original(use_case, params)
            captured.extend(row["connection_id"] for row in result["items"])
            return result

        with mock.patch.object(self.registry, "load_models", return_value=models), \
                mock.patch.object(ListModelsUseCase, "execute", capture):
            self.assertEqual(self.deck.call("list_added_models", {}), self.deck.list_added_models())
        self.assertEqual(len(set(captured)), 2)
        self.assertNotIn("https://", json.dumps(captured))

    def test_composed_search_routes_errors_and_keys_through_selected_account(self):
        self.registry.endpoints_path.write_text(json.dumps({
            ACCOUNT: {"name": "First", "base_url": "https://first.test/v1", "wire": "chat"},
            "22345678-1234-1234-1234-123456789abc": {
                "name": "Second", "base_url": "https://second.test/v1", "wire": "chat"}}))
        with mock.patch.object(self.deck, "_key_for_account", return_value="fixture-only") as key:
            self.deck.call("search_models", {"query": "qwen", "endpoint": "Second"})
        key.assert_called_once_with("22345678-1234-1234-1234-123456789abc")
        self.assertEqual(self.fetched[-1], ("https://second.test/v1/models", {"Authorization": "Bearer fixture-only"}))
        result = mcp.handle_message(self.deck, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "search_models", "arguments": {"query": "", "endpoint": "missing"}}})
        self.assertTrue(result["result"]["isError"])
        self.assertIn("No endpoint named", result["result"]["content"][0]["text"])

    def test_source_and_packaged_loader_have_explicit_package_roots(self):
        root = Path(mcp.__file__).resolve().parent
        request = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {}}) + "\n"
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        stage = Path(self.temp.name) / "stage"
        stage.mkdir()
        for filename in ("model_deck_mcp.py", "codex_settings.py", "pricing.py", "model_benchmarks.py",
                         "provider_connections.py", "routing_registry.py"):
            shutil.copyfile(root / filename, stage / filename)
        for directory in (root, stage):
            with self.subTest(directory=directory):
                if directory == stage:
                    shutil.copytree(root / "python/src/model_deck", stage / "vendor/model_deck",
                                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                result = subprocess.run([sys.executable, "-B", str(directory / "model_deck_mcp.py")],
                    input=request, text=True, capture_output=True, timeout=10, cwd=self.temp.name, env=environment)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["result"]["serverInfo"], mcp.SERVER_INFO)

    def test_missing_packaged_engine_fails_before_protocol_or_live_access(self):
        stage = Path(self.temp.name) / "missing"
        stage.mkdir()
        shutil.copyfile(mcp.__file__, stage / "model_deck_mcp.py")
        result = subprocess.run([sys.executable, "-I", str(stage / "model_deck_mcp.py")],
            input="", text=True, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("requires its packaged engine service", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_preferences_fallback_when_endpoints_file_is_missing(self):
        self.registry.endpoints_path.unlink()
        self.registry.preferences_path.write_text(json.dumps({"accounts": [{"id": ACCOUNT, "name": "Codex Key"},
                                                                            {"id": "x", "name": "Local", "baseURL": "http://localhost:11434/v1", "hasKey": False}]}))
        listed = self.deck.endpoints()
        self.assertEqual([(e["name"], e["keyed"], e["openrouter"]) for e in listed], [("Codex Key", True, True), ("Local", False, False)])

    def test_search_openrouter_ranks_ids_and_local_lists_server_models(self):
        result = self.deck.search_models("kimi")
        self.assertEqual([row["id"] for row in result["models"]], ["moonshotai/kimi-k2.5"])
        self.assertEqual(result["models"][0]["price"], "in $0.5/M · out $2/M · 262k context")
        self.assertEqual(self.deck.search_models("", limit=1)["models"][0]["id"], "deepseek/deepseek-v4.1-flash")
        local = self.deck.search_models("qwen", endpoint="LM Studio")
        self.assertEqual([row["id"] for row in local["models"]], ["qwen2.5-0.5b-instruct-mlx"])
        self.assertEqual(local["models"][0]["price"], "not listed")
        self.assertEqual(self.fetched, [("http://localhost:1234/v1/models", {})])

    def test_direct_provider_catalogs_use_saved_account_keys_and_truthful_billing(self):
        for preset in load_provider_presets():
            self.registry.endpoints_path.write_text(json.dumps({ACCOUNT: {
                "name": "My " + preset["name"], "base_url": preset["base_url"], "wire": preset["wire"]}}))
            model = preset["default_models"][0]
            self.deck.fetch = mock.Mock(return_value={"data": [{"id": model, "name": "Fixture Model", "tools": True}]})
            with self.subTest(provider=preset["id"]), mock.patch.object(self.deck, "_key_for_account", return_value="fixture-key") as key:
                endpoint = self.deck.list_endpoints()["endpoints"][0]
                self.assertEqual(endpoint["billing"], preset["billing"])
                self.assertEqual(endpoint["billing_note"], preset["billing_note"])
                result = self.deck.search_models("fixture", endpoint="My " + preset["name"])
                key.assert_called_once_with(ACCOUNT)
                self.deck.fetch.assert_called_once_with(preset["base_url"] + "/models", {"Authorization": "Bearer fixture-key"})
                self.assertEqual(result["models"][0]["id"], model)
                self.assertEqual(result["billing"], preset["billing"])
                self.assertEqual(result["source"], "remote")
                self.assertTrue(result["verified"])
                self.assertTrue(result["models"][0]["tools"])
                self.assertNotIn("fixture-key", json.dumps(result))

    def test_subscription_catalog_suggestions_do_not_hide_bad_credentials(self):
        preset = next(entry for entry in load_provider_presets() if entry["id"] == "kimi")
        self.registry.endpoints_path.write_text(json.dumps({ACCOUNT: {"name": preset["name"], "base_url": preset["base_url"], "wire": "chat"}}))
        with mock.patch.object(self.deck, "_key_for_account", return_value="fixture-key"):
            self.deck.fetch = mock.Mock(side_effect=urllib.error.HTTPError(preset["base_url"], 404, "missing", {}, io.BytesIO()))
            result = self.deck.search_models("", preset["name"])
            self.assertEqual(result["source"], "suggested")
            self.assertFalse(result["verified"])
            self.assertEqual([row["id"] for row in result["models"]], preset["default_models"])
            self.deck.fetch = mock.Mock(side_effect=urllib.error.HTTPError(preset["base_url"], 401, "secret error", {}, io.BytesIO()))
            with self.assertRaises(mcp.DeckError) as caught:
                self.deck.search_models("", preset["name"])
            self.assertNotIn("secret error", str(caught.exception))
            self.assertNotIn("fixture-key", str(caught.exception))

    def test_catalog_keys_stay_with_the_selected_saved_account(self):
        kimi, deepseek = [next(entry for entry in load_provider_presets() if entry["id"] == identifier)
                          for identifier in ("kimi", "deepseek")]
        second_account = "22345678-1234-1234-1234-123456789abc"
        self.registry.endpoints_path.write_text(json.dumps({
            ACCOUNT: {"name": "Kimi", "base_url": kimi["base_url"], "wire": "chat"},
            second_account: {"name": "DeepSeek", "base_url": deepseek["base_url"], "wire": "chat"}}))
        with mock.patch.object(self.deck, "_key_for_account", side_effect=lambda account: "kimi-key" if account == ACCOUNT else "deepseek-key"):
            self.deck.search_models("", "DeepSeek")
            self.deck.search_models("", "Kimi")
        self.assertEqual(self.fetched, [(deepseek["base_url"] + "/models", {"Authorization": "Bearer deepseek-key"}),
                                       (kimi["base_url"] + "/models", {"Authorization": "Bearer kimi-key"})])

    def test_malformed_or_duplicate_endpoint_models_are_errors(self):
        for document in ({"data": [{"id": "valid"}, {"id": 7}]}, {"data": [{"id": "same"}, {"id": "same"}]}):
            self.deck.fetch = mock.Mock(return_value=document)
            with self.subTest(document=document), self.assertRaises(mcp.DeckError):
                self.deck.search_models("", "LM Studio")

    def test_registered_direct_models_keep_provider_billing_and_wire(self):
        preset = next(entry for entry in load_provider_presets() if entry["id"] == "zai")
        self.registry.endpoints_path.write_text(json.dumps({ACCOUNT: {"name": "My Z.AI", "base_url": preset["base_url"], "wire": preset["wire"]}}))
        with mock.patch.object(mcp.codex_settings, "handle", return_value={"agent_name": "fixture_role"}) as settings:
            result = self.deck.add_model(preset["default_models"][0], "My Z.AI")
        request = settings.call_args.args[0]
        self.assertEqual(request["wire"], "responses")
        self.assertEqual(request["account"], ACCOUNT)
        self.assertEqual(request["base_url"], preset["base_url"])
        self.assertEqual(result["billing"], "GLM Coding Plan")
        with mock.patch.object(self.registry, "load_models", return_value={preset["default_models"][0]: {
            "role": "fixture_role", "endpoint": {"name": "My Z.AI", "base_url": preset["base_url"],
                                                 "has_key": True, "account": ACCOUNT, "wire": "responses"}}}):
            rows = self.deck.list_added_models()["models"]
        self.assertEqual(rows[0]["billing"], "GLM Coding Plan")
        self.assertEqual(rows[0]["billing_note"], preset["billing_note"])

    def test_readding_same_route_reports_existing_registration(self):
        with mock.patch.object(mcp.codex_settings, "handle", return_value={"agent_name": "fixture_role", "already_registered": True}):
            result = self.deck.add_model("deepseek/deepseek-v4.1-flash", "OpenRouter")
        self.assertTrue(result["already_registered"])
        self.assertIn("already added", result["message"])

    def test_add_list_rename_and_remove(self):
        added = self.deck.add_model("deepseek/deepseek-v4.1-flash", display_name="Flash")
        self.assertTrue(added["ok"])
        self.assertIn("spawn_agent", added["message"])
        role_file = Path(self.temp.name) / "agents" / (added["role"] + ".toml")
        text = role_file.read_text()
        self.assertTrue(text.startswith(AGENT_MARKER))
        self.assertIn('args = ["--token", "%s"]' % ACCOUNT, text)
        local = self.deck.add_model("qwen2.5-0.5b-instruct-mlx", endpoint="LM Studio")
        self.assertNotIn("auth", (Path(self.temp.name) / "agents" / (local["role"] + ".toml")).read_text())
        rows = self.deck.list_added_models()["models"]
        self.assertEqual([(row["id"], row["name"], row["endpoint"], row["billing"]) for row in rows],
                         [("deepseek/deepseek-v4.1-flash", "Flash", "OpenRouter", "OpenRouter credits"),
                          ("qwen2.5-0.5b-instruct-mlx", "Qwen 2.5 0.5B Instruct Mlx", "LM Studio", "none (local)")])
        self.assertEqual(rows[0]["price"], "in $0.15/M · out $0.6/M · cached $0.003/M · 1.04858M context")
        self.assertEqual(self.deck.set_display_name("deepseek/deepseek-v4.1-flash", "")["name"], "DeepSeek V4.1 Flash")
        removed = self.deck.remove_model("deepseek/deepseek-v4.1-flash")
        self.assertFalse(role_file.exists())
        self.assertEqual(removed["role"], added["role"])
        self.assertEqual([row["id"] for row in self.deck.list_added_models()["models"]], ["qwen2.5-0.5b-instruct-mlx"])
        with self.assertRaises(mcp.DeckError):
            self.deck.remove_model("deepseek/deepseek-v4.1-flash")

    def test_refusals(self):
        with self.assertRaises(mcp.DeckError):
            self.deck.add_model("gpt-5.5")
        with self.assertRaises(mcp.DeckError):
            self.deck.add_model("bare-id")  # OpenRouter needs provider/model
        with self.assertRaises(mcp.DeckError):
            self.deck.add_model("a/b", endpoint="Nowhere")
        with self.assertRaises(mcp.DeckError):
            self.deck.add_model("a/b", effort="turbo")
        with self.assertRaises(mcp.DeckError):
            self.deck.set_display_name("not/added", "x")
        self.assertIn("Warning", self.deck.add_model("unknown/model")["message"])
        self.assertEqual(self.deck.model_pricing("nobody/nothing")["price"], "not listed on OpenRouter")
        self.assertEqual(self.deck.model_pricing("moonshotai/kimi-k2.5:nitro")["context"], 262144)

    def test_openrouter_alias_and_saved_endpoint_names(self):
        self.registry.endpoints_path.write_text(json.dumps({
            ACCOUNT: {"name": "Codex Key", "base_url": "https://openrouter.ai/api/v1", "wire": "auto"},
            "http://localhost:1234/v1": {"name": "LM Studio", "base_url": "http://localhost:1234/v1", "wire": "chat"}}))
        self.assertEqual(self.deck.endpoint_named("OpenRouter")["name"], "Codex Key")
        self.assertEqual(self.deck.endpoint_named()["name"], "Codex Key")
        added = self.deck.add_model("deepseek/deepseek-v4.1-flash", endpoint="openrouter")
        self.assertEqual(added["endpoint"], "Codex Key")
        local = self.deck.add_model("qwen2.5-0.5b-instruct-mlx", endpoint="LM Studio")
        self.assertEqual(local["endpoint"], "LM Studio")
        rows = {row["id"]: row["endpoint"] for row in self.deck.list_added_models()["models"]}
        self.assertEqual(rows, {"deepseek/deepseek-v4.1-flash": "Codex Key", "qwen2.5-0.5b-instruct-mlx": "LM Studio"})

    def test_json_rpc_surface(self):
        lines = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "search_models", "arguments": {"query": "deepseek"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "add_model", "arguments": {"model": "deepseek/deepseek-v4.1-flash", "bogus": 1}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 6, "method": "resources/list"},
            {"jsonrpc": "2.0", "id": 7, "method": "ping"},
        ]
        stdin = io.StringIO("\n".join(json.dumps(line) for line in lines) + "\nnot json\n")
        stdout = io.StringIO()
        mcp.serve(stdin, stdout, self.deck)
        replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3, 4, 5, 6, 7, None])
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual([tool["name"] for tool in replies[1]["result"]["tools"]],
                         ["list_endpoints", "search_models", "list_added_models", "add_model", "remove_model", "set_display_name", "model_pricing",
                          "cursor_status", "benchmark_status", "refresh_benchmarks", "model_benchmarks", "compare_models", "rank_models"])
        self.assertFalse(replies[2]["result"]["isError"])
        self.assertIn("deepseek/deepseek-v4.1-flash", replies[2]["result"]["content"][0]["text"])
        self.assertTrue(replies[3]["result"]["isError"])
        self.assertIn("bogus", replies[3]["result"]["content"][0]["text"])
        self.assertTrue(replies[4]["result"]["isError"])
        self.assertEqual(replies[5]["error"]["code"], -32601)
        self.assertEqual(replies[6]["result"], {})
        self.assertEqual(replies[7]["error"]["code"], -32700)

    def test_cursor_discovery_subscription_billing_and_registration_contract(self):
        self.registry.endpoints_path.write_text(json.dumps({ACCOUNT: {
            "name": "Cursor", "base_url": "https://api.cursor.com", "wire": "cursor"}}))
        self.assertEqual(self.deck.list_endpoints()["endpoints"][0]["billing"], "Cursor subscription")
        self.assertIn("overage", self.deck.list_endpoints()["endpoints"][0]["billing_note"])
        with mock.patch.object(mcp.codex_settings, "handle", return_value={"ok": True, "models": [{"id": "cursor/auto", "name": "Auto"}, {"id": "cursor/sonnet", "name": "Sonnet"}]}) as handle:
            result = self.deck.search_models("sonnet", "Cursor")
            self.assertEqual([row["id"] for row in result["models"]], ["cursor/sonnet"])
            self.assertEqual(handle.call_args.args[0]["action"], "cursor_models")
            self.assertEqual(handle.call_args.args[0]["account"], ACCOUNT)
        with mock.patch.object(mcp.codex_settings, "handle", return_value={"ok": True, "agent_name": "cursor_auto"}) as handle:
            result = self.deck.add_model("cursor/auto", endpoint="Cursor")
            self.assertEqual(result["billing"], "Cursor subscription")
            self.assertEqual(handle.call_args.args[0]["model"], "cursor/auto")
            self.assertEqual(handle.call_args.args[0]["base_url"], "https://api.cursor.com")
            self.assertEqual(handle.call_args.args[0]["account"], ACCOUNT)
        self.assertEqual(self.fetched, [])

    def test_cursor_status_no_install_or_secrets(self):
        with mock.patch.object(mcp.codex_settings, "handle", return_value={"ok": True, "installed": False, "version": None, "required_version": "1.2", "api_key": "hidden", "message": "unsafe hidden"}) as handle:
            result = self.deck.cursor_status()
            self.assertFalse(result["sdk"]["installed"])
            self.assertEqual(handle.call_args.args[0]["action"], "cursor_status")
            self.assertIn("explicitly install", result["setup"])
            self.assertNotIn("hidden", json.dumps(result))

    def test_benchmark_tools_annotations_pagination_and_schema_validation(self):
        self.deck.benchmarks = bm.BenchmarkStore(Path(self.temp.name) / "benchmarks.json", fetch=lambda url, headers: CATALOG)
        self.assertEqual(self.deck.call("benchmark_status", {})["catalog_models"], 0)
        self.assertEqual(self.deck.call("refresh_benchmarks", {})["catalog_models"], 3)
        self.assertEqual(self.deck.call("rank_models", {"task": "coding", "limit": 1})["models"][0]["model"], "lab/b")
        self.assertEqual(self.deck.call("model_benchmarks", {"model": "lab/a", "limit": 1})["next_offset"], 1)
        self.assertEqual(self.deck.call("compare_models", {"models": ["lab/a", "lab/b"], "task": "coding"})["comparison_count"], 1)
        for tool, args in [("rank_models", {"task": "coding", "limit": 51}),
                           ("rank_models", {"task": "coding", "offset": -1}),
                           ("rank_models", {"task": "coding", "source": "guess"}),
                           ("rank_models", {"task": "coding", "metric": "elo"}),
                           ("refresh_benchmarks", {"authenticated": "yes"}),
                           ("compare_models", {"models": "lab/a"}),
                           ("compare_models", {"models": []}),
                           ("compare_models", {"models": ["a"] * 11}),
                           ("compare_models", {"models": ["a", "a"]}),
                           ("compare_models", {"models": [1]}),
                           ("model_benchmarks", {"model": "x" * 301})]:
            with self.subTest(tool=tool, args=args), self.assertRaises(mcp.DeckError):
                self.deck.call(tool, args)
        self.assertTrue(mcp.TOOL_INDEX["model_benchmarks"]["annotations"]["readOnlyHint"])
        self.assertFalse(mcp.TOOL_INDEX["refresh_benchmarks"]["annotations"]["readOnlyHint"])

    def test_invalid_json_rpc_arguments_are_not_coerced(self):
        for arguments in (False, [], "", 0):
            result = mcp.handle_message(self.deck, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_endpoints", "arguments": arguments}})
            self.assertTrue(result["result"]["isError"])
        for message in ([1], {"method": "ping", "id": 1}, {"jsonrpc": "2.0", "method": "ping", "id": []}):
            self.assertEqual(mcp.handle_message(self.deck, message)["error"]["code"], -32600)
        self.assertEqual(mcp.handle_message(self.deck, {"jsonrpc": "2.0", "method": "ping", "id": 1, "params": []})["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()

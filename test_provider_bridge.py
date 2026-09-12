"""Provider-boundary checks without inference, credentials, or a live backend."""
import copy
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from provider_bridge import BackendError, BridgeError, MCP_INSTRUCTIONS, ProviderBridge, ROUTING_INSTRUCTIONS, mcp_server_arguments, routing_instructions, toml_inline


def registered_model(account="12345678-1234-1234-1234-123456789abc"):
    return {"provider": "openrouter-settings", "role": "openrouter_qwen",
            "config": {"model_providers": {"openrouter-settings": {
                "name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
                "wire_api": "responses", "supports_websockets": False,
                "auth": {"command": "/Applications/OpenRouterCredentialHelper",
                         "args": ["--token", account], "timeout_ms": 5000,
                         "refresh_interval_ms": 300000}}}}}


class FakeRegistry:
    def __init__(self):
        self.models = {"qwen/test": registered_model()}
        self.selection = {}

    def load_models(self):
        return copy.deepcopy(self.models)

    def catalog_entries(self, price_lines=None):
        return [{"id": model, "model": model, "displayName": model + " · OpenRouter",
                 "description": "Uses OpenRouter credits", "hidden": False,
                 "isDefault": False, "defaultReasoningEffort": "low",
                 "supportedReasoningEfforts": [{"reasoningEffort": "low", "description": "Low"}],
                 "inputModalities": ["text"], "supportsPersonality": False}
                for model in self.models]

    def selected(self):
        return dict(self.selection)

    def select(self, model, effort=None):
        self.selection = {"model": model, "effort": effort}
        return dict(self.selection)


class FakeRouter:
    base_url = "http://127.0.0.1:4242/backend-api/codex"

    def __init__(self):
        self.catalog_waits = []

    def codex_arguments(self):
        return ["-c", f'openai_base_url="{self.base_url}"', "-c", "features.enable_request_compression=false"]

    def wait_for_catalog(self, timeout):
        self.catalog_waits.append(timeout)
        return True


class FakeBridge(ProviderBridge):
    def __init__(self):
        self.messages = []
        super().__init__(FakeRegistry(), self.messages.append, router=FakeRouter())
        self.requests = []
        self.forwarded = []
        self.responses = {}

    async def request(self, method, params):
        self.requests.append((method, copy.deepcopy(params)))
        response = self.responses.get(method, {})
        if isinstance(response, Exception):
            raise response
        if method == "thread/start" and method not in self.responses:
            return {"thread": {"id": "new-thread"}, "modelProvider": params.get("modelProvider", "openai")}
        return copy.deepcopy(response)

    async def send_backend(self, message):
        self.forwarded.append(copy.deepcopy(message))


class ProviderBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupt_cancels_exact_cursor_turn_only_after_backend_success(self):
        cancel = Mock(side_effect=lambda *args: self.assertEqual(self.bridge.requests[-1][0], 'turn/interrupt'))
        self.bridge.router.cancel_cursor_turn = cancel
        params = {'threadId': 'thread-a', 'turnId': 'turn-b'}
        self.bridge.responses['turn/interrupt'] = {'accepted': True}
        self.assertEqual(await self.bridge.dispatch('turn/interrupt', params), {'accepted': True})
        cancel.assert_called_once_with('thread-a', 'turn-b')
        cancel.reset_mock()
        failure = BackendError({'code': 42, 'message': 'rejected'})
        self.bridge.responses['turn/interrupt'] = failure
        with self.assertRaises(BackendError) as caught:
            await self.bridge.dispatch('turn/interrupt', params)
        self.assertIs(caught.exception, failure)
        cancel.assert_not_called()

    async def test_interrupt_works_without_cursor_router_method(self):
        self.bridge.responses['turn/interrupt'] = {'accepted': True}
        self.assertEqual(await self.bridge.dispatch('turn/interrupt', {'threadId': 'a', 'turnId': 'b'}), {'accepted': True})

    async def test_thread_started_notification_preserves_payload_and_remembers_cwd(self):
        remember = Mock()
        self.bridge.router.remember_thread = remember
        notification = {'method': 'thread/started', 'params': {'thread': {'id': 'child', 'cwd': '/work/child'}, 'extra': 7}}
        stream = asyncio.StreamReader()
        stream.feed_data((json.dumps(notification) + '\n').encode())
        stream.feed_eof()
        self.bridge.process = SimpleNamespace(stdout=stream)
        await self.bridge.read_backend()
        self.assertEqual(self.bridge.messages, [notification])
        remember.assert_called_once_with('child', '/work/child')

    def test_remember_requires_nonempty_cwd_and_supports_result_cwd(self):
        remember = Mock()
        self.bridge.router.remember_thread = remember
        for cwd in (None, '', '   ', 17):
            self.bridge.remember({'thread': {'id': 'child'}, 'cwd': cwd})
        remember.assert_not_called()
        self.bridge.remember({'thread': {'id': 'child'}, 'cwd': '/work'})
        remember.assert_called_once_with('child', '/work')

    def test_cursor_billing_and_roster_keep_openai_transport(self):
        bridge = FakeBridge()
        bridge.registry.models["cursor/auto"] = {"endpoint": {"cursor": True, "has_key": True, "name": "Cursor"}}
        self.assertEqual(bridge.route("cursor/auto"), {"provider": "openai", "billing": "cursor"})
        instructions = routing_instructions(bridge.registry)
        self.assertIn("price unknown", instructions)
        self.assertIn("overages apply", instructions)
        for name in ("benchmark_status", "refresh_benchmarks", "model_benchmarks", "compare_models", "rank_models"):
            self.assertIn(name, instructions)

    async def test_thread_roster_includes_cached_price_capabilities_and_benchmark_evidence(self):
        self.bridge.router.price_table = lambda: {"qwen/test": {"input": 0, "output": 0.6, "context": 128000,
                                                               "tools": True, "modalities": ["text"]}}
        self.bridge.router.benchmark_lines = lambda: {"qwen/test": "Artificial Analysis coding=75 (higher better); cache stale"}
        await self.bridge.dispatch("thread/start", {"model": "gpt-6-astra", "developerInstructions": "Existing rules."})
        instructions = next(params["developerInstructions"] for method, params in self.bridge.requests if method == "thread/start")
        self.assertTrue(instructions.startswith("Existing rules."))
        for expected in ("role=openrouter_qwen", "OpenRouter credits", "in $0/M", "128k context", "tools=yes",
                         "coding=75", "cache stale", "spawn_agent description"):
            self.assertIn(expected, instructions)

    def setUp(self):
        self.bridge = FakeBridge()
        self.bridge.responses["config/read"] = {
            "config": {"model": "gpt-6-astra", "model_reasoning_effort": "ultra", "sandbox_mode": "read-only"},
            "layers": [{"name": {"type": "user", "file": "/Users/test/.codex/config.toml"}, "version": "v"}]}

    def test_backend_arguments_point_codex_at_the_router(self):
        argv = ["-c", "features.code_mode_host=true", "app-server", "--analytics-default-enabled"]
        arguments = self.bridge.backend_arguments(argv)
        self.assertEqual(arguments[:4], argv)
        self.assertIn('openai_base_url="http://127.0.0.1:4242/backend-api/codex"', arguments)
        self.assertIn("features.enable_request_compression=false", arguments)

    async def test_catalog_preserves_genuine_entries_and_appends_only_last_page(self):
        genuine = {"id": "gpt-6-astra", "model": "gpt-6-astra", "isDefault": True,
                   "supportsPersonality": True, "inputModalities": ["text", "image"]}
        self.bridge.responses["model/list"] = {"data": [genuine], "nextCursor": "page-2"}
        first = await self.bridge.dispatch("model/list", {})
        self.assertEqual(first["data"], [genuine])
        self.bridge.responses["model/list"]["nextCursor"] = None
        last = await self.bridge.dispatch("model/list", {"cursor": "page-2"})
        self.assertEqual(last["data"][0], genuine)
        self.assertEqual([entry["model"] for entry in last["data"]], ["gpt-6-astra", "qwen/test"])
        self.bridge.responses["model/list"] = last
        repeated = await self.bridge.dispatch("model/list", {})
        self.assertEqual(sum(entry["model"] == "qwen/test" for entry in repeated["data"]), 1)

    async def test_new_openrouter_thread_stays_on_openai_provider_for_the_router(self):
        params = {"model": "qwen/test", "config": {"sandbox_mode": "read-only"}}
        original = copy.deepcopy(params)
        result = await self.bridge.dispatch("thread/start", params)
        sent = self.bridge.requests[-1][1]
        self.assertNotIn("modelProvider", sent)
        self.assertNotIn("model_providers", sent["config"])
        self.assertEqual(sent["model"], "qwen/test")
        self.assertEqual(sent["config"]["sandbox_mode"], "read-only")
        self.assertIn(ROUTING_INSTRUCTIONS, sent["developerInstructions"])
        self.assertTrue(sent["developerInstructions"].endswith(MCP_INSTRUCTIONS))
        self.assertEqual(result["modelProvider"], "openai")
        self.assertEqual(self.bridge.thread_providers["new-thread"], "openai")
        self.assertEqual(self.bridge.router.catalog_waits, [8])
        self.assertEqual(params, original)

    async def test_bare_gpt_thread_is_forwarded_unchanged_apart_from_instructions(self):
        await self.bridge.dispatch("thread/start", {"model": "gpt-6-astra"})
        sent = self.bridge.requests[-1][1]
        self.assertNotIn("modelProvider", sent)
        self.assertNotIn("config", sent)
        self.assertEqual(sent["model"], "gpt-6-astra")

    async def test_unregistered_slash_model_is_refused_before_any_request(self):
        with self.assertRaises(BridgeError):
            await self.bridge.dispatch("thread/start", {"model": "deepseek/unregistered"})
        self.assertEqual([method for method, _ in self.bridge.requests], [])

    async def test_explicit_foreign_provider_is_refused(self):
        with self.assertRaises(BridgeError):
            await self.bridge.dispatch("thread/start", {"model": "qwen/test", "modelProvider": "ollama"})

    async def test_backend_choosing_another_provider_is_reported(self):
        self.bridge.responses["thread/start"] = {"thread": {"id": "t"}, "modelProvider": "azure"}
        with self.assertRaises(BridgeError):
            await self.bridge.dispatch("thread/start", {"model": "qwen/test"})

    async def test_legacy_route_identity_follows_key_account(self):
        original = self.bridge.legacy_route("qwen/test")["provider"]
        self.assertTrue(original.startswith("openrouter-bridge-"))
        self.assertEqual(self.bridge.legacy_route("qwen/test")["provider"], original)
        self.bridge.registry.models["qwen/other"] = registered_model("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        self.assertNotEqual(self.bridge.legacy_route("qwen/other")["provider"], original)
        self.assertIsNone(self.bridge.legacy_route("gpt-6-astra"))

    async def test_picker_write_is_virtual_and_config_read_overlays(self):
        result = await self.bridge.dispatch("config/batchWrite", {
            "edits": [{"keyPath": "model", "value": "qwen/test", "mergeStrategy": "replace"},
                      {"keyPath": "model_reasoning_effort", "value": "medium", "mergeStrategy": "replace"}],
            "expectedVersion": "v"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.bridge.registry.selected(), {"model": "qwen/test", "effort": "medium"})
        self.assertEqual([method for method, _ in self.bridge.requests], ["config/read"])
        overlaid = await self.bridge.dispatch("config/read", {"includeLayers": True})
        self.assertEqual(overlaid["config"]["model"], "qwen/test")
        self.assertEqual(overlaid["config"]["model_reasoning_effort"], "medium")
        self.assertEqual(overlaid["config"]["sandbox_mode"], "read-only")
        self.assertEqual(overlaid["layers"][0]["version"], "v")

    async def test_router_backed_task_can_switch_between_openai_and_openrouter_models(self):
        self.bridge.thread_providers["existing"] = "openai"
        for method, selection in (("thread/settings/update", {"model": "qwen/test"}),
                                  ("turn/start", {"model": "qwen/test"}),
                                  ("turn/start", {"collaborationMode": {"mode": "default", "settings": {"model": "qwen/test"}}}),
                                  ("turn/start", {"model": "gpt-5.6-sol"})):
            with self.subTest(method=method, selection=selection):
                params = {"threadId": "existing", **selection}
                await self.bridge.dispatch(method, params)
                self.assertEqual(self.bridge.requests[-1], (method, params))

    async def test_legacy_task_rejects_cross_route_changes_before_request(self):
        self.bridge.thread_providers["existing"] = self.bridge.legacy_route("qwen/test")["provider"]
        with self.assertRaises(BridgeError):
            await self.bridge.dispatch("turn/start", {"threadId": "existing", "model": "gpt-5.6-sol"})
        self.assertEqual(self.bridge.requests, [])
        await self.bridge.dispatch("turn/start", {"threadId": "existing", "model": "qwen/test"})
        self.assertEqual(self.bridge.requests[-1][1]["effort"], "low")

    async def test_resume_reconstructs_legacy_saved_provider(self):
        provider = self.bridge.legacy_route("qwen/test")["provider"]
        self.bridge.thread_providers["existing"] = provider
        await self.bridge.dispatch("thread/resume", {"threadId": "existing"})
        sent = self.bridge.requests[-1][1]
        self.assertEqual(sent["modelProvider"], provider)
        self.assertIn(provider, sent["config"]["model_providers"])

    async def test_resume_of_router_backed_task_is_untouched(self):
        self.bridge.thread_providers["existing"] = "openai"
        await self.bridge.dispatch("thread/resume", {"threadId": "existing"})
        self.assertEqual(self.bridge.requests[-1], ("thread/resume", {"threadId": "existing"}))

    async def test_resume_refuses_missing_legacy_credential_route(self):
        self.bridge.thread_providers["existing"] = self.bridge.legacy_route("qwen/test")["provider"]
        self.bridge.registry.models["qwen/test"] = registered_model("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        with self.assertRaises(BridgeError):
            await self.bridge.dispatch("thread/resume", {"threadId": "existing"})
        self.assertEqual(self.bridge.requests, [])

    async def test_backend_error_keeps_original_client_id(self):
        error = {"code": -32602, "message": "bad argument", "data": {"field": "x"}}
        self.bridge.responses["example/read"] = BackendError(error)
        await self.bridge.handle_client({"id": 41, "method": "example/read", "params": {}})
        self.assertEqual(self.bridge.messages, [{"id": 41, "error": error}])

    async def test_server_tool_responses_and_client_notifications_unchanged(self):
        messages = [{"id": "server-tool-1", "result": {"approved": True}},
                    {"id": "server-tool-2", "error": {"code": -1, "message": "denied"}},
                    {"method": "initialized", "params": {}}]
        for message in messages:
            await self.bridge.handle_client(message)
        self.assertEqual(self.bridge.forwarded, messages)
        self.assertEqual(self.bridge.requests, [])


if __name__ == "__main__":
    unittest.main()

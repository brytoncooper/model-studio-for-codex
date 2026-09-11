"""Provider-boundary checks without inference, credentials, or a live backend."""
import copy
import unittest

from provider_bridge import BackendError, BridgeError, ProviderBridge


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

    def catalog_entries(self):
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


class FakeBridge(ProviderBridge):
    def __init__(self):
        self.messages = []
        super().__init__(FakeRegistry(), self.messages.append)
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
    def setUp(self):
        self.bridge = FakeBridge()
        self.bridge.responses["config/read"] = {
            "config": {"model": "gpt-6-astra", "model_reasoning_effort": "ultra", "sandbox_mode": "read-only"},
            "layers": [{"name": {"type": "user", "file": "/Users/test/.codex/config.toml"}, "version": "v"}]}

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

    async def test_new_openrouter_thread_injects_key_specific_provider(self):
        params = {"model": "qwen/test", "modelProvider": "openai", "config": {"sandbox_mode": "read-only"}}
        original = copy.deepcopy(params)
        result = await self.bridge.dispatch("thread/start", params)
        sent = self.bridge.requests[-1][1]
        provider = self.bridge.route("qwen/test")["provider"]
        self.assertTrue(provider.startswith("openrouter-bridge-"))
        self.assertEqual(sent["modelProvider"], provider)
        self.assertEqual(sent["config"]["model_providers"][provider], registered_model()["config"]["model_providers"]["openrouter-settings"])
        self.assertEqual(sent["config"]["model_reasoning_effort"], "low")
        self.assertEqual(sent["config"]["sandbox_mode"], "read-only")
        self.assertEqual(result["modelProvider"], provider)
        self.assertEqual(params, original)

    async def test_bare_gpt_uses_builtin_openai(self):
        await self.bridge.dispatch("thread/start", {"model": "gpt-6-astra"})
        sent = self.bridge.requests[-1][1]
        self.assertEqual(sent["modelProvider"], "openai")
        self.assertNotIn("model_providers", sent["config"])

    async def test_account_identity_determines_provider(self):
        original = self.bridge.route("qwen/test")["provider"]
        self.assertEqual(self.bridge.route("qwen/test")["provider"], original)
        self.bridge.registry.models["qwen/other"] = registered_model("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        self.assertNotEqual(self.bridge.route("qwen/other")["provider"], original)

    async def test_picker_write_is_virtual_and_config_read_overlays(self):
        result = await self.bridge.dispatch("config/batchWrite", {
            "edits": [{"keyPath": "model", "value": "qwen/test", "mergeStrategy": "replace"},
                      {"keyPath": "model_reasoning_effort", "value": "low", "mergeStrategy": "replace"}],
            "expectedVersion": "v"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.bridge.registry.selected(), {"model": "qwen/test", "effort": "low"})
        self.assertEqual([method for method, _ in self.bridge.requests], ["config/read"])
        overlaid = await self.bridge.dispatch("config/read", {"includeLayers": True})
        self.assertEqual(overlaid["config"]["model"], "qwen/test")
        self.assertEqual(overlaid["config"]["model_reasoning_effort"], "low")
        self.assertEqual(overlaid["config"]["sandbox_mode"], "read-only")
        self.assertEqual(overlaid["layers"][0]["version"], "v")

    async def test_cross_provider_existing_changes_rejected_before_request(self):
        self.bridge.thread_providers["existing"] = "openai"
        for method, selection in (("thread/settings/update", {"model": "qwen/test"}),
                                  ("turn/start", {"model": "qwen/test"}),
                                  ("turn/start", {"collaborationMode": {"mode": "default", "settings": {"model": "qwen/test"}}})):
            with self.subTest(method=method, selection=selection):
                with self.assertRaises(BridgeError):
                    await self.bridge.dispatch(method, {"threadId": "existing", **selection})
                self.assertEqual(self.bridge.requests, [])

    async def test_same_provider_existing_change_forwarded(self):
        self.bridge.thread_providers["existing"] = "openai"
        params = {"threadId": "existing", "model": "gpt-5.6-sol"}
        await self.bridge.dispatch("turn/start", params)
        self.assertEqual(self.bridge.requests, [("turn/start", params)])

    async def test_resume_reconstructs_saved_provider(self):
        provider = self.bridge.route("qwen/test")["provider"]
        self.bridge.thread_providers["existing"] = provider
        await self.bridge.dispatch("thread/resume", {"threadId": "existing"})
        sent = self.bridge.requests[-1][1]
        self.assertEqual(sent["modelProvider"], provider)
        self.assertIn(provider, sent["config"]["model_providers"])

    async def test_resume_refuses_missing_saved_credential_route(self):
        self.bridge.thread_providers["existing"] = self.bridge.route("qwen/test")["provider"]
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

"""Endpoints: registration against OpenRouter, another API, or a keyless local server, and the chat wire."""
import json
from pathlib import Path
import tempfile
import unittest

import codex_settings as settings
from chat_wire import ChatStreamTranslator, chat_request_from_responses
from routing_registry import AGENT_MARKER, RegistryError, RoutingRegistry, validate_base_url


LOCAL_AGENT = '''name = "openrouter_local_coder_deadbeef"
model = "local-coder"
model_provider = "openrouter-settings"
[model_providers.openrouter-settings]
name = "LM Studio"
base_url = "http://localhost:1234/v1"
wire_api = "responses"
supports_websockets = false
'''


class RegistryEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "agents").mkdir()
        self.registry = RoutingRegistry(root / "agents", selection_path=root / "s.json", display_names_path=root / "d.json")
        self.registry.endpoints_path = root / "endpoints.json"

    def write(self, content, name="openrouter_local_coder_deadbeef"):
        (self.registry.agents_dir / f"{name}.toml").write_text(AGENT_MARKER + "\n" + content)

    def test_keyless_local_endpoint_is_accepted_and_summarized(self):
        self.write(LOCAL_AGENT)
        self.registry.endpoints_path.write_text(json.dumps({"http://localhost:1234/v1": {"name": "LM Studio", "wire": "chat"}}))
        entry = self.registry.load_models()["local-coder"]
        self.assertEqual(entry["endpoint"], {"name": "LM Studio", "base_url": "http://localhost:1234/v1", "has_key": False,
                                             "account": None, "openrouter": False, "wire": "chat"})
        self.assertIn("billing is managed separately", self.registry.catalog_entries()[0]["description"])

    def test_wire_defaults_to_auto_and_bad_endpoint_settings_are_ignored(self):
        self.write(LOCAL_AGENT)
        self.registry.endpoints_path.write_text("garbage")
        self.assertEqual(self.registry.load_models()["local-coder"]["endpoint"]["wire"], "auto")

    def test_openrouter_requires_key_and_slash_ids(self):
        self.write(LOCAL_AGENT.replace("http://localhost:1234/v1", "https://openrouter.ai/api/v1"))
        with self.assertRaises(RegistryError):
            self.registry.load_models()

    def test_plain_http_is_local_only_and_reserved_names_are_refused(self):
        self.write(LOCAL_AGENT.replace("http://localhost:1234/v1", "http://example.com/v1"))
        with self.assertRaises(RegistryError):
            self.registry.load_models()
        self.write(LOCAL_AGENT.replace('model = "local-coder"', 'model = "gpt-5.5"'))
        with self.assertRaises(RegistryError):
            self.registry.load_models()
        for url in ("https://api.deepseek.com/v1", "http://192.168.1.20:11434/v1", "http://mac-mini.local:1234/v1"):
            validate_base_url(url)
        for url in ("ftp://x/v1", "https://openrouter.ai/api/v1?x=1", "not a url"):
            with self.assertRaises(RegistryError):
                validate_base_url(url)


class RegistrationEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "agents").mkdir()

    def register(self, **fields):
        request = {"action": "register_agent", "executable": "/Applications/Model Deck.app/Contents/MacOS/ModelDeck",
                   "config_path": str(self.root / "config.toml"), "state_dir": str(self.root / "state"),
                   "agents_dir": str(self.root / "agents"), **fields}
        try:
            return settings.handle(request)
        except settings.SettingsError as error:  # main() turns these into {"ok": false}
            return {"ok": False, "error": str(error)}

    def agent_file(self, result):
        return (self.root / "agents" / (result["agent_name"] + ".toml")).read_text()

    def test_local_endpoint_without_key(self):
        result = self.register(model="qwen3-8b", base_url="http://localhost:1234/v1/", endpoint_name="LM Studio")
        self.assertTrue(result["ok"], result)
        text = self.agent_file(result)
        self.assertIn('base_url = "http://localhost:1234/v1"', text)
        self.assertIn('name = "LM Studio"', text)
        self.assertNotIn("auth", text)
        self.assertIn("billing is managed separately", text)

    def test_cursor_registration_preserves_globals_and_recovers_sdk_wire(self):
        original = 'model = "gpt-6-astra"\nmodel_provider = "openai"\n'
        (self.root / "config.toml").write_text(original)
        result = self.register(model="cursor/composer-2.5", base_url="https://api.cursor.com", wire="cursor",
                               account="12345678-1234-1234-1234-123456789abc")
        self.assertTrue(result["ok"], result)
        self.assertEqual((self.root / "config.toml").read_text(), original)
        registry = RoutingRegistry(self.root / "agents")
        registry.endpoints_path = self.root / "absent.json"
        endpoint = registry.load_models()["cursor/composer-2.5"]["endpoint"]
        self.assertEqual(endpoint["wire"], "cursor")
        self.assertTrue(endpoint["cursor"])
        self.assertTrue(endpoint["has_key"])
        self.assertEqual(endpoint["name"], "Cursor")
        self.assertIn("overages apply", self.agent_file(result))

    def test_cursor_registration_rejects_wrong_endpoint_wire_or_missing_key(self):
        valid = dict(model="cursor/auto", base_url="https://api.cursor.com", wire="cursor",
                     account="12345678-1234-1234-1234-123456789abc")
        for change in ({"base_url": "https://openrouter.ai/api/v1"}, {"base_url": "https://api.cursor.com/v1"},
                       {"wire": "responses"}, {"wire": "chat"}, {"account": None}, {"model": "auto"}):
            with self.subTest(change=change):
                self.assertFalse(self.register(**dict(valid, **change))["ok"])

    def test_other_provider_with_key(self):
        account = "12345678-1234-1234-1234-123456789abc"
        result = self.register(model="deepseek-chat", account=account, base_url="https://api.deepseek.com/v1", endpoint_name="DeepSeek")
        self.assertTrue(result["ok"], result)
        text = self.agent_file(result)
        self.assertIn('args = ["--token", "%s"]' % account, text)
        self.assertIn("through DeepSeek", text)

    def test_openrouter_still_needs_slash_and_key(self):
        self.assertFalse(self.register(model="deepseek-chat", account="12345678-1234-1234-1234-123456789abc")["ok"])
        self.assertFalse(self.register(model="deepseek/deepseek-chat")["ok"])
        self.assertFalse(self.register(model="gpt-oss-20b", base_url="http://localhost:1234/v1")["ok"])
        self.assertFalse(self.register(model="a", base_url="http://example.com/v1")["ok"])


class ChatWireTests(unittest.TestCase):
    def test_request_conversion_merges_tool_calls_into_the_assistant_turn(self):
        request = {"model": "m", "instructions": "sys", "tools": [{"type": "function", "name": "f", "description": "d", "parameters": {"type": "object"}, "strict": False}],
                   "input": [
                       {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "rules"}]},
                       {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}, {"type": "input_image", "image_url": "data:x"}]},
                       {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "on it"}]},
                       {"type": "function_call", "name": "f", "arguments": "{}", "call_id": "c1"},
                       {"type": "function_call", "name": "f", "arguments": "{\"a\":1}", "call_id": "c2"},
                       {"type": "function_call_output", "call_id": "c1", "output": "r1"},
                       {"type": "function_call_output", "call_id": "c2", "output": [{"type": "input_text", "text": "r2"}]}]}
        payload = chat_request_from_responses(request)
        roles = [message["role"] for message in payload["messages"]]
        self.assertEqual(roles, ["system", "system", "user", "assistant", "tool", "tool"])
        self.assertEqual(payload["messages"][2]["content"], [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": "data:x"}}])
        self.assertEqual(payload["messages"][3]["content"], "on it")
        self.assertEqual([call["id"] for call in payload["messages"][3]["tool_calls"]], ["c1", "c2"])
        self.assertEqual(payload["messages"][5], {"role": "tool", "tool_call_id": "c2", "content": "r2"})
        self.assertEqual(payload["tools"][0], {"type": "function", "function": {"name": "f", "description": "d", "parameters": {"type": "object"}, "strict": False}})
        self.assertEqual(payload["stream_options"], {"include_usage": True})

    def test_stream_translation_without_tool_calls_or_usage(self):
        translator = ChatStreamTranslator()
        events = translator.feed({"choices": [{"delta": {"content": "Hi"}}]})
        self.assertEqual([event["type"] for event in events], ["response.created", "response.output_item.added", "response.output_text.delta"])
        self.assertEqual(translator.feed({"choices": [{"delta": {}, "finish_reason": "stop"}]}), [])
        events = translator.finish()
        self.assertEqual([event["type"] for event in events], ["response.output_text.done", "response.output_item.done", "response.completed"])
        self.assertNotIn("usage", events[-1]["response"])  # the provider reported none; nothing is invented
        self.assertNotIn("_", events[1]["item"]["id"])

    def test_empty_stream_completes_only_after_a_terminal_outcome(self):
        translator = ChatStreamTranslator()
        events = translator.feed({"choices": [{"delta": {}, "finish_reason": "stop"}]}) + translator.finish()
        self.assertEqual([event["type"] for event in events], ["response.created", "response.completed"])
        # A stream that ends without a finish reason was truncated: Codex must not treat it as complete.
        self.assertEqual([event["type"] for event in ChatStreamTranslator().finish()], ["response.created", "response.failed"])


if __name__ == "__main__":
    unittest.main()

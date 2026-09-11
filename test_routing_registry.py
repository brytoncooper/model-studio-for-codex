import json
from pathlib import Path
import stat
import tempfile
import unittest

from routing_registry import AGENT_MARKER, RegistryError, RoutingRegistry


AGENT = '''name = "openrouter_test"
model = "qwen/test"
model_provider = "openrouter-settings"
[model_providers.openrouter-settings]
name = "OpenRouter"
base_url = "https://openrouter.ai/api/v1"
wire_api = "responses"
supports_websockets = false
[model_providers.openrouter-settings.auth]
command = "/Applications/OpenRouterCredentialHelper"
args = ["--token", "12345678-1234-1234-1234-123456789abc"]
timeout_ms = 5000
refresh_interval_ms = 300000
'''


class RoutingRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.agents = self.root / "agents"
        self.agents.mkdir()
        self.selection = self.root / "support/picker-selection.json"
        self.registry = RoutingRegistry(self.agents, selection_path=self.selection)

    def write_agent(self, content=AGENT):
        path = self.agents / "openrouter_test.toml"
        path.write_text(AGENT_MARKER + "\n" + content)
        return path

    def test_catalog_and_routing_only_managed_models(self):
        self.write_agent()
        (self.agents / "openrouter_user.toml").write_text("user owned invalid TOML")
        models = self.registry.load_models()
        self.assertEqual(list(models), ["qwen/test"])
        self.assertEqual(models["qwen/test"]["role"], "openrouter_test")
        self.assertEqual(models["qwen/test"]["provider"], "openrouter-settings")
        entry = self.registry.catalog_entries()[0]
        self.assertEqual(entry["id"], entry["model"])
        self.assertEqual(entry["inputModalities"], ["text"])
        self.assertFalse(entry["supportsPersonality"])

    def test_invalid_managed_agent_fails_loudly(self):
        for content in ("invalid = [", AGENT.replace("qwen/test", "openai/gpt-test"),
                        AGENT.replace('model_provider = "openrouter-settings"', 'model_provider = "openai"')):
            with self.subTest(content=content):
                self.write_agent(content)
                with self.assertRaises(RegistryError):
                    self.registry.load_models()

    def test_secret_or_environment_auth_refused(self):
        for field in ('experimental_bearer_token = "not-a-real-key"', 'env_key = "SOME_KEY"'):
            self.write_agent(AGENT.replace('name = "OpenRouter"', 'name = "OpenRouter"\n' + field))
            with self.assertRaises(RegistryError):
                self.registry.load_models()

    def test_selection_atomic_private_and_idempotent(self):
        unrelated = self.root / "unrelated"
        unrelated.write_text("preserve")
        self.assertEqual(self.registry.selected(), {})
        self.registry.select("gpt-6-astra", "ultra")
        first = self.selection.read_bytes()
        self.registry.select("gpt-6-astra", "ultra")
        self.assertEqual(self.selection.read_bytes(), first)
        self.assertEqual(stat.S_IMODE(self.selection.stat().st_mode), 0o600)
        self.assertEqual(unrelated.read_text(), "preserve")
        self.assertEqual(self.registry.selected(), {"model": "gpt-6-astra", "effort": "ultra"})
        self.assertEqual(list(self.selection.parent.glob(".picker-selection-*")), [])

    def test_unknown_or_malformed_selection_not_overwritten(self):
        self.selection.parent.mkdir()
        for content in ('{', json.dumps({"model": "x", "effort": None, "extra": True})):
            self.selection.write_text(content)
            with self.assertRaises(RegistryError):
                self.registry.select("qwen/test", "low")
            self.assertEqual(self.selection.read_text(), content)

    def test_selection_validation(self):
        for model, effort in (("", None), ("has space", "low"), ("x", "default"), ("x", [])):
            with self.subTest(model=model, effort=effort):
                with self.assertRaises(RegistryError):
                    self.registry.select(model, effort)

    def test_symlink_reads_and_writes_refused(self):
        real_agent = self.root / "real.toml"
        real_agent.write_text(AGENT_MARKER + "\n" + AGENT)
        (self.agents / "openrouter_test.toml").symlink_to(real_agent)
        with self.assertRaises(RegistryError):
            self.registry.load_models()
        self.selection.parent.mkdir()
        self.selection.symlink_to(self.root / "missing")
        with self.assertRaises(RegistryError):
            self.registry.select("x", None)


if __name__ == "__main__":
    unittest.main()

"""Picker display names for OpenRouter models: automatic aliases plus optional custom names."""
import json
from pathlib import Path
import tempfile
import unittest

from routing_registry import RoutingRegistry, friendly_model_name, support_directory


class SupportDirectoryTests(unittest.TestCase):
    def test_legacy_folder_is_renamed_once_and_new_folder_wins_afterwards(self):
        with tempfile.TemporaryDirectory() as home:
            support = Path(home) / "Library/Application Support"
            (support / "Codex OpenRouter").mkdir(parents=True)
            (support / "Codex OpenRouter" / "preferences.json").write_text("{}")
            self.assertEqual(support_directory(home), support / "Model Deck")
            self.assertTrue((support / "Model Deck" / "preferences.json").exists())
            self.assertFalse((support / "Codex OpenRouter").exists())
            # A router from before the rename may recreate the old folder with log lines: fold them in.
            (support / "Codex OpenRouter").mkdir()
            (support / "Codex OpenRouter" / "router.log").write_text("late line\n")
            (support / "Model Deck" / "router.log").write_text("early line\n")
            (support / "Codex OpenRouter" / "unrelated.txt").write_text("keep")
            self.assertEqual(support_directory(home), support / "Model Deck")
            self.assertEqual((support / "Model Deck" / "router.log").read_text(), "early line\nlate line\n")
            self.assertFalse((support / "Codex OpenRouter" / "router.log").exists())
            self.assertTrue((support / "Codex OpenRouter" / "unrelated.txt").exists())  # never deleted
            (support / "Codex OpenRouter" / "unrelated.txt").unlink()
            support_directory(home)
            self.assertFalse((support / "Codex OpenRouter").exists())
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(support_directory(home), Path(home) / "Library/Application Support" / "Model Deck")


class FriendlyNameTests(unittest.TestCase):
    def test_common_openrouter_ids_get_short_readable_names(self):
        expected = {
            "deepseek/deepseek-v4.1-flash": "DeepSeek V4.1 Flash",
            "moonshotai/kimi-k3": "Kimi K3",
            "qwen/qwen3.8-27b": "Qwen 3.8 27B",
            "qwen/qwen3.8-max-0902": "Qwen 3.8 Max 0902",
            "x-ai/grok-4.6": "Grok 4.6",
            "~x-ai/grok-latest": "Grok Latest",
            "z-ai/glm-5.3-flash": "GLM 5.3 Flash",
            "anthropic/claude-sonnet-4.5": "Claude Sonnet 4.5",
            "meta-llama/llama-3.3-70b-instruct": "Llama 3.3 70B Instruct",
            "google/gemini-2.5-pro": "Gemini 2.5 Pro",
            "deepseek/deepseek-r1:free": "DeepSeek R1 (Free)",
        }
        for model, name in expected.items():
            with self.subTest(model=model):
                self.assertEqual(friendly_model_name(model), name)

    def test_names_are_bounded_and_never_empty(self):
        self.assertEqual(friendly_model_name("vendor/"), "vendor/")
        self.assertLessEqual(len(friendly_model_name("vendor/" + "-".join(["word"] * 40))), 48)


class CustomNameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        (base / "agents").mkdir()
        self.registry = RoutingRegistry(agents_dir=base / "agents", preferences_path=base / "p.json",
                                        selection_path=base / "s.json", display_names_path=base / "display-names.json")

    def tearDown(self):
        self.temp.cleanup()

    def test_custom_names_override_automatic_ones_and_bad_entries_are_ignored(self):
        self.registry.display_names_path.write_text(json.dumps({
            "deepseek/deepseek-v4.1-flash": "  Flash  ",
            "qwen/qwen3.8-27b": "",
            "bad model id": "x",
            "x-ai/grok-4.6": "a" * 49,
            "z-ai/glm-5.3": "tab\tname"}))
        self.assertEqual(self.registry.load_display_names(), {"deepseek/deepseek-v4.1-flash": "Flash"})
        self.assertEqual(self.registry.display_name_for("deepseek/deepseek-v4.1-flash"), "Flash")
        self.assertEqual(self.registry.display_name_for("qwen/qwen3.8-27b"), "Qwen 3.8 27B")

    def test_missing_or_broken_file_falls_back_to_automatic_names(self):
        self.assertEqual(self.registry.display_name_for("x-ai/grok-4.6"), "Grok 4.6")
        self.registry.display_names_path.write_text("not json")
        self.assertEqual(self.registry.load_display_names(), {})
        self.registry.display_names_path.write_text("[1, 2]")
        self.assertEqual(self.registry.load_display_names(), {})


if __name__ == "__main__":
    unittest.main()

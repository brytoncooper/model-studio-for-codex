import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tomlkit

import codex_settings as settings


class SettingsTests(unittest.TestCase):
    def test_cursor_sdk_status_and_install_are_explicit_and_preserve_config(self):
        self.config.write_text('model = "gpt-6-astra"\n')
        original = self.config.read_bytes()
        with patch("cursor_sdk_runtime.status", return_value={"ok": True, "installed": False}) as status, \
                patch("cursor_sdk_runtime.install", return_value={"ok": True, "installed": True}) as install:
            self.assertFalse(self.call("cursor_status")["installed"])
            install.assert_not_called()
            self.assertTrue(self.call("install_cursor_sdk")["installed"])
            status.assert_called_once()
            install.assert_called_once()
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(self.state.exists())

    def test_cursor_catalog_uses_saved_helper_and_sanitizes_errors(self):
        from subprocess import CompletedProcess
        fields = {"account": "bd3f9c00-03df-4dab-af64-3231575887a0", "executable": "/Applications/Helper"}
        with patch.object(settings.subprocess, "run", return_value=CompletedProcess([], 0, "secret-token\n")) as run, \
                patch("cursor_sdk_runtime.list_models", return_value={"ok": True, "models": [{"id": "cursor/auto", "name": "Auto"}]}) as models:
            result = self.call("cursor_models", **fields)
            models.assert_called_once_with("secret-token")
            self.assertEqual(run.call_args.args[0], [fields["executable"], "--token", fields["account"]])
            self.assertNotIn("secret-token", json.dumps(result))
            models.side_effect = RuntimeError("secret-token")
            with self.assertRaisesRegex(settings.SettingsError, "Cursor SDK operation failed") as caught:
                self.call("cursor_models", **fields)
            self.assertNotIn("secret-token", str(caught.exception))
        with patch.object(settings.subprocess, "run") as run:
            with self.assertRaises(settings.SettingsError):
                self.call("cursor_models", account="bad", executable="relative")
            run.assert_not_called()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "codex" / "config.toml"
        self.config.parent.mkdir()
        self.state = self.root / "state"
        self.agents = self.root / "agents"
        self.base = {"config_path": str(self.config), "state_dir": str(self.state),
                     "agents_dir": str(self.agents)}

    def call(self, action, **values):
        return settings.handle(dict(self.base, action=action, **values))

    def apply(self, **values):
        fields = {"model": "openai/example", "account": "bd3f9c00-03df-4dab-af64-3231575887a0",
                  "executable": "/Applications/OpenRouter Settings.app/Contents/MacOS/OpenRouter Settings"}
        fields.update(values)
        return self.call("apply", **fields)

    def test_model_inventory_is_read_only_and_sanitized(self):
        self.config.write_text("invalid config is irrelevant to inventory")
        self.agents.mkdir()
        agent = {"name": "openrouter_fixture", "model": "qwen/fixture",
                 "model_provider": "openrouter-settings", "model_providers": {
                     "openrouter-settings": settings.provider_table(
                         "bd3f9c00-03df-4dab-af64-3231575887a0", "/Applications/CredentialHelper")}}
        source = settings.AGENT_MARKER + "\n" + tomlkit.dumps(agent)
        path = self.agents / "openrouter_fixture.toml"
        path.write_text(source)
        result = self.call("list_models")
        self.assertEqual(result, {"ok": True, "models": [{"model": "qwen/fixture", "role": "openrouter_fixture"}]})
        self.assertFalse(self.state.exists())
        self.assertEqual(path.read_text(), source)
        self.assertEqual(self.config.read_text(), "invalid config is irrelevant to inventory")
        self.assertNotIn("bd3f9c00", json.dumps(result))

    def test_model_inventory_invalid_managed_file_fails_safely(self):
        self.agents.mkdir()
        (self.agents / "openrouter_bad.toml").write_text(settings.AGENT_MARKER + "\ninvalid = [")
        with self.assertRaisesRegex(settings.SettingsError, "Could not read registered models"):
            self.call("list_models")
        self.assertFalse(self.state.exists())

    def test_model_inventory_missing_folder_does_not_create_it(self):
        self.assertEqual(self.call("list_models"), {"ok": True, "models": []})
        self.assertFalse(self.state.exists())
        self.assertFalse(self.agents.exists())

    def test_switch_restore_preserves_unrelated_edits_and_original_backup(self):
        original = (b'# personal settings\nmodel = "previous" # keep model comment\n'
                    b'service_tier = "priority"\nmodel_reasoning_effort = "high"\n'
                    b'\n[projects."/work"]\ntrust_level = "trusted" # project comment\n'
                    b'\n[model_providers.company]\nname = "Company"\n')
        self.config.write_bytes(original)
        self.apply(effort="low")
        self.assertEqual((self.state / "original-config.toml").read_bytes(), original)
        doc = tomlkit.parse(self.config.read_text())
        self.assertNotIn("service_tier", doc)
        self.assertEqual(doc["model_providers"][settings.PROVIDER]["auth"]["args"][0], "--token")
        self.apply(model="other/model")
        self.assertEqual((self.state / "original-config.toml").read_bytes(), original)
        self.assertNotIn("model_reasoning_effort", tomlkit.parse(self.config.read_text()))
        with self.config.open("a") as stream:
            stream.write('\n[features]\nmulti_agent = true # later edit\n')
        self.call("restore")
        restored = tomlkit.parse(self.config.read_text())
        self.assertEqual(restored["model"], "previous")
        self.assertEqual(restored["model_reasoning_effort"], "high")
        self.assertEqual(restored["service_tier"], "priority")
        self.assertNotIn("model_provider", restored)
        self.assertNotIn(settings.PROVIDER, restored["model_providers"])
        self.assertTrue(restored["features"]["multi_agent"])
        self.assertIn("# project comment", self.config.read_text())
        self.assertIn("# later edit", self.config.read_text())
        self.assertIn("# personal settings", self.config.read_text())
        self.assertEqual(restored["model_providers"]["company"]["name"], "Company")
        self.assertFalse(self.call("status")["can_restore"])

    def test_absent_configuration_round_trip(self):
        self.assertFalse(self.call("status")["can_restore"])
        self.apply()
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.state / "original-config.toml").stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.call("status")["can_restore"])
        self.call("restore")
        self.assertFalse(self.config.exists())

    def test_reserved_provider_collision_is_unchanged(self):
        original = b'[model_providers.openrouter-settings]\nname = "Existing"\n'
        self.config.write_bytes(original)
        with self.assertRaisesRegex(settings.SettingsError, "reserved"):
            self.apply()
        self.assertEqual(self.config.read_bytes(), original)

    def test_external_touched_edits_block_apply_and_restore(self):
        self.apply()
        doc = tomlkit.parse(self.config.read_text())
        doc["model"] = "user-chosen"
        self.config.write_text(tomlkit.dumps(doc))
        for action in (self.apply, lambda: self.call("restore")):
            with self.assertRaisesRegex(settings.SettingsError, "outside"):
                action()
        self.assertEqual(tomlkit.parse(self.config.read_text())["model"], "user-chosen")

    def test_malformed_and_symlink_config_refused(self):
        self.config.write_bytes(b"not a TOML config [")
        with self.assertRaisesRegex(settings.SettingsError, "valid"):
            self.apply()
        self.config.unlink()
        target = self.root / "target"
        target.write_text("")
        self.config.symlink_to(target)
        with self.assertRaisesRegex(settings.SettingsError, "Symbolic"):
            self.apply()
        self.assertEqual(target.read_text(), "")

    def test_invalid_inputs_do_not_mutate_config(self):
        for values in ({"account": "bad"}, {"executable": "relative"}, {"model": "\n"}, {"effort": "nonsense"}):
            with self.assertRaises(settings.SettingsError):
                self.apply(**values)
        self.assertFalse(self.config.exists())

    def test_state_write_failure_rolls_back_configuration(self):
        original = b'model = "original"\n'
        self.config.write_bytes(original)
        real_write = settings.atomic_write

        def fail_state(path, data):
            if path.name == "state.json":
                raise OSError("simulated disk failure")
            real_write(path, data)

        with patch.object(settings, "atomic_write", side_effect=fail_state):
            with self.assertRaisesRegex(settings.SettingsError, "Previous settings were restored"):
                self.apply()
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse((self.state / "state.json").exists())

    def test_state_record_does_not_include_secret(self):
        self.apply()
        state = json.loads((self.state / "state.json").read_text())
        self.assertNotIn("api_key", state)
        self.assertEqual(state["last"]["provider_table"]["auth"]["timeout_ms"], 5000)

    def register(self, **values):
        fields = {"model": "anthropic/example", "account": "bd3f9c00-03df-4dab-af64-3231575887a0",
                  "executable": "/Applications/OpenRouter Settings.app/Contents/MacOS/OpenRouter Settings"}
        fields.update(values)
        return self.call("register_agent", **fields)

    def test_register_agent_preserves_global_bytes_and_defines_native_provider(self):
        original = b'# retained default\nmodel = "gpt-6-astra"\nservice_tier = "priority"\n'
        self.config.write_bytes(original)
        result = self.register()
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(result["provider"], "openai")
        self.assertEqual(result["model"], "gpt-6-astra")
        self.assertFalse(result["can_restore"])
        self.assertFalse((self.state / "state.json").exists())
        path = Path(result["agent_path"])
        self.assertEqual(path.parent, self.agents)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertLessEqual(len(result["agent_name"]), 70)
        text = path.read_text()
        self.assertTrue(text.startswith(settings.AGENT_MARKER + "\n"))
        agent = tomlkit.parse(text)
        self.assertEqual(agent["name"], result["agent_name"])
        self.assertEqual(agent["model"], "anthropic/example")
        self.assertEqual(agent["model_reasoning_effort"], "low")
        self.assertIn("OpenRouter credits", agent["description"])
        self.assertIn("Do not delegate", agent["developer_instructions"])
        self.assertEqual(agent["model_provider"], settings.PROVIDER)
        provider = agent["model_providers"][settings.PROVIDER]
        self.assertEqual(provider["base_url"], "https://openrouter.ai/api/v1")
        self.assertEqual(provider["wire_api"], "responses")
        self.assertFalse(provider["supports_websockets"])
        self.assertEqual(provider["auth"]["args"], ["--token", "bd3f9c00-03df-4dab-af64-3231575887a0"])
        self.assertEqual(provider["auth"]["timeout_ms"], 5000)
        self.assertEqual(provider["auth"]["refresh_interval_ms"], 300000)

    def test_register_is_idempotent_and_refuses_silent_connection_changes(self):
        result = self.register()
        path = Path(result["agent_path"])
        first = path.read_bytes()
        repeated = self.register()
        self.assertEqual(repeated["agent_name"], result["agent_name"])
        self.assertTrue(repeated["already_registered"])
        self.assertEqual(path.read_bytes(), first)
        replacement = "019f11a1-04d2-799b-ae83-d76f94a51796"
        for fields in ({"account": replacement, "effort": "medium"},
                       {"base_url": "https://api.deepseek.com"},
                       {"base_url": "http://localhost:8080/v1", "account": None}):
            with self.assertRaisesRegex(settings.SettingsError, "another connection"):
                self.register(**fields)
            self.assertEqual(path.read_bytes(), first)
        self.assertFalse(self.config.exists())

    def saved_catalog_connection(self, keyed=True):
        self.state.mkdir(exist_ok=True)
        account = "bd3f9c00-03df-4dab-af64-3231575887a0"
        url = "https://api.deepseek.com"
        preferences = {"accounts": [{"id": account, "name": "DeepSeek", "baseURL": url,
                                      "wire": "chat", "hasKey": keyed}]}
        (self.state / "preferences.json").write_text(json.dumps(preferences))
        return {"account": account, "base_url": url, "wire": "chat", "has_key": keyed,
                "executable": "/Applications/Model Deck.app/Contents/MacOS/ModelDeck"}

    def test_same_connection_refreshes_relocated_credential_command_only(self):
        path = Path(self.register(effort="high")["agent_path"])
        moved = "/Applications/Model Deck.app/Contents/MacOS/ModelDeck"
        self.assertTrue(self.register(executable=moved, effort="medium")["already_registered"])
        agent = tomlkit.parse(path.read_text())
        self.assertEqual(agent["model_providers"][settings.PROVIDER]["auth"]["command"], moved)
        self.assertEqual(agent["model_reasoning_effort"], "high")

    def test_same_connection_refuses_invalid_registered_provider(self):
        path = Path(self.register()["agent_path"])
        agent = tomlkit.parse(path.read_text())
        agent["model_providers"][settings.PROVIDER]["wire_api"] = "chat"
        original = tomlkit.dumps(agent)
        path.write_text(original)
        with self.assertRaisesRegex(settings.SettingsError, "existing model connection is invalid"):
            self.register()
        self.assertEqual(path.read_text(), original)

    def test_endpoint_catalog_uses_only_the_matching_saved_connection(self):
        from subprocess import CompletedProcess
        fields = self.saved_catalog_connection()
        fixture = {"models": [{"id": "deepseek-flash", "name": "DeepSeek Flash"}],
                   "source": "remote", "verified": True, "note": "Catalog only"}
        with patch.object(settings.subprocess, "run", return_value=CompletedProcess([], 0, "fixture-secret\n")) as run, \
                patch("provider_connections.fetch_endpoint_models", return_value=fixture) as fetch:
            result = self.call("endpoint_models", **fields)
            self.assertTrue(result["ok"])
            self.assertEqual(result["models"], fixture["models"])
            fetch.assert_called_once_with(fields["base_url"], key="fixture-secret")
            self.assertEqual(run.call_args.args[0], [fields["executable"], "--token", fields["account"]])
            self.assertNotIn("fixture-secret", json.dumps(result))
        self.assertFalse(self.config.exists())

    def test_endpoint_catalog_rejects_changed_destination_before_reading_key(self):
        fields = self.saved_catalog_connection()
        with patch.object(settings.subprocess, "run") as run, \
                patch("provider_connections.fetch_endpoint_models") as fetch:
            for changed in ({"base_url": "https://example.org"}, {"wire": "responses"},
                            {"has_key": False}, {"account": "019f11a1-04d2-799b-ae83-d76f94a51796"}):
                with self.assertRaises(settings.SettingsError):
                    self.call("endpoint_models", **dict(fields, **changed))
            run.assert_not_called()
            fetch.assert_not_called()

    def test_endpoint_catalog_keyless_and_sanitized_failures(self):
        from subprocess import CompletedProcess
        fields = self.saved_catalog_connection(keyed=False)
        with patch.object(settings.subprocess, "run") as run, \
                patch("provider_connections.fetch_endpoint_models", return_value={"models": []}) as fetch:
            self.assertTrue(self.call("endpoint_models", **fields)["ok"])
            run.assert_not_called()
            fetch.assert_called_once_with(fields["base_url"], key=None)
        fields = self.saved_catalog_connection(keyed=True)
        with patch.object(settings.subprocess, "run", return_value=CompletedProcess([], 0, "fixture-secret")), \
                patch("provider_connections.fetch_endpoint_models", side_effect=RuntimeError("fixture-secret")):
            with self.assertRaises(settings.SettingsError) as caught:
                self.call("endpoint_models", **fields)
            self.assertNotIn("fixture-secret", str(caught.exception))

    def test_register_refuses_unowned_collision_and_symlink(self):
        result = self.register()
        path = Path(result["agent_path"])
        original = b'name = "user-agent"\n'
        path.write_bytes(original)
        with self.assertRaisesRegex(settings.SettingsError, "unowned"):
            self.register()
        self.assertEqual(path.read_bytes(), original)
        path.unlink()
        target = self.root / "target-agent"
        target.write_bytes(original)
        path.symlink_to(target)
        with self.assertRaisesRegex(settings.SettingsError, "Symbolic"):
            self.register()
        self.assertEqual(target.read_bytes(), original)

    def test_register_rejects_invalid_model_ids_without_global_mutation(self):
        original = b'model = "gpt-6-astra"\n'
        self.config.write_bytes(original)
        for model in ("no-slash", "provider/has space", "/empty-provider", "provider/", "p/" + "x" * 256):
            with self.assertRaises(settings.SettingsError):
                self.register(model=model)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(self.agents.exists())

    def test_agent_names_distinguish_normalized_collisions_and_bound_length(self):
        first = self.register(model="p/a-b")
        second = self.register(model="p/a_b")
        self.assertNotEqual(first["agent_name"], second["agent_name"])
        long = self.register(model="p/" + "x" * 240)
        self.assertLessEqual(len(long["agent_name"]), 70)

    def test_register_openai_models_use_normal_subscription(self):
        original = b'model = "gpt-6-astra"\n'
        self.config.write_bytes(original)
        for model in ("openai/gpt-6-astra", "~openai/gpt-latest", "OpenAI/example", "~OPENAI/example"):
            with self.assertRaisesRegex(settings.SettingsError, "normal Codex subscription"):
                self.register(model=model)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(self.agents.exists())

    def test_subscription_agent_preserves_defaults_without_auth(self):
        original = b'# current lead\nmodel = "gpt-6-astra"\nservice_tier = "priority"\n'
        self.config.write_bytes(original)
        result = self.call("register_subscription_agent", model="gpt-5.6-luna")
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(result["agent_name"], "subscription_gpt_5_6_luna")
        self.assertEqual(result["model"], "gpt-6-astra")
        self.assertEqual(result["provider"], "openai")
        self.assertFalse(result["can_restore"])
        path = Path(result["agent_path"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        agent = tomlkit.parse(path.read_text())
        self.assertEqual(agent["model"], "gpt-5.6-luna")
        self.assertEqual(agent["model_provider"], "openai")
        self.assertEqual(agent["model_reasoning_effort"], "low")
        self.assertIn("OpenAI subscription connection", agent["description"])
        self.assertIn("Obey the parent's scope", agent["developer_instructions"])
        self.assertEqual(set(agent), {"name", "description", "developer_instructions", "model",
                                     "model_provider", "model_reasoning_effort"})
        self.assertFalse((self.state / "state.json").exists())

    def test_subscription_registration_idempotence_collision_and_symlink(self):
        result = self.call("register_subscription_agent", model="gpt-6-astra")
        path = Path(result["agent_path"])
        original = path.read_bytes()
        self.call("register_subscription_agent", model="gpt-6-astra")
        self.assertEqual(path.read_bytes(), original)
        self.call("register_subscription_agent", model="gpt-6-astra", effort="medium")
        self.assertEqual(tomlkit.parse(path.read_text())["model_reasoning_effort"], "medium")
        unowned = b'name = "personal"\n'
        path.write_bytes(unowned)
        with self.assertRaisesRegex(settings.SettingsError, "unowned"):
            self.call("register_subscription_agent", model="gpt-6-astra")
        self.assertEqual(path.read_bytes(), unowned)
        path.unlink()
        target = self.root / "personal-agent"
        target.write_bytes(unowned)
        path.symlink_to(target)
        with self.assertRaisesRegex(settings.SettingsError, "Symbolic"):
            self.call("register_subscription_agent", model="gpt-6-astra")
        self.assertEqual(target.read_bytes(), unowned)
        self.assertFalse(self.config.exists())

    def test_subscription_registration_model_allowlist(self):
        for model in ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
                      "gpt-5.5", "gpt-5.3-codex-spark"):
            self.assertTrue(self.call("register_subscription_agent", model=model)["ok"])
        before = sorted(self.agents.iterdir())
        for model in ("openai/gpt-6-astra", "~openai/gpt-latest", "anthropic/example", "gpt-unknown", None):
            with self.assertRaisesRegex(settings.SettingsError, "supported bare"):
                self.call("register_subscription_agent", model=model)
        with self.assertRaisesRegex(settings.SettingsError, "reasoning effort"):
            self.call("register_subscription_agent", model="gpt-6-astra", effort="ultra")
        self.assertEqual(sorted(self.agents.iterdir()), before)
        self.assertFalse(self.config.exists())


if __name__ == "__main__":
    unittest.main()

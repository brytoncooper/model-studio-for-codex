from __future__ import annotations

import importlib.util
import io
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[3]
PREPARE_SCRIPT = REPO_ROOT / "scripts" / "v2" / "prepare_coding_provider.py"
CODEX_SCRIPT = REPO_ROOT / "scripts" / "v2" / "run_isolated_codex.py"


def _load_script(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class PrepareCodingProviderTests(unittest.TestCase):
    def test_managed_agent_becomes_non_secret_v2_profile(self) -> None:
        module = _load_script(PREPARE_SCRIPT, "prepare_coding_provider")
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            managed_agent = root / "openrouter_deepseek.toml"
            managed_agent.write_text(
                "\n".join(
                    [
                        "# Managed by OpenRouter Settings native-agent registration v1",
                        'name = "openrouter_deepseek"',
                        'model = "deepseek/deepseek-v4.1-flash"',
                        'model_provider = "openrouter-settings"',
                        "",
                        "[model_providers.openrouter-settings]",
                        'name = "OpenRouter"',
                        'base_url = "https://openrouter.ai/api/v1"',
                        'wire_api = "responses"',
                        "supports_websockets = false",
                        "",
                        "[model_providers.openrouter-settings.auth]",
                        'command = "/usr/bin/printf"',
                        'args = ["--token", "550E8400-E29B-41D4-A716-446655440000"]',
                        "timeout_ms = 5000",
                        "refresh_interval_ms = 300000",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            output = root / "provider-profile.json"

            module.prepare_coding_provider(managed_agent, output)

            profile = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(profile["schema_version"], 1)
            self.assertEqual(profile["provider_id"], "com.modeldeck.openrouter")
            self.assertEqual(
                profile["provider_model_id"], "deepseek/deepseek-v4.1-flash"
            )
            self.assertEqual(profile["endpoint"]["wire_mode"], "auto")
            self.assertIn("OpenRouter credits", profile["billing_description"])
            self.assertEqual(
                profile["credential_command"]["executable"], "/usr/bin/printf"
            )
            self.assertEqual(
                profile["credential_command"]["args"],
                ["--token", "550E8400-E29B-41D4-A716-446655440000"],
            )
            self.assertNotIn("api_key", json.dumps(profile).casefold())
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_cursor_managed_agent_becomes_isolated_sdk_profile(self) -> None:
        module = _load_script(PREPARE_SCRIPT, "prepare_coding_provider_cursor")
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            managed_agent = root / "cursor.toml"
            managed_agent.write_text(
                "\n".join(
                    [
                        "# Managed by OpenRouter Settings native-agent registration v1",
                        'model = "cursor/default"',
                        'model_provider = "openrouter-settings"',
                        "[model_providers.openrouter-settings]",
                        'name = "Cursor"',
                        'base_url = "https://api.cursor.com"',
                        'wire_api = "responses"',
                        "supports_websockets = false",
                        "[model_providers.openrouter-settings.auth]",
                        'command = "/usr/bin/printf"',
                        'args = ["--token", "550e8400-e29b-41d4-a716-446655440000"]',
                        "timeout_ms = 5000",
                        "refresh_interval_ms = 300000",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            sdk_python = root / "sdk-python"
            sdk_python.write_text("#!/bin/sh\n", encoding="utf-8")
            sdk_python.chmod(0o700)
            workspace = root / "project"
            workspace.mkdir()
            state_root = root / "cursor-state"
            output = root / "profile.json"

            module.prepare_coding_provider(
                managed_agent,
                output,
                cursor_sdk_python=sdk_python,
                cursor_workspace=workspace,
                cursor_state_root=state_root,
            )

            profile = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(profile["provider_id"], "com.modeldeck.provider.cursor")
            self.assertEqual(profile["provider_model_id"], "cursor/default")
            self.assertEqual(profile["sdk_version"], "1.0.31")
            self.assertEqual(profile["sdk_python"], str(sdk_python))
            self.assertEqual(profile["workspace_path"], str(workspace))
            self.assertEqual(profile["state_root"], str(state_root))
            self.assertIn("IDE/Cloud Agent", profile["billing_description"])
            self.assertNotIn("api_key", json.dumps(profile).casefold())
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_cursor_profile_requires_isolated_runtime_paths(self) -> None:
        module = _load_script(PREPARE_SCRIPT, "prepare_coding_provider_cursor_paths")
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            managed_agent = root / "cursor.toml"
            managed_agent.write_text(
                "\n".join(
                    [
                        "# Managed by OpenRouter Settings native-agent registration v1",
                        'model = "cursor/default"',
                        'model_provider = "openrouter-settings"',
                        "[model_providers.openrouter-settings]",
                        'name = "Cursor"',
                        'base_url = "https://api.cursor.com"',
                        'wire_api = "responses"',
                        "supports_websockets = false",
                        "[model_providers.openrouter-settings.auth]",
                        'command = "/usr/bin/printf"',
                        'args = ["--token", "550e8400-e29b-41d4-a716-446655440000"]',
                        "timeout_ms = 5000",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "SDK Python"):
                module.prepare_coding_provider(managed_agent, root / "profile.json")


class IsolatedCodexEnvironmentTests(unittest.TestCase):
    def test_cancellation_timer_starts_after_codex_thread_is_ready(self) -> None:
        module = _load_script(CODEX_SCRIPT, "run_isolated_codex_timer")
        process = SimpleNamespace(
            stdout=io.StringIO(
                '\n'.join([
                    '{"type":"thread.started","thread_id":"thread-1"}',
                    '{"type":"turn.started"}',
                ])
                + '\n'
            )
        )
        observed: list[str] = []

        conversation_id = module._stream_codex_output(
            process,
            output=io.StringIO(),
            on_thread_started=lambda: observed.append("ready"),
        )

        self.assertEqual(conversation_id, "thread-1")
        self.assertEqual(observed, ["ready"])

    def test_environment_and_config_are_confined_to_acceptance_root(self) -> None:
        module = _load_script(CODEX_SCRIPT, "run_isolated_codex")
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_root = root / "v2-state"
            engine_root = state_root / "application-state" / "engine"
            engine_root.mkdir(parents=True)
            token_path = engine_root / "codex-bridge-token"
            token_path.write_text("local-test-token", encoding="utf-8")
            descriptor_path = engine_root / "codex-bridge.json"
            descriptor_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "base_url": "http://127.0.0.1:43123/v1",
                        "provider_id": "com.modeldeck.openrouter",
                        "model": "deepseek/deepseek-v4.1-flash",
                        "billing_description": "OpenRouter credits.",
                        "token_path": str(token_path),
                    }
                ),
                encoding="utf-8",
            )

            launch = module.prepare_isolated_codex_environment(
                state_root=state_root,
                descriptor_path=descriptor_path,
            )

            isolated_root = state_root.resolve() / "codex-harness"
            self.assertTrue(str(launch.codex_home).startswith(str(isolated_root)))
            self.assertEqual(launch.environment["CODEX_HOME"], str(launch.codex_home))
            self.assertEqual(launch.environment["HOME"], str(isolated_root / "home"))
            self.assertEqual(
                launch.environment["XDG_CONFIG_HOME"], str(isolated_root / "xdg-config")
            )
            self.assertEqual(
                launch.environment["MODEL_DECK_V2_BRIDGE_TOKEN"], "local-test-token"
            )
            config = (launch.codex_home / "config.toml").read_text(encoding="utf-8")
            self.assertIn('model_provider = "model_deck_v2"', config)
            self.assertIn('base_url = "http://127.0.0.1:43123/v1"', config)
            self.assertIn('env_key = "MODEL_DECK_V2_BRIDGE_TOKEN"', config)
            self.assertIn('approval_policy = "never"', config)
            self.assertIn('web_search = "disabled"', config)
            self.assertNotIn("local-test-token", config)


if __name__ == "__main__":
    unittest.main()

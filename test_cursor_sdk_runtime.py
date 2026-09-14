"""Runtime setup, secrecy and token accounting with mocked subprocesses."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cursor_sdk_runtime as runtime


class CursorRuntimeTests(unittest.TestCase):
    def test_empty_tool_output_is_a_non_empty_mcp_result(self):
        self.assertEqual(
            runtime._tool_content(""),
            {"content": [{"type": "text", "text": "Tool completed with no output."}]},
        )

    def test_sdk_child_cannot_make_broker_control_pipe_nonblocking(self):
        program = '''
import json, os, subprocess, sys
import cursor_sdk_runtime as runtime
with runtime._isolated_control_input() as commands:
    subprocess.run([sys.executable, "-c", "import os; os.set_blocking(0, False)"], check=True)
    print(json.dumps({"blocking": os.get_blocking(commands.fileno()),
                      "inheritable": os.get_inheritable(commands.fileno()),
                      "commands": [json.loads(line) for line in commands]}))
'''
        result = subprocess.run([sys.executable, "-c", program],
            input='{"command":"start"}\n{"command":"tool_result"}\n',
            capture_output=True, text=True, timeout=10, check=True,
            cwd=Path(runtime.__file__).parent)
        self.assertEqual(json.loads(result.stdout), {"blocking": True, "inheritable": False,
            "commands": [{"command": "start"}, {"command": "tool_result"}]})

    def test_usage_adds_cache_input_once_and_preserves_reasoning_subset(self):
        usage = SimpleNamespace(input_tokens=10, output_tokens=7, cache_read_tokens=5,
                                cache_write_tokens=3, total_tokens=25, reasoning_tokens=2)
        result = runtime._sdk_usage(usage)
        self.assertEqual(result["input_tokens"], 18)
        self.assertEqual(result["output_tokens"], 7)
        self.assertEqual(result["total_tokens"], 25)
        self.assertEqual(result["input_tokens_details"]["cached_tokens"], 5)
        self.assertEqual(result["output_tokens_details"]["reasoning_tokens"], 2)
        self.assertIsNone(runtime._sdk_usage(None))

    def test_environment_drops_credential_and_sdk_debugging(self):
        with patch.dict(os.environ, {"CURSOR_API_KEY": "secret", "CURSOR_SDK_LOG": "debug",
                                     "PYTHONPATH": "/unexpected", "PYTHONHOME": "/other"}):
            environment = runtime._environment()
        for key in ("CURSOR_API_KEY", "CURSOR_SDK_LOG", "PYTHONPATH", "PYTHONHOME"):
            self.assertNotIn(key, environment)

    def test_router_model_requires_account_supported_balance(self):
        catalog = [SimpleNamespace(id="auto-smart", parameters=[SimpleNamespace(id="optimize_for",
                   values=[SimpleNamespace(value="balanced")])])]
        self.assertEqual(runtime._model_selection("auto-smart", catalog), {
            "id": "auto-smart", "params": [{"id": "optimize_for", "value": "balanced"}]})
        catalog[0].parameters[0].values = [SimpleNamespace(value="cost")]
        with self.assertRaisesRegex(runtime.CursorRuntimeError, "router_mode_unavailable"):
            runtime._model_selection("auto-smart", catalog)
        with self.assertRaisesRegex(runtime.CursorRuntimeError, "model_unavailable"):
            runtime._model_selection("nonexistent", catalog)

    def test_fast_and_effort_follow_codex_choices(self):
        def values(*names):
            return [SimpleNamespace(value=name) for name in names]
        grok = SimpleNamespace(id="grok-4.6", parameters=[SimpleNamespace(id="effort", values=values("low", "medium", "high")),
                                                         SimpleNamespace(id="fast", values=values("false", "true"))])
        flash = SimpleNamespace(id="gemini-flash", parameters=[SimpleNamespace(id="reasoning_effort", values=values("low", "high"))])
        catalog = [grok, flash]
        self.assertEqual(runtime._model_selection("grok-4.6", catalog, {"effort": "high"}, "priority")["params"],
                         [{"id": "effort", "value": "high"}, {"id": "fast", "value": "true"}])
        self.assertEqual(runtime._model_selection("grok-4.6", catalog, {"effort": "high"}, "fast")["params"][-1],
                         {"id": "fast", "value": "true"})
        # Fast off (no tier, or Codex's explicit "default") is sent explicitly, never left to Cursor's default variant.
        for tier in (None, "default", "flex"):
            self.assertEqual(runtime._model_selection("grok-4.6", catalog, None, tier)["params"],
                             [{"id": "fast", "value": "false"}], tier)
        self.assertEqual(runtime._model_selection("gemini-flash", catalog, {"effort": "high"})["params"],
                         [{"id": "reasoning_effort", "value": "high"}])
        with self.assertRaisesRegex(runtime.CursorRuntimeError, "fast_unavailable"):
            runtime._model_selection("gemini-flash", catalog, None, "priority")

    def test_catalog_cache_records_fast_capable_models_without_the_key(self):
        rows = [{"id": "cursor/composer-2.5", "name": "Composer 2.5", "parameters": [{"id": "fast", "values": ["false", "true"]}],
                 "variants": []},
                {"id": "cursor/muse-spark-1.3", "name": "Muse Spark 1.3", "parameters": [{"id": "effort", "values": ["low"]}],
                 "variants": []},
                {"id": "grok-4.6", "name": "Grok", "parameters": [{"id": "fast", "values": ["false", "true"]}]}]
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "state" / "cursor-models.json"
            with patch.object(runtime, "catalog_cache_path", return_value=cache):
                self.assertIsNone(runtime.fast_capable_models())
                self.assertIsNone(runtime.catalog_cache_age())
                runtime.save_catalog_cache(rows)
                self.assertEqual(oct(cache.stat().st_mode & 0o777), "0o600")
                self.assertNotIn("api_key", cache.read_text())
                self.assertEqual(runtime.fast_capable_models(), {"cursor/composer-2.5", "cursor/grok-4.6"})
                self.assertLess(runtime.catalog_cache_age(), 60)
                cache.write_text("garbage")
                self.assertIsNone(runtime.fast_capable_models())
        self.assertEqual(runtime.fast_capable_models(rows[:1]), {"cursor/composer-2.5"})

    def test_tool_result_preserves_text_and_image_blocks(self):
        result = runtime._tool_content([{"type": "input_text", "text": "Screenshot"},
                                       {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}])
        self.assertEqual(result, {"content": [{"type": "text", "text": "Screenshot"},
                          {"type": "image", "data": "AAAA", "mimeType": "image/png"}]})

    def test_billed_cost_is_bounded_and_unknown_if_unavailable(self):
        class Client:
            def with_options(self, **options):
                self.options = options
                return self
        client = Client()
        agent = SimpleNamespace(client=client, get_usage=lambda: SimpleNamespace(cost=None))
        self.assertIsNone(runtime._billed_cost(agent))
        self.assertEqual(client.options, {"unary_timeout": 5, "max_retries": 0})
        self.assertIs(agent.client, client)

    def test_status_requires_pinned_version(self):
        with patch.object(runtime, "_installed_version", return_value="older"):
            result = runtime.status()
        self.assertFalse(result["installed"])
        self.assertEqual(result["required_version"], "1.0.31")

    def test_catalog_credentials_only_go_in_stdin(self):
        expected = {"ok": True, "models": [{"id": "cursor/model", "name": "Model"}], "message": "Loaded"}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(runtime, "catalog_cache_path", return_value=Path(directory) / "cursor-models.json"), \
                patch.object(runtime, "status", return_value={"installed": True}), \
                patch.object(runtime, "_bounded_command", return_value=json.dumps(expected)) as command:
            result = runtime.list_models("private-user-key")
            cached = json.loads((Path(directory) / "cursor-models.json").read_text())
        arguments = command.call_args.args
        self.assertNotIn("private-user-key", repr(arguments[0]))
        self.assertEqual(json.loads(arguments[2]), {"api_key": "private-user-key"})
        self.assertEqual(result, expected)
        self.assertEqual(cached["models"], expected["models"])  # a successful listing is cached for the router
        self.assertNotIn("private-user-key", json.dumps(cached))

    def test_failed_catalog_never_returns_raw_error_or_secret(self):
        with patch.object(runtime, "status", return_value={"installed": True}), \
                patch.object(runtime, "_bounded_command", side_effect=runtime.CursorRuntimeError("secret")):
            result = runtime.list_models("secret")
        self.assertFalse(result["ok"])
        self.assertNotIn("secret", repr(result))

    def test_install_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(runtime, "runtime_directory", return_value=Path(directory) / "cursor-sdk"), \
                patch.object(runtime, "_installed_version", return_value=runtime.SDK_VERSION), \
                patch.object(runtime, "_bounded_command") as command:
            result = runtime.install()
            self.assertFalse((Path(directory) / ".cursor-sdk-install.lock").exists())
        self.assertTrue(result["installed"])
        command.assert_not_called()

    def test_install_failure_preserves_previous_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "cursor-sdk"
            target.mkdir()
            marker = target / "previous-version"
            marker.write_text("old runtime")
            with patch.object(runtime, "runtime_directory", return_value=target), \
                    patch.object(runtime, "_installed_version", return_value=None), \
                    patch.object(runtime, "_bounded_command", side_effect=runtime.CursorRuntimeError("failure")):
                result = runtime.install()
            self.assertFalse(result["ok"])
            self.assertEqual(marker.read_text(), "old runtime")
            self.assertEqual(sorted(path.name for path in Path(directory).iterdir()), ["cursor-sdk"])

    def test_failed_relocated_install_rolls_back_verified_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "cursor-sdk"
            target.mkdir()
            (target / "old").write_text("preserved")
            with patch.object(runtime, "runtime_directory", return_value=target), \
                    patch.object(runtime, "_installed_version", side_effect=[None, runtime.SDK_VERSION, None]), \
                    patch.object(runtime, "_bounded_command", return_value=""):
                result = runtime.install()
            self.assertFalse(result["ok"])
            self.assertEqual((target / "old").read_text(), "preserved")

    def test_broken_bridge_after_relocation_restores_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "cursor-sdk"
            target.mkdir()
            (target / "old").write_text("preserved")
            with patch.object(runtime, "runtime_directory", return_value=target), \
                    patch.object(runtime, "_installed_version", side_effect=[None, runtime.SDK_VERSION, runtime.SDK_VERSION]), \
                    patch.object(runtime, "_bounded_command", return_value=""), \
                    patch.object(runtime, "_verify_bridge", side_effect=runtime.CursorRuntimeError("broken")) as verify:
                result = runtime.install()
            verify.assert_called_once()
            self.assertFalse(result["ok"])
            self.assertEqual((target / "old").read_text(), "preserved")

    def test_bridge_verification_launches_installed_python_without_api_key(self):
        with patch.object(runtime, "_bounded_command", return_value="bridge-ready\n") as command:
            runtime._verify_bridge(Path("/installed/runtime"))
        arguments, timeout = command.call_args.args
        self.assertEqual(arguments[0], "/installed/runtime/venv/bin/python3")
        self.assertIn("CursorClient.launch_bridge", arguments[2])
        self.assertIn("allow_api_key_env_fallback=False", arguments[2])
        self.assertEqual(timeout, 40)

    def test_concurrent_install_is_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / ".cursor-sdk-install.lock").write_text("")
            with patch.object(runtime, "runtime_directory", return_value=Path(directory) / "cursor-sdk"), \
                    patch.object(runtime, "_bounded_command") as command:
                result = runtime.install()
        self.assertFalse(result["ok"])
        command.assert_not_called()


if __name__ == "__main__":
    unittest.main()

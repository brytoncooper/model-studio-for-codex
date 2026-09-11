from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

from codex_runtime import discover_runtime
import provider_bridge
import provider_usage


class CodexRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.applications = Path(self.temporary.name)

    def application(self, name, identifier="com.openai.codex"):
        application = self.applications / name
        executable = application / "Contents/Resources/codex"
        executable.parent.mkdir(parents=True)
        executable.write_text("fixture, not executed")
        executable.chmod(0o755)
        (application / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": identifier}))
        return application, executable

    def test_prefers_current_chatgpt_bundle(self):
        self.application("Codex.app")
        application, executable = self.application("ChatGPT.app")
        self.assertEqual(discover_runtime(self.applications), {
            "application_path": str(application), "executable_path": str(executable),
            "bundle_identifier": "com.openai.codex"})

    def test_falls_back_to_legacy_bundle(self):
        application, executable = self.application("Codex.app")
        self.assertEqual(discover_runtime(self.applications)["executable_path"], str(executable))

    def test_wrong_identifier_and_malformed_preferred_bundle_fall_back(self):
        preferred, _ = self.application("ChatGPT.app", "com.some.other.application")
        _, executable = self.application("Codex.app")
        self.assertEqual(discover_runtime(self.applications)["executable_path"], str(executable))
        (preferred / "Contents/Info.plist").write_bytes(b"invalid plist")
        self.assertEqual(discover_runtime(self.applications)["executable_path"], str(executable))

    def test_missing_or_nonexecutable_runtime_fails_clearly(self):
        with self.assertRaisesRegex(RuntimeError, "No supported Codex"):
            discover_runtime(self.applications)
        _, executable = self.application("ChatGPT.app")
        executable.chmod(0o600)
        with self.assertRaises(RuntimeError):
            discover_runtime(self.applications)

    @patch.object(provider_usage, "discover_runtime", return_value={"executable_path": "/fixture/current/codex"})
    @patch.object(provider_usage.subprocess, "Popen", side_effect=RuntimeError("stop before process"))
    def test_usage_client_resolves_runtime_at_invocation(self, spawn, resolve):
        with self.assertRaisesRegex(RuntimeError, "stop before process"):
            provider_usage.NativeUsageClient()
        self.assertEqual(spawn.call_args.args[0], ["/fixture/current/codex", "app-server"])
        resolve.assert_called_once()

    @patch.object(provider_bridge, "discover_runtime", return_value={"executable_path": "/fixture/current/codex"})
    @patch.object(provider_bridge.os, "execv", side_effect=RuntimeError("stop before exec"))
    @patch.object(provider_bridge.sys, "argv", ["provider_bridge.py", "--version"])
    def test_cli_passthrough_resolves_runtime(self, execute, resolve):
        with self.assertRaisesRegex(RuntimeError, "stop before exec"):
            provider_bridge.main()
        execute.assert_called_once_with("/fixture/current/codex", ["/fixture/current/codex", "--version"])


class BridgeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    @patch.object(provider_bridge, "discover_runtime", return_value={"executable_path": "/fixture/current/codex"})
    @patch.object(provider_bridge.asyncio, "create_subprocess_exec", side_effect=RuntimeError("stop before process"))
    async def test_bridge_resolves_runtime_at_invocation(self, spawn, resolve):
        bridge = provider_bridge.ProviderBridge(None, lambda message: None)
        with self.assertRaisesRegex(RuntimeError, "stop before process"):
            await bridge.run(["app-server"])
        self.assertEqual(spawn.call_args.args, ("/fixture/current/codex", "app-server"))
        resolve.assert_called_once()


if __name__ == "__main__":
    unittest.main()

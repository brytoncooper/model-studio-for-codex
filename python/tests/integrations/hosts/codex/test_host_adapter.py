from __future__ import annotations

import plistlib
import tempfile
import unittest
from pathlib import Path

from model_deck.engine.hosts import HostConflictError, HostNotFoundError, HostVersionMismatchError
from model_deck.integrations.hosts.codex.host_adapter import (
    CODEX_API_PROFILE,
    CODEX_HOST_ID,
    CodexHostAdapter,
)


class _Probe:
    def __init__(self, running: bool = False) -> None:
        self.running = running
        self.calls = 0

    def is_codex_running(self) -> bool:
        self.calls += 1
        return self.running


class CodexHostAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.applications = Path(self.temporary.name)
        application = self.applications / "ChatGPT.app"
        executable = application / "Contents/Resources/codex"
        executable.parent.mkdir(parents=True)
        executable.write_text("fixture, not executed", encoding="utf-8")
        executable.chmod(0o755)
        (application / "Contents/Info.plist").write_bytes(
            plistlib.dumps({
                "CFBundleIdentifier": CODEX_HOST_ID,
                "CFBundleShortVersionString": "2026.9",
            })
        )

    def adapter(self, *, protocol: str | None = "app-server.v1", running: bool = False):
        return CodexHostAdapter(
            applications_dir=self.applications,
            observed_protocol_version=protocol,
            supported_protocol_versions=frozenset({"app-server.v1"}),
            process_probe=_Probe(running),
            arguments=("--analytics-default-enabled",),
            overrides=("-c", 'openai_base_url="http://127.0.0.1:1/v1"'),
        )

    def test_lists_and_prepares_supported_host_without_launching(self) -> None:
        adapter = self.adapter()
        self.assertEqual(adapter.list_hosts(), [{
            "host_id": CODEX_HOST_ID,
            "api_profile": CODEX_API_PROFILE,
        }])
        self.assertTrue(adapter.prepare(CODEX_HOST_ID))
        prepared = adapter.last_preparation
        self.assertIsNotNone(prepared)
        assert prepared is not None
        self.assertEqual(prepared.argv[1:3], ("app-server", "--analytics-default-enabled"))
        self.assertEqual(prepared.overrides, ("-c", 'openai_base_url="http://127.0.0.1:1/v1"'))

    def test_unknown_and_unsupported_protocols_refuse_explicitly(self) -> None:
        for protocol in (None, "app-server.future"):
            with self.subTest(protocol=protocol):
                with self.assertRaises(HostVersionMismatchError):
                    self.adapter(protocol=protocol).prepare(CODEX_HOST_ID)

    def test_already_running_is_unverified_and_refuses_prepare(self) -> None:
        adapter = self.adapter(running=True)
        self.assertEqual(adapter.inspect().availability, "already-running-unverified")
        with self.assertRaises(HostConflictError):
            adapter.prepare(CODEX_HOST_ID)

    def test_unknown_host_refuses_before_discovery(self) -> None:
        with self.assertRaises(HostNotFoundError):
            self.adapter().prepare("com.example.unknown")

    def test_missing_runtime_maps_to_host_not_found(self) -> None:
        missing = self.applications / "missing"
        adapter = CodexHostAdapter(
            applications_dir=missing,
            observed_protocol_version="app-server.v1",
            supported_protocol_versions=frozenset({"app-server.v1"}),
            process_probe=_Probe(),
        )
        with self.assertRaises(HostNotFoundError):
            adapter.list_hosts()
        with self.assertRaises(HostNotFoundError):
            adapter.prepare(CODEX_HOST_ID)


if __name__ == "__main__":
    unittest.main()

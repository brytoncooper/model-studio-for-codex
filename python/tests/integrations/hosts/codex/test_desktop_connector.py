from __future__ import annotations

import json
import plistlib
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from model_deck.integrations.hosts.codex.desktop_connector import (
    ConnectorOutcome,
    ConnectorStatus,
    LaunchPlan,
    RunningCodexApplicationProbe,
    attach,
    prepare_connector,
)


class _ProcessProbe:
    def __init__(self, running: bool) -> None:
        self.running = running

    def is_codex_running(self) -> bool:
        return self.running


class DesktopConnectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.engine = self.root / "engine"
        self.engine.mkdir()
        self.rendezvous = self.engine / "rendezvous.json"
        self.rendezvous.write_text("{}", encoding="utf-8")
        self.credential = self.engine / "operator_credential"
        self.credential.write_text("engine-secret", encoding="utf-8")
        self.token = self.engine / "bridge-token"
        self.token.write_text("bridge-secret", encoding="utf-8")
        self.descriptor = self.engine / "codex-bridge.json"
        self.descriptor.write_text(json.dumps({
            "schema_version": 1,
            "base_url": "http://127.0.0.1:54321/v1",
            "provider_id": "com.modeldeck.openrouter",
            "model": "provider/model",
            "display_name": "Provider Model",
            "billing_description": "Provider credits",
            "token_path": str(self.token),
            "registration_id": str(uuid.uuid4()),
            "connection_id": str(uuid.uuid4()),
        }), encoding="utf-8")
        self.bridge = self.root / "CodexDesktopBridge"
        self.bridge.write_text("#!/bin/sh\n", encoding="utf-8")
        self.applications = self.root / "Applications"
        app = self.applications / "ChatGPT.app"
        (app / "Contents/Resources").mkdir(parents=True)
        (app / "Contents/Resources/codex").write_text("", encoding="utf-8")
        (app / "Contents/Resources/codex").chmod(0o755)
        with (app / "Contents/Info.plist").open("wb") as destination:
            plistlib.dump({
                "CFBundleIdentifier": "com.openai.codex",
                "CFBundleShortVersionString": "test",
            }, destination)

    def prepare(self, running: bool = False):
        return prepare_connector(
            rendezvous_path=self.rendezvous,
            credential_path=self.credential,
            descriptor_path=self.descriptor,
            bridge_script_path=self.bridge,
            applications_dir=self.applications,
            observed_protocol_version="codex.app-server.v1",
            supported_protocol_versions=frozenset({"codex.app-server.v1"}),
            process_probe=_ProcessProbe(running),
        )

    def test_ready_plan_reuses_d1a_environment_without_exposing_bridge_token(self) -> None:
        outcome = self.prepare()
        self.assertEqual(outcome.status, ConnectorStatus.READY)
        arguments = " ".join(outcome.plan.argv)
        self.assertIn("CODEX_CLI_PATH=", arguments)
        self.assertIn("MODEL_DECK_V2_ENGINE_RENDEZVOUS_PATH=", arguments)
        self.assertNotIn("bridge-secret", arguments)

    def test_running_desktop_requires_explicit_restart(self) -> None:
        outcome = self.prepare(running=True)
        self.assertEqual(outcome.status, ConnectorStatus.RESTART_REQUIRED)
        self.assertIsNone(outcome.plan)

    @patch("model_deck.integrations.hosts.codex.desktop_connector.subprocess.run")
    def test_default_process_probe_matches_only_the_exact_application_executable(self, run) -> None:
        app = self.applications / "ChatGPT.app"
        executable = app / "Contents/MacOS/ChatGPT"
        run.return_value.returncode = 0
        run.return_value.stdout = (
            f"{executable}\n"
            f"{executable.parent}/ChatGPT Helper --type=renderer\n"
        )

        self.assertTrue(RunningCodexApplicationProbe(app).is_codex_running())

        self.assertEqual(
            run.call_args.args[0],
            ["/bin/ps", "-axo", "command="],
        )

    def test_missing_configuration_is_actionable(self) -> None:
        self.descriptor.unlink()
        outcome = self.prepare()
        self.assertEqual(outcome.status, ConnectorStatus.MISSING_CONFIGURATION)
        self.assertIn("not ready", outcome.reason)

    def test_attach_uses_exact_prepared_arguments(self) -> None:
        plan = LaunchPlan(Path("/usr/bin/open"), ("/usr/bin/open", "-a", "/tmp/Codex.app"), Path("/tmp/Codex.app"))
        calls = []
        outcome = attach(
            ConnectorOutcome(ConnectorStatus.READY, "ready", plan),
            process_launcher=lambda arguments: calls.append(arguments) or 123,
        )
        self.assertEqual(calls, [plan.argv])
        self.assertEqual(outcome.status, ConnectorStatus.CONNECTED)


if __name__ == "__main__":
    unittest.main()

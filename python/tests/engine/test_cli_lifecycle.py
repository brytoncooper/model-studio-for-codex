"""Focused CLI tests for the external plugin lifecycle and panel commands.

These tests cover the public CLI surface added by the CLI owner for the
isolated external-plugin refactor:

    model-deck plugin install/enable/disable/get/list
    model-deck panels list
    model-deck panels get

They drive the CLI through a mocked ``UnixSocketEngineClient`` so the
tests exercise the wire shape, the bundled-contract checks, and the
argument-validation guards without touching the real engine. Every test
asserts the outgoing JSON-RPC method, the params dictionary, and the
stdout/stderr contract.

The wrapper ``invoke`` command and the pack/validate commands live in
``test_cli_invoke.py`` and ``test_cli_plugin_authoring.py`` respectively
and are NOT re-tested here.
"""
from __future__ import annotations

import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from model_deck.cli import main as cli_main


UUID4_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
)


# ---------------------------------------------------------------------------
# Test helpers: same pattern as tests/engine/test_cli_invoke.py so the two
# suites can be read side-by-side.
# ---------------------------------------------------------------------------


def _write_mock_rendezvous_and_credential(directory: Path):
    rendezvous = directory / "rendezvous.json"
    rendezvous.write_text(json.dumps({
        "transport": "unix",
        "socket_path": "/tmp/whatever.sock",
        "engine_instance_id": "550e8400-e29b-41d4-a716-446655440099",
        "instance_nonce": "nonce",
        "api_profile": {"major": 1, "minor": 0},
    }))
    credential = directory / "cred"
    credential.write_text("secret")
    return rendezvous, credential


def _hello1_reply():
    return {"result": {
        "authenticated": False,
        "api_profile": {"major": 1, "minor": 0},
        "engine_instance_id": "550e8400-e29b-41d4-a716-446655440099",
        "instance_nonce": "nonce",
    }}


def _hello2_reply():
    return {"result": {
        "authenticated": True,
        "api_profile": {"major": 1, "minor": 0},
        "engine_instance_id": "550e8400-e29b-41d4-a716-446655440099",
        "instance_nonce": "nonce",
    }}


class _RecordingSession:
    """Stand-in for ``UnixSocketEngineClient.session``.

    Replays a queue of JSON-RPC responses for each ``call(frame)`` and
    records every frame the CLI sent so the test can assert on those
    frames. The lifecycle commands only need hello-1 + hello-2 + one
    command reply, so the queue is short.
    """

    def __init__(self, replies):
        self._replies = list(replies)
        self.frames: list[dict[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def call(self, frame):
        self.frames.append(frame)
        if not self._replies:
            raise AssertionError(
                f"session.call invoked more times than expected "
                f"({len(self.frames)} frames sent)",
            )
        return self._replies.pop(0)


def _mock_client_with_replies(replies):
    standin = mock.MagicMock()
    session = _RecordingSession(replies)

    def factory(_path):
        instance = mock.MagicMock()
        instance.session.return_value = session
        return instance

    standin.side_effect = factory
    return standin, session


def _run_cli(argv: list[str]):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with mock.patch.object(sys, "stdout", stdout), \
            mock.patch.object(sys, "stderr", stderr):
        code = cli_main.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


# ---------------------------------------------------------------------------
# Shared base: every test wires up a temp rendezvous/credential pair.
# ---------------------------------------------------------------------------


class CliLifecycleTestBase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="cli-lifecycle-")
        self.addCleanup(self.directory.cleanup)
        self.workdir = Path(self.directory.name).resolve()
        self.rendezvous, self.credential = _write_mock_rendezvous_and_credential(
            self.workdir,
        )

    def _base_argv(self) -> list[str]:
        return [
            "--rendezvous", str(self.rendezvous),
            "--credential", str(self.credential),
        ]


# ---------------------------------------------------------------------------
# plugin install
# ---------------------------------------------------------------------------


class PluginInstallTests(CliLifecycleTestBase):
    def test_install_sends_extensions_install_with_archive_idempotency_and_revision(self):
        archive = self.workdir / "fixture.mdpack"
        archive.write_bytes(b"fake-archive-bytes")
        replies = [
            _hello1_reply(),
            _hello2_reply(),
            {"result": {"extension_id": "org.example.cli", "version": "1.0.0"}},
        ]
        standin, session = _mock_client_with_replies(replies)
        argv = [
            "plugin", "install", str(archive),
            "--rendezvous", str(self.rendezvous),
            "--credential", str(self.credential),
            "--expected-revision", "3",
        ]
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, stdout, stderr = _run_cli(argv)
        self.assertEqual(exit_code, 0, msg=stderr)
        self.assertEqual(len(session.frames), 3, msg=f"frames={session.frames!r}")
        install_frame = session.frames[2]
        self.assertEqual(install_frame["method"], "engine.v1.extensions.install")
        params = install_frame["params"]
        self.assertEqual(params["archive_path"], str(archive))
        self.assertEqual(params["expected_revision"], 3)
        self.assertRegex(str(params["idempotency_key"]), UUID4_PATTERN)
        parsed = json.loads(stdout)
        self.assertEqual(parsed, {"extension_id": "org.example.cli", "version": "1.0.0"})

    def test_install_default_expected_revision_is_zero(self):
        archive = self.workdir / "fixture.mdpack"
        archive.write_bytes(b"fake")
        replies = [
            _hello1_reply(),
            _hello2_reply(),
            {"result": {"extension_id": "org.example.cli", "version": "1.0.0"}},
        ]
        standin, session = _mock_client_with_replies(replies)
        argv = [
            "plugin", "install", str(archive),
            "--rendezvous", str(self.rendezvous),
            "--credential", str(self.credential),
        ]
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, _, stderr = _run_cli(argv)
        self.assertEqual(exit_code, 0, msg=stderr)
        params = session.frames[2]["params"]
        self.assertEqual(params["expected_revision"], 0)

    def test_install_explicit_idempotency_key_is_passed_through(self):
        archive = self.workdir / "fixture.mdpack"
        archive.write_bytes(b"fake")
        replies = [
            _hello1_reply(),
            _hello2_reply(),
            {"result": {"extension_id": "org.example.cli", "version": "1.0.0"}},
        ]
        standin, session = _mock_client_with_replies(replies)
        argv = [
            "plugin", "install", str(archive),
            "--rendezvous", str(self.rendezvous),
            "--credential", str(self.credential),
            "--idempotency-key", "deterministic-install-key",
        ]
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, _, stderr = _run_cli(argv)
        self.assertEqual(exit_code, 0, msg=stderr)
        params = session.frames[2]["params"]
        self.assertEqual(params["idempotency_key"], "deterministic-install-key")

    def test_install_rejects_relative_archive_path_before_connecting(self):
        argv = [
            "plugin", "install", "relative/path.mdpack",
            "--rendezvous", str(self.rendezvous),
            "--credential", str(self.credential),
        ]
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient") as client_cls:
            exit_code, stdout, stderr = _run_cli(argv)
            client_cls.assert_not_called()
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("absolute archive path", stderr)

    def test_install_invalid_result_schema_is_reported(self):
        archive = self.workdir / "fixture.mdpack"
        archive.write_bytes(b"fake")
        replies = [
            _hello1_reply(),
            _hello2_reply(),
            {"result": {"extension_id": "org.example.cli"}},  # missing version
        ]
        standin, _ = _mock_client_with_replies(replies)
        argv = [
            "plugin", "install", str(archive),
            "--rendezvous", str(self.rendezvous),
            "--credential", str(self.credential),
        ]
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, stdout, stderr = _run_cli(argv)
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("contract validation", stderr)


# ---------------------------------------------------------------------------
# plugin enable / disable (lifecycle)
# ---------------------------------------------------------------------------


class PluginEnableDisableTests(CliLifecycleTestBase):
    def test_enable_sends_extensions_enable_with_extension_id_revision_and_idempotency(self):
        replies = [
            _hello1_reply(),
            _hello2_reply(),
            {"result": {"enabled": True}},
        ]
        standin, session = _mock_client_with_replies(replies)
        argv = [
            "plugin", "enable", "--extension-id", "org.example.cli",
            "--rendezvous", str(self.rendezvous),
            "--credential", str(self.credential),
            "--expected-revision", "7",
        ]
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, stdout, stderr = _run_cli(argv)
        self.assertEqual(exit_code, 0, msg=stderr)
        self.assertEqual(session.frames[2]["method"], "engine.v1.extensions.enable")
        params = session.frames[2]["params"]
        self.assertEqual(params["extension_id"], "org.example.cli")
        self.assertEqual(params["expected_revision"], 7)
        self.assertRegex(str(params["idempotency_key"]), UUID4_PATTERN)
        parsed = json.loads(stdout)
        self.assertEqual(parsed, {"enabled": True})

    def test_disable_sends_extensions_disable_with_explicit_idempotency_key(self):
        replies = [
            _hello1_reply(),
            _hello2_reply(),
            {"result": {"enabled": False}},
        ]
        standin, session = _mock_client_with_replies(replies)
        argv = [
            "plugin", "disable", "--extension-id", "org.example.cli",
            "--rendezvous", str(self.rendezvous),
            "--credential", str(self.credential),
            "--idempotency-key", "deterministic-disable-key",
        ]
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, stdout, stderr = _run_cli(argv)
        self.assertEqual(exit_code, 0, msg=stderr)
        self.assertEqual(session.frames[2]["method"], "engine.v1.extensions.disable")
        params = session.frames[2]["params"]
        self.assertEqual(params["extension_id"], "org.example.cli")
        self.assertEqual(params["idempotency_key"], "deterministic-disable-key")
        # ``expected_revision`` defaults to 0 when omitted.
        self.assertEqual(params["expected_revision"], 0)
        parsed = json.loads(stdout)
        self.assertEqual(parsed, {"enabled": False})

    def test_enable_engine_error_envelope_is_reported(self):
        replies = [
            _hello1_reply(),
            _hello2_reply(),
            {"error": {"code": -32000, "message": "version_mismatch"}},
        ]
        standin, _ = _mock_client_with_replies(replies)
        argv = [
            "plugin", "enable", "--extension-id", "org.example.cli",
            "--rendezvous", str(self.rendezvous),
            "--credential", str(self.credential),
            "--expected-revision", "4",
        ]
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, stdout, stderr = _run_cli(argv)
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("version_mismatch", stderr)

    def test_disable_missing_extension_id_argparse_rejects(self):
        argv = [
            "plugin", "disable",
            "--rendezvous", str(self.rendezvous),
            "--credential", str(self.credential),
        ]
        # argparse exits via SystemExit before any RPC goes out.
        with self.assertRaises(SystemExit) as exc_ctx:
            _run_cli(argv)
        self.assertEqual(exc_ctx.exception.code, 2)


# ---------------------------------------------------------------------------
# plugin get
# ---------------------------------------------------------------------------


class PluginGetTests(CliLifecycleTestBase):
    def test_get_sends_extensions_get_with_extension_id_and_no_revision(self):
        replies = [
            _hello1_reply(),
            _hello2_reply(),
            {
                "result": {
                    "extension_id": "org.example.cli",
                    "status": "enabled",
                    "version": "1.0.0",
                    "revision": 5,
                },
            },
        ]
        standin, session = _mock_client_with_replies(replies)
        argv = [
            "plugin", "get", "--extension-id", "org.example.cli",
            "--rendezvous", str(self.rendezvous),
            "--credential", str(self.credential),
        ]
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, stdout, stderr = _run_cli(argv)
        self.assertEqual(exit_code, 0, msg=stderr)
        self.assertEqual(session.frames[2]["method"], "engine.v1.extensions.get")
        params = session.frames[2]["params"]
        # ``plugin get`` is read-only: no revision, no idempotency key.
        self.assertEqual(params, {"extension_id": "org.example.cli"})
        parsed = json.loads(stdout)
        self.assertEqual(parsed["extension_id"], "org.example.cli")
        self.assertEqual(parsed["status"], "enabled")
        self.assertEqual(parsed["revision"], 5)

    def test_get_unknown_status_value_is_rejected_by_bundled_schema(self):
        replies = [
            _hello1_reply(),
            _hello2_reply(),
            {
                "result": {
                    "extension_id": "org.example.cli",
                    "status": "unknown-state",
                    "revision": 0,
                },
            },
        ]
        standin, _ = _mock_client_with_replies(replies)
        argv = [
            "plugin", "get", "--extension-id", "org.example.cli",
            "--rendezvous", str(self.rendezvous),
            "--credential", str(self.credential),
        ]
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, stdout, stderr = _run_cli(argv)
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("contract validation", stderr)


# ---------------------------------------------------------------------------
# plugin list
# ---------------------------------------------------------------------------


class PluginListTests(CliLifecycleTestBase):
    def test_list_sends_extensions_list_with_empty_params(self):
        replies = [
            _hello1_reply(),
            _hello2_reply(),
            {"result": {"extensions": [
                {"extension_id": "org.example.a", "status": "enabled"},
                {"extension_id": "org.example.b", "status": "disabled"},
            ]}},
        ]
        standin, session = _mock_client_with_replies(replies)
        argv = [
            "plugin", "list",
            "--rendezvous", str(self.rendezvous),
            "--credential", str(self.credential),
        ]
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, stdout, stderr = _run_cli(argv)
        self.assertEqual(exit_code, 0, msg=stderr)
        self.assertEqual(session.frames[2]["method"], "engine.v1.extensions.list")
        # ``extensions.list`` takes no params.
        self.assertEqual(session.frames[2]["params"], {})
        parsed = json.loads(stdout)
        self.assertEqual(len(parsed["extensions"]), 2)

    def test_list_empty_result_array_is_printed_as_empty_array(self):
        replies = [
            _hello1_reply(),
            _hello2_reply(),
            {"result": {"extensions": []}},
        ]
        standin, _ = _mock_client_with_replies(replies)
        argv = [
            "plugin", "list",
            "--rendezvous", str(self.rendezvous),
            "--credential", str(self.credential),
        ]
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, stdout, stderr = _run_cli(argv)
        self.assertEqual(exit_code, 0, msg=stderr)
        self.assertEqual(json.loads(stdout), {"extensions": []})


# ---------------------------------------------------------------------------
# panels list / panels get
# ---------------------------------------------------------------------------


class PanelsCommandsTests(CliLifecycleTestBase):
    def test_panels_list_sends_ui_contributions_list_with_empty_params(self):
        replies = [
            _hello1_reply(),
            _hello2_reply(),
            {"result": {"panels": [
                {"panel_id": "org.example.panels.overview", "title": "Overview"},
            ]}},
        ]
        standin, session = _mock_client_with_replies(replies)
        argv = [
            "panels", "list",
            "--rendezvous", str(self.rendezvous),
            "--credential", str(self.credential),
        ]
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, stdout, stderr = _run_cli(argv)
        self.assertEqual(exit_code, 0, msg=stderr)
        self.assertEqual(
            session.frames[2]["method"], "engine.v1.ui.contributions.list",
        )
        self.assertEqual(session.frames[2]["params"], {})
        parsed = json.loads(stdout)
        self.assertEqual(parsed["panels"][0]["panel_id"], "org.example.panels.overview")

    def test_panel_get_sends_ui_panel_get_with_panel_id(self):
        # Panel tree must satisfy contracts/ui.panel.v1/tree.schema.json:
        # ``panel_id`` / ``revision`` / ``title`` / ``state`` are always
        # required; ``root`` is required only when ``state == "ready"``.
        panel_tree = {
            "panel_id": "org.example.panels.overview",
            "revision": 1,
            "title": "Overview",
            "state": "ready",
            "root": {
                "id": "root",
                "kind": "stack",
                "children": [{
                    "id": "intro", "kind": "text", "value": "Hello",
                }],
            },
        }
        replies = [
            _hello1_reply(),
            _hello2_reply(),
            {"result": {"panel": panel_tree}},
        ]
        standin, session = _mock_client_with_replies(replies)
        argv = [
            "panels", "get", "--panel-id", "org.example.panels.overview",
            "--rendezvous", str(self.rendezvous),
            "--credential", str(self.credential),
        ]
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, stdout, stderr = _run_cli(argv)
        self.assertEqual(exit_code, 0, msg=stderr)
        self.assertEqual(session.frames[2]["method"], "engine.v1.ui.panel.get")
        self.assertEqual(
            session.frames[2]["params"],
            {"panel_id": "org.example.panels.overview"},
        )
        self.assertEqual(json.loads(stdout), {"panel": panel_tree})

    def test_panel_get_missing_required_panel_field_reports_validation_error(self):
        replies = [
            _hello1_reply(),
            _hello2_reply(),
            {"result": {"unexpected": "no panel key"}},
        ]
        standin, _ = _mock_client_with_replies(replies)
        argv = [
            "panels", "get", "--panel-id", "org.example.panels.overview",
            "--rendezvous", str(self.rendezvous),
            "--credential", str(self.credential),
        ]
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, stdout, stderr = _run_cli(argv)
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("contract validation", stderr)


# ---------------------------------------------------------------------------
# Cross-cutting failure modes (apply uniformly to every lifecycle command).
# ---------------------------------------------------------------------------


class LifecycleFailureModesTests(CliLifecycleTestBase):
    def test_authentication_failure_is_reported_before_command_rpc(self):
        # hello-1 returns an authenticated=False with no further replies queued.
        # The CLI follows up with hello-2; reply with an error so the CLI exits.
        replies = [
            _hello1_reply(),
            {"error": {"code": -32001, "message": "credential rejected"}},
        ]
        standin, session = _mock_client_with_replies(replies)
        argv = [
            "plugin", "list",
            "--rendezvous", str(self.rendezvous),
            "--credential", str(self.credential),
        ]
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, stdout, stderr = _run_cli(argv)
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        # Only hello-1 and hello-2 sent; no extensions.list dispatch.
        self.assertEqual(len(session.frames), 2)
        self.assertIn("credential rejected", stderr)

    def test_non_envelope_response_is_reported_as_invalid_engine_response(self):
        replies = [
            _hello1_reply(),
            _hello2_reply(),
            "totally not a dict",
        ]
        standin, _ = _mock_client_with_replies(replies)
        argv = [
            "plugin", "list",
            "--rendezvous", str(self.rendezvous),
            "--credential", str(self.credential),
        ]
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, stdout, stderr = _run_cli(argv)
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("invalid engine response", stderr)

    def test_engine_returns_non_object_result_is_reported(self):
        replies = [
            _hello1_reply(),
            _hello2_reply(),
            {"result": "string-not-object"},
        ]
        standin, _ = _mock_client_with_replies(replies)
        argv = [
            "plugin", "list",
            "--rendezvous", str(self.rendezvous),
            "--credential", str(self.credential),
        ]
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, stdout, stderr = _run_cli(argv)
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("invalid result", stderr)


# ---------------------------------------------------------------------------
# engine serve opt-in
# ---------------------------------------------------------------------------


class EngineServeExtensionsOptInTests(unittest.TestCase):
    """``model-deck engine serve`` validates and forwards extension opt-in."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="cli-serve-")
        self.addCleanup(self.directory.cleanup)
        self.workdir = Path(self.directory.name).resolve()
        self.state_root = self.workdir / "state"
        self.artifact_root = self.workdir / "artifacts"
        self.state_root.mkdir()
        self.artifact_root.mkdir()

    def test_enable_extensions_flag_is_forwarded_when_seam_accepts_it(self):
        captured: dict[str, object] = {}

        def fake_build_engine_server(**kwargs):
            captured.update(kwargs)
            return mock.MagicMock(
                server=mock.MagicMock(serve_forever=lambda: None),
            )

        argv = [
            "engine", "serve",
            "--state-root", str(self.state_root),
            "--artifact-root", str(self.artifact_root),
            "--socket-root", str(self.workdir / "sock"),
            "--legacy-agents-dir", str(self.workdir / "agents"),
            "--enable-application-state",
            "--enable-extensions",
            "--extension-state-root", str(self.state_root),
            "--extension-artifact-root", str(self.artifact_root),
        ]
        with mock.patch(
            "model_deck.bootstrap.build_engine_server",
            side_effect=fake_build_engine_server,
        ):
            exit_code, _, stderr = _run_cli(argv)
        self.assertEqual(exit_code, 0, msg=stderr)
        self.assertTrue(captured.get("enable_external_extensions"))
        self.assertEqual(
            captured.get("extension_state_root"), self.state_root,
        )
        self.assertEqual(
            captured.get("extension_artifact_root"), self.artifact_root,
        )

    def test_enable_extensions_requires_application_state_opt_in(self):
        argv = [
            "engine", "serve",
            "--state-root", str(self.state_root),
            "--artifact-root", str(self.artifact_root),
            "--socket-root", str(self.workdir / "sock"),
            "--legacy-agents-dir", str(self.workdir / "agents"),
            "--enable-extensions",
            "--extension-state-root", str(self.workdir / "extension-state"),
            "--extension-artifact-root", str(self.workdir / "extension-artifacts"),
        ]
        with mock.patch(
            "model_deck.bootstrap.build_engine_server",
        ) as build_mock:
            exit_code, _, stderr = _run_cli(argv)
        self.assertEqual(exit_code, 1)
        self.assertIn("application state", stderr)
        build_mock.assert_not_called()

    def test_enable_extensions_requires_absolute_extension_roots(self):
        # Relative path is rejected before bootstrap is called.
        argv = [
            "engine", "serve",
            "--state-root", str(self.state_root),
            "--artifact-root", str(self.artifact_root),
            "--socket-root", str(self.workdir / "sock"),
            "--legacy-agents-dir", str(self.workdir / "agents"),
            "--enable-application-state",
            "--enable-extensions",
            "--extension-state-root", "relative/state",
            "--extension-artifact-root", str(self.workdir / "extension-artifacts"),
        ]
        with mock.patch(
            "model_deck.bootstrap.build_engine_server",
        ) as build_mock:
            exit_code, _, stderr = _run_cli(argv)
        self.assertEqual(exit_code, 1)
        self.assertIn("absolute", stderr)
        build_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()

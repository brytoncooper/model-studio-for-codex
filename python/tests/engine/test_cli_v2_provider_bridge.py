"""CLI composition tests for the v2 openai-compatible provider + codex bridge.

The CLI loads a profile via OpenAICompatibleProfile.load(Path) and composes
(provider_execution, provider_route_definitions) for build_engine_server.
When --enable-codex-bridge is supplied, the CLI also constructs the bridge,
starts the engine explicitly, waits for interrupt, then stops bridge and
engine. The integration modules are mocked through sys.modules because they
are owned by parallel implementers.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from model_deck.cli import main as cli_main


def _install_provider_modules() -> tuple[Any, Any, Any, Any]:
    """Install fake integration modules in sys.modules.

    Returns ``(config_module, profile_cls, compose_fn, bridge_module)`` so
    individual tests can introspect recorded calls.
    """
    config_module = mock.MagicMock(name="openai_compatible.configuration")
    profile_cls = mock.MagicMock(name="OpenAICompatibleProfile")
    compose_fn = mock.MagicMock(name="compose_openai_compatible_profile")
    config_module.OpenAICompatibleProfile = profile_cls
    config_module.compose_openai_compatible_profile = compose_fn

    bridge_module = mock.MagicMock(name="codex.bridge")
    bridge_instance = mock.MagicMock(name="bridge_instance")
    bridge_cls = mock.MagicMock(name="CodexResponsesBridge", return_value=bridge_instance)
    bridge_module.CodexResponsesBridge = bridge_cls

    modules = {
        "model_deck.integrations.providers.openai_compatible.configuration": config_module,
        "model_deck.integrations.hosts.codex.bridge": bridge_module,
    }
    sys.modules.update(modules)
    return config_module, profile_cls, compose_fn, bridge_module


def _remove_provider_modules() -> None:
    for name in (
        "model_deck.integrations.providers.openai_compatible.configuration",
        "model_deck.integrations.hosts.codex.bridge",
    ):
        sys.modules.pop(name, None)


def _make_basic_args(
    *,
    state_root: Path,
    artifact_root: Path,
    socket_root: Path,
    legacy_agents_dir: Path,
    extras: list[str] | None = None,
) -> list[str]:
    return [
        "engine",
        "serve",
        "--state-root",
        str(state_root),
        "--artifact-root",
        str(artifact_root),
        "--socket-root",
        str(socket_root),
        "--legacy-agents-dir",
        str(legacy_agents_dir),
        "--enable-application-state",
        *(extras or []),
    ]


class CliV2ProviderServeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._modules_installed = False
        _, self.profile_cls, self.compose_fn, _ = _install_provider_modules()
        self._modules_installed = True
        self.addCleanup(_remove_provider_modules)
        self.profile = mock.MagicMock(name="profile")
        self.profile_cls.load.return_value = self.profile
        self.provider_execution = mock.MagicMock(name="provider_execution")
        self.provider_routes = {"org.example.profile": mock.MagicMock(name="route")}
        self.compose_fn.return_value = (self.provider_execution, self.provider_routes)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)
        self.fixtures = Path(__file__).resolve().parent / "fixtures"

    def _paths(self) -> tuple[Path, Path, Path, Path]:
        state = self.tmpdir / "state"
        artifact = self.tmpdir / "artifact"
        socket_root = self.tmpdir / "socket"
        for directory in (state, artifact, socket_root):
            directory.mkdir(parents=True, exist_ok=True)
        return state, artifact, socket_root, self.fixtures / "legacy_agent"

    def _provider_config(self) -> Path:
        path = self.tmpdir / "profile.json"
        path.write_text("{}", encoding="utf-8")
        return path

    def test_provider_config_loads_profile_and_forwards_to_build_engine_server(self) -> None:
        state, artifact, socket_root, legacy = self._paths()
        config = self._provider_config()
        runtime = mock.MagicMock()
        runtime.server.serve_forever.side_effect = lambda: None
        with mock.patch(
            "model_deck.bootstrap.build_engine_server", return_value=runtime,
        ) as build_mock:
            exit_code = cli_main.main(
                _make_basic_args(
                    state_root=state, artifact_root=artifact,
                    socket_root=socket_root, legacy_agents_dir=legacy,
                    extras=["--provider-config", str(config)],
                )
            )
        self.assertEqual(exit_code, 0)
        self.profile_cls.load.assert_called_once()
        loaded_path = self.profile_cls.load.call_args.args[0]
        self.assertEqual(Path(loaded_path), config)
        self.compose_fn.assert_called_once_with(
            self.profile,
            route_definition_factory=mock.ANY,
        )
        kwargs = build_mock.call_args.kwargs
        self.assertIs(kwargs["provider_execution"], self.provider_execution)
        self.assertIs(kwargs["provider_route_definitions"], self.provider_routes)
        self.assertTrue(kwargs["enable_application_state"])

    def test_cursor_profile_uses_cursor_sdk_composition(self) -> None:
        state, artifact, socket_root, legacy = self._paths()
        config = self._provider_config()
        config.write_text(
            json.dumps({"provider_id": "com.modeldeck.provider.cursor"}),
            encoding="utf-8",
        )
        profile = mock.MagicMock(name="cursor_profile")
        provider_execution = mock.MagicMock(name="cursor_execution")
        provider_routes = {"com.modeldeck.provider.cursor": mock.MagicMock()}
        runtime = mock.MagicMock()
        runtime.server.serve_forever.side_effect = lambda: None
        with (
            mock.patch(
                "model_deck.integrations.providers.cursor.configuration.CursorProfile.load",
                return_value=profile,
            ) as load_mock,
            mock.patch(
                "model_deck.integrations.providers.cursor.configuration.compose_cursor_profile",
                return_value=(provider_execution, provider_routes),
            ) as compose_mock,
            mock.patch(
                "model_deck.bootstrap.build_engine_server", return_value=runtime,
            ) as build_mock,
        ):
            exit_code = cli_main.main(
                _make_basic_args(
                    state_root=state,
                    artifact_root=artifact,
                    socket_root=socket_root,
                    legacy_agents_dir=legacy,
                    extras=["--provider-config", str(config)],
                )
            )

        self.assertEqual(exit_code, 0)
        load_mock.assert_called_once_with(config)
        compose_mock.assert_called_once_with(
            profile,
            route_definition_factory=mock.ANY,
        )
        self.assertIs(build_mock.call_args.kwargs["provider_execution"], provider_execution)
        self.assertIs(build_mock.call_args.kwargs["provider_route_definitions"], provider_routes)

    def test_provider_config_must_be_absolute(self) -> None:
        state, artifact, socket_root, legacy = self._paths()
        relative_config = Path("profile.json")
        runtime = mock.MagicMock()
        with mock.patch("model_deck.bootstrap.build_engine_server", return_value=runtime):
            exit_code = cli_main.main(
                _make_basic_args(
                    state_root=state, artifact_root=artifact,
                    socket_root=socket_root, legacy_agents_dir=legacy,
                    extras=["--provider-config", str(relative_config)],
                )
            )
        self.assertEqual(exit_code, 1)
        self.profile_cls.load.assert_not_called()
        runtime.server.serve_forever.assert_not_called()

    def test_provider_config_requires_application_state(self) -> None:
        state, artifact, socket_root, legacy = self._paths()
        config = self._provider_config()
        runtime = mock.MagicMock()
        with mock.patch("model_deck.bootstrap.build_engine_server", return_value=runtime):
            exit_code = cli_main.main(
                [
                    "engine", "serve",
                    "--state-root", str(state),
                    "--artifact-root", str(artifact),
                    "--socket-root", str(socket_root),
                    "--legacy-agents-dir", str(legacy),
                    "--provider-config", str(config),
                ]
            )
        self.assertEqual(exit_code, 1)
        self.profile_cls.load.assert_not_called()
        runtime.server.serve_forever.assert_not_called()

    def test_provider_profile_load_failure_reports_and_skips_build(self) -> None:
        state, artifact, socket_root, legacy = self._paths()
        config = self._provider_config()
        self.profile_cls.load.side_effect = ValueError("invalid profile")
        runtime = mock.MagicMock()
        with mock.patch("model_deck.bootstrap.build_engine_server", return_value=runtime):
            exit_code = cli_main.main(
                _make_basic_args(
                    state_root=state, artifact_root=artifact,
                    socket_root=socket_root, legacy_agents_dir=legacy,
                    extras=["--provider-config", str(config)],
                )
            )
        self.assertEqual(exit_code, 1)
        self.compose_fn.assert_not_called()
        runtime.server.serve_forever.assert_not_called()

    def test_provider_compose_failure_reports_and_skips_build(self) -> None:
        state, artifact, socket_root, legacy = self._paths()
        config = self._provider_config()
        self.compose_fn.side_effect = RuntimeError("compose failed")
        runtime = mock.MagicMock()
        with mock.patch("model_deck.bootstrap.build_engine_server", return_value=runtime):
            exit_code = cli_main.main(
                _make_basic_args(
                    state_root=state, artifact_root=artifact,
                    socket_root=socket_root, legacy_agents_dir=legacy,
                    extras=["--provider-config", str(config)],
                )
            )
        self.assertEqual(exit_code, 1)
        runtime.server.serve_forever.assert_not_called()

    def test_default_serve_still_uses_serve_forever(self) -> None:
        state, artifact, socket_root, legacy = self._paths()
        runtime = mock.MagicMock()
        runtime.server.serve_forever.side_effect = lambda: None
        with mock.patch("model_deck.bootstrap.build_engine_server", return_value=runtime) as build_mock:
            exit_code = cli_main.main(
                _make_basic_args(
                    state_root=state, artifact_root=artifact,
                    socket_root=socket_root, legacy_agents_dir=legacy,
                )
            )
        self.assertEqual(exit_code, 0)
        self.profile_cls.load.assert_not_called()
        self.compose_fn.assert_not_called()
        kwargs = build_mock.call_args.kwargs
        self.assertNotIn("provider_execution", kwargs)
        self.assertNotIn("provider_route_definitions", kwargs)
        runtime.server.start.assert_not_called()
        runtime.server.serve_forever.assert_called_once_with()


class CliV2BridgeServeTests(unittest.TestCase):
    def setUp(self) -> None:
        _, self.profile_cls, self.compose_fn, self.bridge_module = _install_provider_modules()
        self.addCleanup(_remove_provider_modules)
        self.profile = mock.MagicMock(name="profile")
        self.profile_cls.load.return_value = self.profile
        self.provider_execution = mock.MagicMock(name="provider_execution")
        self.provider_routes = {"org.example.profile": mock.MagicMock(name="route")}
        self.compose_fn.return_value = (self.provider_execution, self.provider_routes)
        self.bridge_instance = mock.MagicMock(name="bridge_instance")
        self.bridge_module.CodexResponsesBridge.return_value = self.bridge_instance
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)
        self.fixtures = Path(__file__).resolve().parent / "fixtures"

    def _paths(self) -> tuple[Path, Path, Path, Path]:
        state = self.tmpdir / "state"
        artifact = self.tmpdir / "artifact"
        socket_root = self.tmpdir / "socket"
        for directory in (state, artifact, socket_root):
            directory.mkdir(parents=True, exist_ok=True)
        return state, artifact, socket_root, self.fixtures / "legacy_agent"

    def _provider_config(self) -> Path:
        path = self.tmpdir / "profile.json"
        path.write_text("{}", encoding="utf-8")
        return path

    def _bridge_paths(self, state_root: Path) -> tuple[Path, Path, Path]:
        engine = state_root / "engine"
        engine.mkdir(parents=True, exist_ok=True)
        return (
            engine / "codex.descriptor.json",
            engine / "codex.token",
            engine / "codex.state.json",
        )

    def _serve_forever_raise_keyboard_interrupt(self, runtime: mock.MagicMock) -> None:
        # First call: start, second call: stop in finally. We don't use this
        # directly because the bridge flow has its own wait loop.
        runtime.server.serve_forever.side_effect = lambda: None

    def test_bridge_requires_provider_config(self) -> None:
        state, artifact, socket_root, legacy = self._paths()
        descriptor, token, bridge_state = self._bridge_paths(state)
        runtime = mock.MagicMock()
        with mock.patch("model_deck.bootstrap.build_engine_server", return_value=runtime):
            exit_code = cli_main.main(
                _make_basic_args(
                    state_root=state, artifact_root=artifact,
                    socket_root=socket_root, legacy_agents_dir=legacy,
                    extras=[
                        "--enable-codex-bridge",
                        "--codex-bridge-descriptor", str(descriptor),
                        "--codex-bridge-token", str(token),
                        "--codex-bridge-state", str(bridge_state),
                    ],
                )
            )
        self.assertEqual(exit_code, 1)
        self.profile_cls.load.assert_not_called()
        self.bridge_module.CodexResponsesBridge.assert_not_called()
        runtime.server.serve_forever.assert_not_called()
        runtime.server.start.assert_not_called()

    def test_bridge_requires_all_three_paths(self) -> None:
        state, artifact, socket_root, legacy = self._paths()
        config = self._provider_config()
        descriptor, token, bridge_state = self._bridge_paths(state)
        runtime = mock.MagicMock()
        with mock.patch("model_deck.bootstrap.build_engine_server", return_value=runtime):
            exit_code = cli_main.main(
                _make_basic_args(
                    state_root=state, artifact_root=artifact,
                    socket_root=socket_root, legacy_agents_dir=legacy,
                    extras=[
                        "--provider-config", str(config),
                        "--enable-codex-bridge",
                        "--codex-bridge-descriptor", str(descriptor),
                        "--codex-bridge-state", str(bridge_state),
                    ],
                )
            )
        self.assertEqual(exit_code, 1)
        self.bridge_module.CodexResponsesBridge.assert_not_called()
        runtime.server.serve_forever.assert_not_called()
        runtime.server.start.assert_not_called()

    def test_bridge_paths_must_be_absolute(self) -> None:
        state, artifact, socket_root, legacy = self._paths()
        config = self._provider_config()
        descriptor, token, bridge_state = self._bridge_paths(state)
        runtime = mock.MagicMock()
        with mock.patch("model_deck.bootstrap.build_engine_server", return_value=runtime):
            exit_code = cli_main.main(
                _make_basic_args(
                    state_root=state, artifact_root=artifact,
                    socket_root=socket_root, legacy_agents_dir=legacy,
                    extras=[
                        "--provider-config", str(config),
                        "--enable-codex-bridge",
                        "--codex-bridge-descriptor", "relative.json",
                        "--codex-bridge-token", str(token),
                        "--codex-bridge-state", str(bridge_state),
                    ],
                )
            )
        self.assertEqual(exit_code, 1)
        self.bridge_module.CodexResponsesBridge.assert_not_called()

    def test_bridge_paths_must_be_under_state_root_engine(self) -> None:
        state, artifact, socket_root, legacy = self._paths()
        config = self._provider_config()
        descriptor, token, _ = self._bridge_paths(state)
        bridge_state = self.tmpdir / "outside" / "codex.state.json"  # not under state/engine
        bridge_state.parent.mkdir(parents=True, exist_ok=True)
        runtime = mock.MagicMock()
        with mock.patch("model_deck.bootstrap.build_engine_server", return_value=runtime):
            exit_code = cli_main.main(
                _make_basic_args(
                    state_root=state, artifact_root=artifact,
                    socket_root=socket_root, legacy_agents_dir=legacy,
                    extras=[
                        "--provider-config", str(config),
                        "--enable-codex-bridge",
                        "--codex-bridge-descriptor", str(descriptor),
                        "--codex-bridge-token", str(token),
                        "--codex-bridge-state", str(bridge_state),
                    ],
                )
            )
        self.assertEqual(exit_code, 1)
        self.bridge_module.CodexResponsesBridge.assert_not_called()

    def test_bridge_starts_engine_then_bridge_then_stops_in_reverse(self) -> None:
        state, artifact, socket_root, legacy = self._paths()
        config = self._provider_config()
        descriptor, token, bridge_state = self._bridge_paths(state)

        runtime = mock.MagicMock()
        call_log: list[str] = []

        def fake_start() -> None:
            call_log.append("server.start")

        def fake_stop() -> None:
            call_log.append("server.stop")

        runtime.server.start.side_effect = fake_start
        runtime.server.stop.side_effect = fake_stop
        runtime.rendezvous_path = state / "engine" / "rendezvous.json"
        runtime.enrollment.credential_path = state / "engine" / "operator_credential"

        def fake_bridge_start() -> None:
            call_log.append("bridge.start")

        def fake_bridge_stop() -> None:
            call_log.append("bridge.stop")

        self.bridge_instance.start.side_effect = fake_bridge_start
        self.bridge_instance.stop.side_effect = fake_bridge_stop

        with mock.patch(
            "model_deck.bootstrap.build_engine_server", return_value=runtime,
        ):
            with mock.patch("model_deck.cli.main.threading") as fake_threading:
                event = fake_threading.Event.return_value

                def fake_wait(_seconds: float) -> None:
                    call_log.append("wait")
                    raise KeyboardInterrupt

                event.wait.side_effect = fake_wait
                exit_code = cli_main.main(
                    _make_basic_args(
                        state_root=state, artifact_root=artifact,
                        socket_root=socket_root, legacy_agents_dir=legacy,
                        extras=[
                            "--provider-config", str(config),
                            "--enable-codex-bridge",
                            "--codex-bridge-descriptor", str(descriptor),
                            "--codex-bridge-token", str(token),
                            "--codex-bridge-state", str(bridge_state),
                        ],
                    )
                )
        # KeyboardInterrupt propagates to main() which converts to exit 130.
        self.assertEqual(exit_code, 130)
        self.assertEqual(
            call_log,
            ["server.start", "bridge.start", "wait", "bridge.stop", "server.stop"],
        )
        runtime.server.serve_forever.assert_not_called()

    def test_bridge_constructs_with_profile_and_paths(self) -> None:
        state, artifact, socket_root, legacy = self._paths()
        config = self._provider_config()
        descriptor, token, bridge_state = self._bridge_paths(state)

        runtime = mock.MagicMock()
        runtime.rendezvous_path = state / "engine" / "rendezvous.json"
        runtime.enrollment.credential_path = state / "engine" / "operator_credential"

        with mock.patch(
            "model_deck.bootstrap.build_engine_server", return_value=runtime,
        ):
            with mock.patch("model_deck.cli.main.threading") as fake_threading:
                event = fake_threading.Event.return_value
                event.wait.side_effect = KeyboardInterrupt
                exit_code = cli_main.main(
                    _make_basic_args(
                        state_root=state, artifact_root=artifact,
                        socket_root=socket_root, legacy_agents_dir=legacy,
                        extras=[
                            "--provider-config", str(config),
                            "--enable-codex-bridge",
                            "--codex-bridge-descriptor", str(descriptor),
                            "--codex-bridge-token", str(token),
                            "--codex-bridge-state", str(bridge_state),
                        ],
                    )
                )
        self.assertEqual(exit_code, 130)
        bridge_kwargs = self.bridge_module.CodexResponsesBridge.call_args.kwargs
        self.assertIs(bridge_kwargs["profile"], self.profile)
        self.assertEqual(Path(bridge_kwargs["rendezvous_path"]), runtime.rendezvous_path)
        self.assertEqual(Path(bridge_kwargs["credential_path"]), runtime.enrollment.credential_path)
        self.assertEqual(Path(bridge_kwargs["state_path"]), bridge_state)
        self.assertEqual(Path(bridge_kwargs["token_path"]), token)
        self.assertEqual(Path(bridge_kwargs["descriptor_path"]), descriptor)


if __name__ == "__main__":
    unittest.main()

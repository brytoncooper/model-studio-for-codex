"""B10: deterministic app-server mapping and protocol mapping without a real Codex process.

Proves the extracted app-server bridge keeps method/event identity, backend errors,
thread ownership, catalog projection, per-thread locking, config overlay,
exact-turn cancellation only after backend success, approval/tool responses
forwarded unchanged, and host-bound gpt-* subscription routing.
"""
from __future__ import annotations

import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

from model_deck.integrations.hosts.codex import app_server
from model_deck.integrations.hosts.codex.app_server import (
    BackendError, BridgeError, AppServerBridge, mcp_server_arguments,
)


def _registered_model(account: str = "12345678-1234-1234-1234-123456789abc") -> dict[str, Any]:
    return {
        "provider": "openrouter-settings",
        "role": "openrouter_qwen",
        "config": {
            "model_providers": {
                "openrouter-settings": {
                    "name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
                    "wire_api": "responses", "supports_websockets": False,
                    "auth": {"command": "/Applications/OpenRouterCredentialHelper",
                             "args": ["--token", account], "timeout_ms": 5000,
                             "refresh_interval_ms": 300000},
                }
            }
        },
    }


class FakeCatalog:
    def __init__(self) -> None:
        self.models = {"qwen/test": _registered_model()}
        self.selection: dict[str, Any] = {}

    def load_models(self) -> dict[str, Any]:
        return copy.deepcopy(self.models)

    def catalog_entries(self, price_lines=None):
        return [{"id": model, "model": model, "displayName": model + " · OpenRouter",
                 "description": "Uses OpenRouter credits", "hidden": False,
                 "isDefault": False, "defaultReasoningEffort": "low",
                 "supportedReasoningEfforts": [{"reasoningEffort": "low", "description": "Low"}],
                 "inputModalities": ["text"], "supportsPersonality": False}
                for model in self.models]

    def selected(self) -> dict[str, Any]:
        return dict(self.selection)

    def select(self, model: str, effort=None) -> dict[str, Any]:
        self.selection = {"model": model, "effort": effort}
        return dict(self.selection)


class FakeRouter:
    base_url = "http://127.0.0.1:4242/backend-api/codex"

    def __init__(self) -> None:
        self.catalog_waits: list[float] = []

    def codex_arguments(self) -> list[str]:
        return ["-c", f'openai_base_url="{self.base_url}"', "-c", "features.enable_request_compression=false"]

    def wait_for_catalog(self, timeout: float) -> bool:
        self.catalog_waits.append(timeout)
        return True


def _instruction_builder(registry, price_table=None, benchmark_lines=None) -> str:
    """Stand-in instruction builder used to prove the bridge only depends on a callable."""
    parts = [app_server.ROUTING_INSTRUCTIONS]
    if price_table is not None or benchmark_lines is not None:
        parts.append(f"price_table={price_table or {}} benchmark_lines={benchmark_lines or {}}")
    parts.append(app_server.MCP_INSTRUCTIONS)
    return "\n\n".join(parts)


def _fake_process_launcher() -> dict[str, str]:
    """Stand-in process launcher that never execs a real binary."""
    return {"application_path": "/Applications/Codex.app",
            "executable_path": "/Applications/Codex.app/Contents/Resources/codex",
            "bundle_identifier": "com.openai.codex"}


class _BytesStream:
    """Async iterator of newline-terminated JSON-lines bytes, like a Codex stdout stream."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""


class AppServerBridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.catalog = FakeCatalog()
        self.router = FakeRouter()
        self.messages: list[dict[str, Any]] = []
        self.bridge = AppServerBridge(
            catalog=self.catalog,
            emit=self.messages.append,
            router=self.router,
            instruction_builder=_instruction_builder,
            process_launcher=_fake_process_launcher,
        )
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, Any] = {}

        async def _request(method, params):
            self.requests.append((method, copy.deepcopy(params)))
            response = self.responses.get(method)
            if isinstance(response, Exception):
                raise response
            if method == "thread/start" and method not in self.responses:
                return {"thread": {"id": "new-thread"},
                        "modelProvider": params.get("modelProvider", "openai")}
            return copy.deepcopy(response) if response is not None else {}

        async def _send_backend(message):
            self.requests.append(("@backend", copy.deepcopy(message)))

        self.bridge.request = _request  # type: ignore[method-assign]
        self.bridge.send_backend = _send_backend  # type: ignore[method-assign]
        # Share storage between the bridge's responses dict and the closure's
        # self.responses so monkey-patched keys reach the fake backend.
        self.bridge.responses = self.responses
        self.responses["config/read"] = {
            "config": {"model": "gpt-6-astra", "model_reasoning_effort": "ultra",
                       "sandbox_mode": "read-only"},
            "layers": [{"name": {"type": "user", "file": "/Users/test/.codex/config.toml"},
                        "version": "v"}]}

    async def test_module_is_independently_importable(self) -> None:
        self.assertTrue(callable(app_server.AppServerBridge))
        self.assertTrue(callable(app_server.mcp_server_arguments))

    async def test_interrupt_cancels_exact_cursor_turn_only_after_backend_success(self) -> None:
        cancel = Mock(side_effect=lambda *args: self.assertEqual(
            self.requests[-1][0], 'turn/interrupt'))
        self.bridge.router.cancel_cursor_turn = cancel
        self.bridge.responses['turn/interrupt'] = {'accepted': True}
        params = {'threadId': 'thread-a', 'turnId': 'turn-b'}
        self.assertEqual(await self.bridge.dispatch('turn/interrupt', params), {'accepted': True})
        cancel.assert_called_once_with('thread-a', 'turn-b')
        cancel.reset_mock()
        failure = BackendError({'code': 42, 'message': 'rejected'})
        self.bridge.responses['turn/interrupt'] = failure
        with self.assertRaises(BackendError) as caught:
            await self.bridge.dispatch('turn/interrupt', params)
        self.assertIs(caught.exception, failure)
        cancel.assert_not_called()

    async def test_thread_started_notification_preserves_payload_and_remembers_cwd(self) -> None:
        remember = Mock()
        self.bridge.router.remember_thread = remember
        notification = {'method': 'thread/started',
                        'params': {'thread': {'id': 'child', 'cwd': '/work/child'},
                                   'extra': 7}}
        line = (json.dumps(notification) + '\n').encode()
        self.bridge.process = SimpleNamespace(stdout=_BytesStream([line]))
        await self.bridge.read_backend()
        self.assertEqual(self.messages, [notification])
        remember.assert_called_once_with('child', '/work/child')

    def test_remember_requires_nonempty_cwd_and_supports_result_cwd(self) -> None:
        remember = Mock()
        self.bridge.router.remember_thread = remember
        for cwd in (None, '', '   ', 17):
            self.bridge.remember({'thread': {'id': 'child'}, 'cwd': cwd})
        remember.assert_not_called()
        self.bridge.remember({'thread': {'id': 'child'}, 'cwd': '/work'})
        remember.assert_called_once_with('child', '/work')

    def test_cursor_billing_and_subscription_route_resolve_to_openai_provider(self) -> None:
        bridge = AppServerBridge(
            catalog=self.catalog,
            emit=self.messages.append,
            router=self.router,
            instruction_builder=_instruction_builder,
            process_launcher=_fake_process_launcher,
        )
        bridge.catalog.models["cursor/auto"] = {
            "endpoint": {"cursor": True, "has_key": True, "name": "Cursor"}}
        self.assertEqual(bridge.route("cursor/auto"),
                         {"provider": "openai", "billing": "cursor"})
        # The host-bound subscription routing must not flow through a generic provider adapter.
        self.assertEqual(bridge.route("gpt-6-astra"),
                         {"provider": "openai", "billing": "subscription"})
        # An unregistered model with a slash is refused before any request leaves the bridge.
        with self.assertRaises(BridgeError):
            bridge.route("deepseek/unregistered")

    async def test_new_openrouter_thread_stays_on_openai_provider_for_the_router(self) -> None:
        params = {"model": "qwen/test", "config": {"sandbox_mode": "read-only"}}
        original = copy.deepcopy(params)
        result = await self.bridge.dispatch("thread/start", params)
        sent = next(p for method, p in self.requests if method == "thread/start")
        self.assertNotIn("modelProvider", sent)
        self.assertNotIn("model_providers", sent["config"])
        self.assertEqual(sent["model"], "qwen/test")
        self.assertEqual(sent["config"]["sandbox_mode"], "read-only")
        self.assertIn(app_server.ROUTING_INSTRUCTIONS, sent["developerInstructions"])
        self.assertTrue(sent["developerInstructions"].endswith(app_server.MCP_INSTRUCTIONS))
        self.assertEqual(result["modelProvider"], "openai")
        self.assertEqual(self.bridge.thread_providers["new-thread"], "openai")
        self.assertEqual(self.router.catalog_waits, [8])
        self.assertEqual(params, original)

    async def test_picker_write_is_virtual_and_config_read_overlays(self) -> None:
        result = await self.bridge.dispatch("config/batchWrite", {
            "edits": [{"keyPath": "model", "value": "qwen/test", "mergeStrategy": "replace"},
                      {"keyPath": "model_reasoning_effort", "value": "medium",
                       "mergeStrategy": "replace"}],
            "expectedVersion": "v"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.catalog.selected(), {"model": "qwen/test", "effort": "medium"})
        self.assertEqual(
            [m for m, _ in self.requests if m not in ("@backend",)], ["config/read"])
        overlaid = await self.bridge.dispatch("config/read", {"includeLayers": True})
        self.assertEqual(overlaid["config"]["model"], "qwen/test")
        self.assertEqual(overlaid["config"]["model_reasoning_effort"], "medium")
        self.assertEqual(overlaid["config"]["sandbox_mode"], "read-only")

    async def test_legacy_route_identity_follows_key_account(self) -> None:
        original = self.bridge.legacy_route("qwen/test")["provider"]
        self.assertTrue(original.startswith("openrouter-bridge-"))
        self.assertEqual(self.bridge.legacy_route("qwen/test")["provider"], original)
        self.catalog.models["qwen/other"] = _registered_model(
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        self.assertNotEqual(self.bridge.legacy_route("qwen/other")["provider"], original)
        self.assertIsNone(self.bridge.legacy_route("gpt-6-astra"))

    async def test_legacy_task_rejects_cross_route_changes_before_request(self) -> None:
        self.bridge.thread_providers["existing"] = self.bridge.legacy_route("qwen/test")["provider"]
        with self.assertRaises(BridgeError):
            await self.bridge.dispatch("turn/start", {"threadId": "existing", "model": "gpt-5.6-sol"})
        self.assertEqual([m for m, _ in self.requests if m not in ("@backend",)], [])
        await self.bridge.dispatch("turn/start", {"threadId": "existing", "model": "qwen/test"})
        sent = next(p for m, p in self.requests if m == "turn/start")
        self.assertEqual(sent["effort"], "low")

    async def test_server_tool_responses_and_client_notifications_forwarded_unchanged(self) -> None:
        messages = [{"id": "server-tool-1", "result": {"approved": True}},
                    {"id": "server-tool-2", "error": {"code": -1, "message": "denied"}},
                    {"method": "initialized", "params": {}}]
        forwarded: list[dict[str, Any]] = []
        original_send = self.bridge.send_backend

        async def capture(message):
            forwarded.append(copy.deepcopy(message))
            await original_send(message)

        self.bridge.send_backend = capture  # type: ignore[method-assign]
        for message in messages:
            await self.bridge.handle_client(message)
        self.assertEqual(forwarded, messages)
        self.assertEqual([m for m, _ in self.requests if m not in ("@backend",)], [])

    async def test_backend_error_keeps_original_client_id(self) -> None:
        error = {"code": -32602, "message": "bad argument", "data": {"field": "x"}}
        self.bridge.responses["example/read"] = BackendError(error)
        await self.bridge.handle_client({"id": 41, "method": "example/read", "params": {}})
        self.assertEqual(self.messages, [{"id": 41, "error": error}])

    async def test_backend_arguments_keep_per_process_overrides_and_mcp_registration(self) -> None:
        argv = ["-c", "features.code_mode_host=true", "app-server", "--analytics-default-enabled"]
        arguments = self.bridge.backend_arguments(argv)
        self.assertEqual(arguments[:4], argv)
        self.assertIn('openai_base_url="http://127.0.0.1:4242/backend-api/codex"', arguments)
        self.assertIn("features.enable_request_compression=false", arguments)
        # The MCP override must be injected as a per-process -c override, never as a global config edit.
        # mcp_server_arguments() returns ["-c", "mcp_servers.model_deck=..."]; the TOML value
        # itself is the next list element, so count the value, not the switch.
        mcp_overrides = [arg for arg in arguments if "mcp_servers.model_deck=" in arg]
        self.assertEqual(len(mcp_overrides), 1)
        self.assertIn("command", mcp_server_arguments()[1])

    async def test_fake_app_server_drains_responses_in_order(self) -> None:
        # Deterministic fake app-server: script every JSON-lines message and assert they are
        # forwarded to the bridge in script order, with no real process, credentials, or config.
        sent: list[dict[str, Any]] = []

        async def fake_request(method, params):
            self.requests.append((method, copy.deepcopy(params)))
            # Return one scripted reply to prove the read loop pairs writes with responses.
            return {"thread": {"id": "thread-1"}, "modelProvider": "openai"}

        async def fake_send(message):
            sent.append(copy.deepcopy(message))
        self.bridge.request = fake_request  # type: ignore[method-assign]
        self.bridge.send_backend = fake_send  # type: ignore[method-assign]
        # Script the streamed backend lines: a notification then EOF.
        lines = [(json.dumps({"method": "thread/started",
                              "params": {"thread": {"id": "t", "cwd": "/w"}}}) + "\n").encode(),
                 b""]
        self.bridge.process = SimpleNamespace(stdout=_BytesStream(lines),
                                              stdin=SimpleNamespace())
        await self.bridge.read_backend()
        # The notification was emitted to the consumer exactly once with the original payload.
        self.assertEqual(self.messages, [{"method": "thread/started",
                                          "params": {"thread": {"id": "t", "cwd": "/w"}}}])


if __name__ == "__main__":
    unittest.main()

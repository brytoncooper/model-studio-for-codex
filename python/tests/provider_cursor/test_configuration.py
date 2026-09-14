from __future__ import annotations

import json
import io
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from model_deck.engine.runs.ports import (
    RunOptions,
    RunServiceTier,
    ToolDefinition,
)
from model_deck.integrations.providers.cursor.configuration import (
    CursorProfile,
    CursorSdkProcess,
    _normalize_usage,
    _prepare,
    compose_cursor_profile,
)
from model_deck.integrations.providers.cursor.coordinator import CursorStartRequest


class CursorConfigurationTests(unittest.TestCase):
    def _profile(self, root: Path) -> CursorProfile:
        workspace = root / "project"
        workspace.mkdir()
        document = {
            "schema_version": 1,
            "provider_id": "com.modeldeck.provider.cursor",
            "provider_name": "Cursor",
            "connection_id": "550e8400-e29b-41d4-a716-446655440000",
            "provider_model_id": "cursor/composer-2.5",
            "display_name": "Cursor Composer 2.5",
            "endpoint_config_ref": "ref:test.cursor.endpoint",
            "credential_ref": "ref:test.cursor.credential",
            "capability_snapshot_ref": "ref:test.cursor.serial-tools",
            "credential_command": {
                "executable": "/usr/bin/printf",
                "args": ["test-secret"],
                "timeout_ms": 5000,
            },
            "sdk_python": "/usr/bin/python3",
            "sdk_version": "1.0.31",
            "workspace_path": str(workspace),
            "state_root": str(root / "cursor-state"),
            "billing_description": "Cursor account pool.",
        }
        path = root / "profile.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        with mock.patch(
            "model_deck.integrations.providers.cursor.configuration._installed_sdk_version",
            return_value="1.0.31",
        ):
            return CursorProfile.load(path)

    def test_profile_and_composition_are_strict_and_non_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            profile = self._profile(root)

            coordinator, routes = compose_cursor_profile(
                profile,
                route_definition_factory=lambda **values: values,
            )

            self.assertEqual(profile.provider_model_id, "cursor/composer-2.5")
            self.assertIn("com.modeldeck.provider.cursor", routes)
            self.assertEqual(routes[profile.provider_id]["execution_mode"].value, "custom")
            self.assertEqual(coordinator.open_run_ids, ())

    def test_payload_uses_selected_model_and_only_host_owned_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = self._profile(Path(temporary_directory))
            request = CursorStartRequest(
                run_id="550e8400-e29b-41d4-a716-446655440001",
                session_id="550e8400-e29b-41d4-a716-446655440002",
                connection_id=profile.connection_id,
                provider_model_id=profile.provider_model_id,
                input_messages=({"type": "message", "role": "user", "content": "fix it"},),
                tools=(
                    ToolDefinition("shell", {"type": "object"}, True, "Run a command"),
                    ToolDefinition("provider_internal", {"type": "object"}, False),
                ),
                options=RunOptions(
                    reasoning_effort="high",
                    service_tier=RunServiceTier.PRIORITY,
                    parallel_tool_calls=False,
                ),
            )

            prepared = _prepare(profile, request)

            self.assertEqual(prepared.payload["model"], "composer-2.5")
            self.assertEqual(prepared.payload["api_key"], "test-secret")
            self.assertEqual([tool["name"] for tool in prepared.payload["tools"]], ["shell"])
            self.assertEqual(prepared.payload["tools"][0]["parameters"], {"type": "object"})
            self.assertEqual(prepared.tool_aliases, {"shell": "shell"})
            self.assertNotIn("test-secret", repr(prepared))

    def test_usage_keeps_missing_cost_absent_and_cached_tokens_distinct(self) -> None:
        request = CursorStartRequest(
            run_id="550e8400-e29b-41d4-a716-446655440001",
            session_id="550e8400-e29b-41d4-a716-446655440002",
            connection_id="550e8400-e29b-41d4-a716-446655440000",
            provider_model_id="cursor/composer-2.5",
        )
        records = _normalize_usage(
            request,
            (),
            {
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 3,
                    "input_tokens_details": {"cached_tokens": 5},
                },
                "cursor_usage_cost": None,
            },
        )

        self.assertEqual(
            [(record["unit_kind"], record["units"]) for record in records],
            [("input_tokens", 12), ("output_tokens", 3), ("cached_tokens", 5)],
        )
        self.assertTrue(all("settled_amount" not in record for record in records))

    def test_silent_broker_exit_becomes_a_terminal_runtime_error(self) -> None:
        process = CursorSdkProcess.__new__(CursorSdkProcess)
        process.events = queue.Queue(maxsize=1)
        process._closed = threading.Event()
        process._process = SimpleNamespace(stdout=io.StringIO(""))

        process._read()

        self.assertEqual(process.events.get_nowait(), {"type": "error"})


if __name__ == "__main__":
    unittest.main()

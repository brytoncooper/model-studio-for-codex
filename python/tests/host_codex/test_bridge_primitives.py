from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from model_deck.integrations.hosts.codex.bridge import CodexResponsesBridge, _ResponsesStream
from model_deck.integrations.hosts.codex.state import BridgeState
from model_deck.integrations.hosts.codex.tool_conversion import ToolConversionError, convert_tools, restore_tool_identity
from model_deck.integrations.providers.continuation import compaction


class BridgePrimitiveTests(unittest.TestCase):
    def test_client_disconnect_probe_does_not_consume_live_input(self) -> None:
        server, client = socket.socketpair()
        self.addCleanup(server.close)
        handler = SimpleNamespace(connection=server)

        self.assertFalse(CodexResponsesBridge._client_disconnected(handler))
        client.close()
        self.assertTrue(CodexResponsesBridge._client_disconnected(handler))

    def test_disconnect_watcher_cancels_before_any_engine_event(self) -> None:
        server, client = socket.socketpair()
        self.addCleanup(server.close)
        handler = SimpleNamespace(connection=server)
        engine = _FakeEngine()
        bridge = object.__new__(CodexResponsesBridge)
        bridge.engine = engine
        stop = threading.Event()
        watcher = threading.Thread(
            target=bridge._cancel_when_client_disconnects,
            args=(handler, "550e8400-e29b-41d4-a716-446655440099", stop),
        )
        watcher.start()

        client.close()
        watcher.join(timeout=1)

        self.assertFalse(watcher.is_alive())
        cancel_calls = [
            params
            for method, params in engine.calls
            if method == "engine.v1.runs.cancel"
        ]
        self.assertEqual(len(cancel_calls), 1)
        self.assertEqual(
            cancel_calls[0]["run_id"],
            "550e8400-e29b-41d4-a716-446655440099",
        )

    def test_namespace_conversion_is_stable_and_restores_alias(self) -> None:
        tools, aliases = convert_tools([
            {"type": "namespace", "name": "shell", "tools": [{"type": "function", "name": "run", "parameters": {"type": "object"}}]},
            {"type": "function", "name": "plain"},
        ])
        self.assertEqual([tool["name"] for tool in tools], ["shell__run", "plain"])
        self.assertEqual(restore_tool_identity("shell__run", aliases), ("shell", "run"))
        self.assertTrue(all(tool["host_execution_required"] for tool in tools))

    def test_unsupported_material_is_rejected(self) -> None:
        with self.assertRaises(ToolConversionError):
            convert_tools([{"type": "custom", "name": "x"}])

    def test_provider_native_web_search_is_not_forwarded_to_v2(self) -> None:
        tools, aliases = convert_tools([
            {"type": "web_search", "external_web_access": False},
            {"type": "function", "name": "shell"},
        ])
        self.assertEqual([tool["name"] for tool in tools], ["shell"])
        self.assertEqual(aliases, {"shell": (None, "shell")})

    def test_desktop_tool_description_is_bounded_for_engine_admission(self) -> None:
        tools, _ = convert_tools([
            {"type": "function", "name": "spawn_agent", "description": "x" * 5000},
        ])
        self.assertEqual(len(tools[0]["description"]), 4096)

    def test_state_atomic_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "state.json"
            state = BridgeState(path)
            state.data["pending"]["c"] = {"run_id": "r", "sequence": 4}
            state.save()
            self.assertEqual(json.loads(path.read_text())["pending"]["c"]["run_id"], "r")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_stream_maps_nested_usage_without_provider_cost(self) -> None:
        stream = _ResponsesStream("550e8400-e29b-41d4-a716-446655440000", "model", {})
        stream.translate({"kind": "usage.observed", "usage": {"unit_kind": "input_tokens", "units": 17.0}})
        completed = stream.completed()["response"]
        self.assertEqual(completed["usage"]["input_tokens"], 17)
        self.assertNotIn("cost", completed)

    def test_stream_does_not_publish_reasoning_as_assistant_text(self) -> None:
        stream = _ResponsesStream("550e8400-e29b-41d4-a716-446655440000", "model", {})

        events, terminal = stream.translate(
            {"kind": "content.delta", "channel": "reasoning", "delta": "private"}
        )

        self.assertEqual(events, [])
        self.assertFalse(terminal)
        self.assertNotIn("private", repr(stream.completed()))


class _FakeEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.next_events: list[list[dict]] = []
        self.run_number = 0

    def call(self, method: str, params: dict):
        self.calls.append((method, params))
        if method == "engine.v1.connections.list":
            return {"connections": []}
        if method == "engine.v1.connections.save":
            return {"connection": params["connection"]}
        if method == "engine.v1.models.list":
            return {"collection": "registered", "items": []}
        if method == "engine.v1.models.register":
            return {"model": {"registration_id": "550e8400-e29b-41d4-a716-446655440010"}}
        if method == "engine.v1.sessions.create":
            return {"session_id": "550e8400-e29b-41d4-a716-446655440020"}
        if method == "engine.v1.runs.start":
            self.run_number += 1
            return {"run": {"run_id": f"550e8400-e29b-41d4-a716-44665544003{self.run_number}"}}
        if method in ("engine.v1.runs.submit_tool_result", "engine.v1.runs.cancel"):
            return {"accepted": True}
        raise AssertionError(method)

    def subscribe_events(self, _run_id: str):
        yield from self.next_events.pop(0)


class CodexResponsesBridgeHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        profile = SimpleNamespace(
            connection_id="550e8400-e29b-41d4-a716-446655440001",
            provider_id="com.modeldeck.openrouter",
            endpoint_config_ref="ref:v2.endpoint",
            credential_ref="ref:v2.credential",
            provider_model_id="deepseek/deepseek-v4.1-flash",
            display_name="DeepSeek V4.1 Flash",
            billing_description="OpenRouter credits",
        )
        self.bridge = CodexResponsesBridge(
            rendezvous_path=root / "rendezvous.json", credential_path=root / "credential",
            profile=profile, state_path=root / "bridge-state.json",
            token_path=root / "bridge-token", descriptor_path=root / "bridge.json",
            rendezvous_loader=lambda _path: None, client_factory=lambda *args, **kwargs: None,
            compaction_codec=compaction,
        )
        self.engine = _FakeEngine()
        self.bridge.engine = self.engine
        base_url = self.bridge.start()
        self.addCleanup(self.bridge.stop)
        self.port = int(base_url.split(":")[2].split("/")[0])

    def _post(
        self,
        body: dict,
        *,
        thread_id: str = "thread-1",
        turn_id: str = "turn-1",
    ) -> list[dict]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("POST", "/v1/responses", body=json.dumps(body), headers={
            "Authorization": f"Bearer {self.bridge.token}", "Content-Type": "application/json",
            "x-codex-turn-metadata": json.dumps({"thread_id": thread_id, "turn_id": turn_id}),
        })
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        payload = response.read().decode("utf-8")
        connection.close()
        return [json.loads(block.removeprefix("data: ")) for block in payload.strip().split("\n\n")]

    def _post_path(self, path: str, body: dict, *, thread_id: str = "thread-1"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("POST", path, body=json.dumps(body), headers={
            "Authorization": f"Bearer {self.bridge.token}", "Content-Type": "application/json",
            "x-codex-turn-metadata": json.dumps({"thread_id": thread_id, "turn_id": "compact-1"}),
        })
        response = connection.getresponse()
        payload = response.read().decode("utf-8")
        connection.close()
        return response.status, payload

    def test_streamed_compaction_emits_one_local_item_and_withholds_tools(self) -> None:
        self.engine.next_events.append([
            {"kind": "usage.observed", "usage": {"unit_kind": "output_tokens", "units": 4}},
            {"kind": "content.delta", "delta": "handoff"}, {"kind": "run.completed"},
        ])
        events = self._post({"tools": [{"type": "function", "name": "must_not_run"}],
                             "input": [{"type": "message", "role": "user", "content": "x"},
                                       {"type": "compaction_trigger"}]})
        items = [event["item"] for event in events if event["type"] == "response.output_item.done"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["type"], "compaction")
        starts = [params for method, params in self.engine.calls if method == "engine.v1.runs.start"]
        self.assertEqual(starts[-1]["tools"], [])
        self.assertNotIn("compaction_trigger", json.dumps(starts[-1]))

    def test_unary_compaction_returns_only_item_shape(self) -> None:
        self.engine.next_events.append([{"kind": "content.delta", "delta": "summary"}, {"kind": "run.completed"}])
        status, payload = self._post_path("/v1/responses/compact", {"input": [{"type": "message", "role": "user", "content": "x"}]})
        self.assertEqual(status, 200)
        result = json.loads(payload)
        self.assertEqual(result["output"][0]["type"], "compaction")
        self.assertNotIn("id", result)

    def test_failed_compaction_with_partial_text_returns_no_item(self) -> None:
        self.engine.next_events.append([{"kind": "content.delta", "delta": "partial"}, {"kind": "run.failed"}])
        status, payload = self._post_path("/v1/responses/compact", {"input": [{"type": "message", "role": "user", "content": "x"}]})
        self.assertEqual(status, 502)
        self.assertNotIn("partial", payload)

    def test_streams_text_and_continues_same_engine_session(self) -> None:
        self.engine.next_events.extend([
            [{"kind": "content.delta", "delta": "fixed"}, {"kind": "run.completed"}],
            [{"kind": "content.delta", "delta": "continued"}, {"kind": "run.completed"}],
        ])
        first = self._post({"input": [{"type": "message", "role": "user", "content": "fix"}]})
        second = self._post({"input": [{"type": "message", "role": "user", "content": "follow up"}]}, turn_id="turn-2")
        self.assertIn("fixed", [event.get("delta") for event in first])
        self.assertIn("continued", [event.get("delta") for event in second])
        starts = [params for method, params in self.engine.calls if method == "engine.v1.runs.start"]
        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[0]["session_id"], starts[1]["session_id"])
        self.assertEqual(sum(method == "engine.v1.sessions.create" for method, _ in self.engine.calls), 1)
        self.assertEqual(sum(event["type"] == "response.completed" for event in first), 1)

    def test_opaque_reasoning_is_stripped_before_engine_admission(self) -> None:
        self.engine.next_events.append([
            {"kind": "content.delta", "delta": "continued"},
            {"kind": "run.completed"},
        ])

        events = self._post({
            "input": [
                {"type": "message", "role": "user", "content": "continue"},
                {
                    "type": "reasoning",
                    "id": "rs_private",
                    "encrypted_content": "must-not-cross-the-host-boundary",
                    "summary": [],
                },
            ]
        })

        self.assertIn("continued", [event.get("delta") for event in events])
        start = next(
            params
            for method, params in self.engine.calls
            if method == "engine.v1.runs.start"
        )
        serialized = json.dumps(start)
        self.assertNotIn("rs_private", serialized)
        self.assertNotIn("must-not-cross-the-host-boundary", serialized)

    def test_tool_result_resumes_same_run(self) -> None:
        tools = [{"type": "namespace", "name": "shell", "tools": [{"type": "function", "name": "run", "parameters": {"type": "object"}}]}]
        self.engine.next_events.extend([
            [{"kind": "tool.requested", "sequence": 2, "tool_call": {"call_id": "call-1", "tool_name": "shell__run", "arguments": {"cmd": "test"}}}],
            [{"kind": "content.delta", "delta": "done"}, {"kind": "run.completed"}],
        ])
        first = self._post({"tools": tools, "input": [{"type": "message", "role": "user", "content": "test"}]})
        function_item = next(event["item"] for event in first if event["type"] == "response.output_item.done")
        self.assertEqual((function_item["namespace"], function_item["name"]), ("shell", "run"))
        history = [
            {"type": "message", "role": "user", "content": "test"},
            {"type": "function_call", "namespace": "shell", "name": "run", "call_id": "call-1", "arguments": '{"cmd":"test"}'},
            {"type": "function_call_output", "call_id": "call-1", "output": "passed"},
        ]
        second = self._post({"tools": tools, "input": history})
        self.assertIn("done", [event.get("delta") for event in second])
        submissions = [params for method, params in self.engine.calls if method == "engine.v1.runs.submit_tool_result"]
        self.assertEqual(len(submissions), 1)
        self.assertEqual(submissions[0]["run_id"], "550e8400-e29b-41d4-a716-446655440031")
        self.assertEqual(sum(method == "engine.v1.runs.start" for method, _ in self.engine.calls), 1)

    def test_tool_result_cannot_resume_a_different_thread_run(self) -> None:
        tools = [{"type": "function", "name": "shell"}]
        self.engine.next_events.extend([
            [{"kind": "tool.requested", "sequence": 2, "tool_call": {"call_id": "call-1", "tool_name": "shell", "arguments": {}}}],
            [{"kind": "content.delta", "delta": "separate"}, {"kind": "run.completed"}],
        ])
        self._post({"tools": tools, "input": [{"type": "message", "role": "user", "content": "first"}]})
        history = [
            {"type": "message", "role": "user", "content": "first"},
            {"type": "function_call", "name": "shell", "call_id": "call-1", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call-1", "output": "private"},
        ]

        self._post(
            {"tools": tools, "input": history},
            thread_id="thread-2",
            turn_id="turn-2",
        )

        self.assertFalse(any(
            method == "engine.v1.runs.submit_tool_result"
            for method, _ in self.engine.calls
        ))
        self.assertEqual(
            sum(method == "engine.v1.runs.start" for method, _ in self.engine.calls),
            2,
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from model_deck.engine.routing.ports import ExecutionMode, RouteSnapshot
from model_deck.engine.runs.input_codec import parse_normalized_messages
from model_deck.engine.runs.ports import (
    ProviderCancelTerminationStatus,
    RunOptions,
    RunRequest,
    SubmitToolResultProviderOutcome,
    ToolDefinition,
)
from model_deck.integrations.providers.openai_compatible.execution import (
    OpenAICompatibleEndpointConfig,
    OpenAICompatibleExecutionError,
    OpenAICompatibleExecutionPort,
    WireMode,
)


RUN_ID = "550e8400-e29b-41d4-a716-446655440003"
SESSION_ID = "550e8400-e29b-41d4-a716-446655440004"
REGISTRATION_ID = "550e8400-e29b-41d4-a716-446655440001"
CONNECTION_ID = "550e8400-e29b-41d4-a716-446655440002"
PROVIDER_ID = "com.example.openai-compatible"
NOW = "2026-09-12T12:00:00Z"
SECRET = "test-secret-that-must-not-leak"


def _sse(*envelopes: dict, done: bool = True) -> bytes:
    payload = b"".join(
        b"data: " + json.dumps(envelope, separators=(",", ":")).encode() + b"\n\n"
        for envelope in envelopes
    )
    if done:
        payload += b"data: [DONE]\n\n"
    return payload


class FakeResponse:
    def __init__(self, status: int, *chunks: bytes) -> None:
        self.status = status
        self._chunks = list(chunks)
        self.closed = False
        self.read_calls = 0

    def read1(self, _amount: int = -1) -> bytes:
        self.read_calls += 1
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def close(self) -> None:
        self.closed = True


class BlockingResponse(FakeResponse):
    def __init__(self) -> None:
        super().__init__(
            200,
            _sse({"type": "response.created", "response": {}}, done=False),
        )
        self.read_blocked = threading.Event()
        self._closed_event = threading.Event()

    def read1(self, amount: int = -1) -> bytes:
        if self._chunks:
            return super().read1(amount)
        self.read_blocked.set()
        self._closed_event.wait(timeout=5)
        raise OSError("locally closed")

    def close(self) -> None:
        super().close()
        self._closed_event.set()


class TerminalWinsCloseRaceResponse(FakeResponse):
    def __init__(self) -> None:
        super().__init__(
            200,
            _sse({"type": "response.created", "response": {}}, done=False),
        )
        self.read_blocked = threading.Event()
        self._closed_event = threading.Event()

    def read1(self, amount: int = -1) -> bytes:
        if self._chunks:
            return super().read1(amount)
        if not self._closed_event.is_set():
            self.read_blocked.set()
            self._closed_event.wait(timeout=5)
            return _sse(
                {"type": "response.completed", "response": {}},
            )
        return b""

    def close(self) -> None:
        super().close()
        self._closed_event.set()


class GatedFallbackCloseResponse(FakeResponse):
    def __init__(self) -> None:
        super().__init__(404, b"body must remain unread")
        self.first_close_entered = threading.Event()
        self.second_close_entered = threading.Event()
        self.release_close = threading.Event()
        self._close_lock = threading.Lock()
        self._close_calls = 0

    def close(self) -> None:
        with self._close_lock:
            self._close_calls += 1
            close_call = self._close_calls
        if close_call == 1:
            self.first_close_entered.set()
        elif close_call == 2:
            self.second_close_entered.set()
        self.release_close.wait(timeout=5)
        super().close()


class FakePostStream:
    def __init__(self, *responses: FakeResponse) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, **kwargs):
        with self._lock:
            self.calls.append(kwargs)
            if not self._responses:
                raise AssertionError("unexpected HTTP request")
            return self._responses.pop(0)


class GatedFallbackPostStream(FakePostStream):
    def __init__(self, first: FakeResponse, fallback: FakeResponse) -> None:
        super().__init__(first, fallback)
        self.fallback_open_entered = threading.Event()
        self.release_fallback = threading.Event()

    def __call__(self, **kwargs):
        with self._lock:
            call_number = len(self.calls) + 1
            self.calls.append(kwargs)
            if not self._responses:
                raise AssertionError("unexpected HTTP request")
            response = self._responses.pop(0)
        if call_number == 2:
            self.fallback_open_entered.set()
            self.release_fallback.wait(timeout=5)
        return response


class EventSink:
    def __init__(self) -> None:
        self.events = []
        self._condition = threading.Condition()

    def publish_provider_event(self, event) -> None:
        with self._condition:
            self.events.append(event)
            self._condition.notify_all()

    def wait_for(self, kind: str, timeout: float = 2) -> object:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                for event in self.events:
                    if event.kind == kind:
                        return event
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.fail_timeout(kind)
                self._condition.wait(remaining)

    def fail_timeout(self, kind: str) -> None:
        raise AssertionError(
            f"timed out waiting for {kind}; saw {[event.kind for event in self.events]}"
        )


def _route() -> RouteSnapshot:
    return RouteSnapshot(
        registration_id=REGISTRATION_ID,
        registration_revision=2,
        connection_id=CONNECTION_ID,
        connection_revision=3,
        provider_id=PROVIDER_ID,
        provider_model_id="provider/model-1",
        execution_mode=ExecutionMode.RESPONSES,
        endpoint_config_ref="ref:test.endpoint",
        credential_ref="ref:test.credential",
    )


def _request(
    *,
    run_id: str = RUN_ID,
    options: RunOptions = RunOptions(),
    tools: tuple[ToolDefinition, ...] = (),
) -> RunRequest:
    return RunRequest(
        run_id=run_id,
        session_id=SESSION_ID,
        client_request_id="client-1",
        idempotency_key="execution-1",
        route_snapshot=_route(),
        input=parse_normalized_messages(
            [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                }
            ]
        ),
        tools=tools,
        options=options,
    )


def _config(wire_mode: WireMode = WireMode.AUTO) -> OpenAICompatibleEndpointConfig:
    return OpenAICompatibleEndpointConfig(
        host="provider.invalid",
        port=443,
        secure=True,
        path_prefix="/v1",
        wire_mode=wire_mode,
        vendor_id="example",
    )


def _port(
    post: FakePostStream,
    *,
    config: OpenAICompatibleEndpointConfig | None = None,
    endpoint_calls: list | None = None,
    credential_calls: list | None = None,
) -> OpenAICompatibleExecutionPort:
    def resolve_endpoint(reference: str, revision: int):
        if endpoint_calls is not None:
            endpoint_calls.append((reference, revision))
        return config or _config()

    def resolve_credential(reference: str):
        if credential_calls is not None:
            credential_calls.append(reference)
        return SECRET

    return OpenAICompatibleExecutionPort(
        resolve_endpoint,
        resolve_credential,
        post_stream=post,
        clock=lambda: NOW,
        request_timeout=7,
    )


class OpenAICompatibleExecutionTests(unittest.TestCase):
    def test_responses_http_maps_payload_text_usage_and_terminal(self) -> None:
        response = FakeResponse(
            200,
            _sse(
                {"type": "response.created", "response": {"id": "resp-1"}},
                {"type": "response.output_text.delta", "delta": "hello back"},
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "hello back"}],
                    },
                },
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp-1",
                        "usage": {
                            "input_tokens": 1,
                            "output_tokens": 2,
                            "total_tokens": 3,
                        },
                    },
                },
            ),
        )
        post = FakePostStream(response)
        endpoints: list = []
        credentials: list = []
        port = _port(post, endpoint_calls=endpoints, credential_calls=credentials)
        sink = EventSink()

        started_at = time.monotonic()
        handle = port.start(_request(), sink)
        elapsed = time.monotonic() - started_at
        sink.wait_for("run.completed")

        self.assertLess(elapsed, 0.5)
        self.assertEqual(
            [event.kind for event in sink.events],
            [
                "run.started",
                "content.delta",
                "usage.observed",
                "usage.observed",
                "run.completed",
            ],
        )
        self.assertEqual(sink.events[1].payload, {"delta": "hello back", "channel": "text"})
        input_usage = sink.events[2].payload["usage"]
        self.assertEqual(input_usage["run_id"], RUN_ID)
        self.assertEqual(input_usage["session_id"], SESSION_ID)
        self.assertEqual(input_usage["registration_id"], REGISTRATION_ID)
        self.assertEqual(input_usage["connection_id"], CONNECTION_ID)
        self.assertEqual(input_usage["provider_model_id"], "provider/model-1")
        self.assertEqual(input_usage["unit_kind"], "input_tokens")
        self.assertEqual(input_usage["units"], 1.0)
        output_usage = sink.events[3].payload["usage"]
        self.assertEqual(output_usage["run_id"], RUN_ID)
        self.assertEqual(output_usage["session_id"], SESSION_ID)
        self.assertEqual(output_usage["registration_id"], REGISTRATION_ID)
        self.assertEqual(output_usage["connection_id"], CONNECTION_ID)
        self.assertEqual(output_usage["provider_model_id"], "provider/model-1")
        self.assertEqual(output_usage["unit_kind"], "output_tokens")
        self.assertEqual(output_usage["units"], 2.0)
        for usage_payload in (input_usage, output_usage):
            self.assertNotIn("total_tokens", usage_payload)
        self.assertEqual(endpoints, [("ref:test.endpoint", 3)])
        self.assertEqual(credentials, ["ref:test.credential"])
        self.assertEqual(len(post.calls), 1)
        call = post.calls[0]
        self.assertEqual(call["path"], "/v1/responses")
        self.assertEqual(call["host"], "provider.invalid")
        self.assertEqual(call["port"], 443)
        self.assertIs(call["secure"], True)
        self.assertEqual(call["timeout"], 7)
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {SECRET}")
        payload = json.loads(call["payload"])
        self.assertEqual(payload["model"], "provider/model-1")
        self.assertEqual(payload["input"], json.loads(json.dumps(list(_request().input.messages))))
        self.assertIs(payload["stream"], True)
        self.assertTrue(response.closed)
        self.assertNotIn(SECRET, repr(handle))

    def test_tool_result_resumes_once_with_completed_history(self) -> None:
        first = FakeResponse(
            200,
            _sse(
                {"type": "response.created", "response": {"id": "resp-1"}},
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "call_id": "call-1",
                        "name": "lookup",
                        "arguments": '{"city":"Oslo"}',
                    },
                },
                {"type": "response.completed", "response": {"id": "resp-1"}},
            ),
        )
        second = FakeResponse(
            200,
            _sse(
                {"type": "response.created", "response": {"id": "resp-2"}},
                {"type": "response.output_text.delta", "delta": "sunny"},
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "sunny"}],
                    },
                },
                {"type": "response.completed", "response": {"id": "resp-2"}},
            ),
        )
        post = FakePostStream(first, second)
        endpoints: list = []
        credentials: list = []
        port = _port(post, endpoint_calls=endpoints, credential_calls=credentials)
        sink = EventSink()
        tool = ToolDefinition(
            name="lookup",
            description="Lookup weather",
            input_schema={"type": "object"},
            host_execution_required=True,
        )
        handle = port.start(_request(tools=(tool,)), sink)
        sink.wait_for("tool.requested")

        wrong = handle.submit_tool_result("call-wrong", {"forecast": "rain"})
        accepted = handle.submit_tool_result("call-1", {"forecast": "sunny"})
        repeated = handle.submit_tool_result("call-1", {"forecast": "sunny"})
        sink.wait_for("run.completed")

        self.assertEqual(wrong.outcome, SubmitToolResultProviderOutcome.REJECTED)
        self.assertEqual(accepted.outcome, SubmitToolResultProviderOutcome.ACCEPTED)
        self.assertEqual(repeated.outcome, SubmitToolResultProviderOutcome.ACCEPTED)
        self.assertEqual(len(post.calls), 2)
        self.assertEqual(endpoints, [("ref:test.endpoint", 3)])
        self.assertEqual(credentials, ["ref:test.credential"])
        second_payload = json.loads(post.calls[1]["payload"])
        self.assertEqual(
            second_payload["input"][-2:],
            [
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "lookup",
                    "arguments": '{"city":"Oslo"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": '{"forecast":"sunny"}',
                },
            ],
        )
        self.assertEqual(
            [event.kind for event in sink.events].count("run.started"), 1
        )
        self.assertEqual(
            [event.kind for event in sink.events].count("run.completed"), 1
        )

    def test_auto_fallback_is_pre_body_once_and_cached_by_endpoint_revision(self) -> None:
        chat_chunk = {
            "choices": [
                {"index": 0, "delta": {"content": "fallback"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        post = FakePostStream(
            FakeResponse(404, b"body-that-must-not-be-read"),
            FakeResponse(200, _sse(chat_chunk)),
            FakeResponse(200, _sse(chat_chunk)),
        )
        port = _port(post)
        first_sink = EventSink()
        second_sink = EventSink()

        port.start(_request(), first_sink)
        first_sink.wait_for("run.completed")
        port.start(
            _request(run_id="550e8400-e29b-41d4-a716-446655440005"),
            second_sink,
        )
        second_sink.wait_for("run.completed")

        self.assertEqual(
            [call["path"] for call in post.calls],
            ["/v1/responses", "/v1/chat/completions", "/v1/chat/completions"],
        )
        self.assertEqual(
            [event.payload["delta"] for event in first_sink.events if event.kind == "content.delta"],
            ["fallback"],
        )

    def test_all_fallback_statuses_retry_once_and_fixed_modes_never_retry(self) -> None:
        chat_chunk = {
            "choices": [
                {"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}
            ]
        }
        for status in (404, 405, 501):
            with self.subTest(auto_status=status):
                rejected = FakeResponse(status, b"unread response body")
                post = FakePostStream(
                    rejected,
                    FakeResponse(200, _sse(chat_chunk)),
                )
                sink = EventSink()
                _port(post).start(_request(), sink)
                sink.wait_for("run.completed")
                self.assertEqual(len(post.calls), 2)
                self.assertEqual(rejected.read_calls, 0)

        fixed_responses = FakePostStream(
            FakeResponse(404, b"no retry"),
            FakeResponse(200, _sse(chat_chunk)),
        )
        fixed_sink = EventSink()
        _port(
            fixed_responses,
            config=_config(WireMode.RESPONSES),
        ).start(_request(), fixed_sink)
        fixed_sink.wait_for("run.failed")
        self.assertEqual(len(fixed_responses.calls), 1)

        fixed_chat = FakePostStream(FakeResponse(200, _sse(chat_chunk)))
        chat_sink = EventSink()
        _port(
            fixed_chat,
            config=_config(WireMode.CHAT_COMPLETIONS),
        ).start(_request(), chat_sink)
        chat_sink.wait_for("run.completed")
        self.assertEqual(fixed_chat.calls[0]["path"], "/v1/chat/completions")

    def test_no_fallback_after_200_decode_failure_or_other_status(self) -> None:
        for status, chunks in (
            (200, (b"data: {secret-invalid-json}\n\n",)),
            (429, (b"ignored",)),
        ):
            with self.subTest(status=status):
                post = FakePostStream(FakeResponse(status, *chunks))
                sink = EventSink()
                port = _port(post)
                port.start(_request(), sink)
                failed = sink.wait_for("run.failed")
                self.assertEqual(len(post.calls), 1)
                self.assertEqual(
                    failed.payload["terminal_result"]["error"]["message"],
                    "The provider execution failed.",
                )
                self.assertNotIn("secret-invalid-json", repr(failed))

    def test_parallel_tools_rejected_before_resolution_or_http(self) -> None:
        post = FakePostStream()
        endpoint_calls: list = []
        credential_calls: list = []
        port = _port(
            post,
            endpoint_calls=endpoint_calls,
            credential_calls=credential_calls,
        )

        with self.assertRaises(OpenAICompatibleExecutionError):
            port.start(
                _request(options=RunOptions(parallel_tool_calls=True)),
                EventSink(),
            )

        self.assertEqual(post.calls, [])
        self.assertEqual(endpoint_calls, [])
        self.assertEqual(credential_calls, [])

    def test_cancel_closes_local_response_and_terminalizes_once(self) -> None:
        response = BlockingResponse()
        post = FakePostStream(response)
        port = _port(post)
        sink = EventSink()
        handle = port.start(_request(), sink)
        sink.wait_for("run.started")
        self.assertTrue(response.read_blocked.wait(timeout=2))

        cancellation = handle.request_cancel(deadline=NOW)
        sink.wait_for("run.cancelled")
        repeated = handle.request_cancel(deadline=NOW)

        self.assertTrue(cancellation.request_accepted)
        self.assertEqual(
            cancellation.termination_status,
            ProviderCancelTerminationStatus.UNCONFIRMED,
        )
        self.assertFalse(repeated.request_accepted)
        self.assertEqual(
            repeated.termination_status,
            ProviderCancelTerminationStatus.CONFIRMED,
        )
        self.assertTrue(response.closed)
        self.assertEqual(
            [event.kind for event in sink.events].count("run.cancelled"), 1
        )

    def test_cancel_suppresses_terminal_returned_after_acceptance(self) -> None:
        response = TerminalWinsCloseRaceResponse()
        sink = EventSink()
        handle = _port(FakePostStream(response)).start(_request(), sink)
        sink.wait_for("run.started")
        self.assertTrue(response.read_blocked.wait(timeout=2))

        cancellation = handle.request_cancel(deadline=NOW)
        sink.wait_for("run.cancelled")

        terminal_events = [
            event
            for event in sink.events
            if event.kind in {"run.completed", "run.failed", "run.cancelled", "run.interrupted"}
        ]
        self.assertEqual(cancellation.termination_status, ProviderCancelTerminationStatus.UNCONFIRMED)
        self.assertEqual([event.kind for event in terminal_events], ["run.cancelled"])

    def test_cancel_during_fallback_close_prevents_chat_request(self) -> None:
        rejected = GatedFallbackCloseResponse()
        post = FakePostStream(
            rejected,
            FakeResponse(200, _sse({"type": "response.completed", "response": {}})),
        )
        sink = EventSink()
        handle = _port(post).start(_request(), sink)
        self.assertTrue(rejected.first_close_entered.wait(timeout=2))
        cancellation_result: list = []

        cancel_thread = threading.Thread(
            target=lambda: cancellation_result.append(
                handle.request_cancel(deadline=NOW)
            )
        )
        cancel_thread.start()
        self.assertTrue(rejected.second_close_entered.wait(timeout=2))
        rejected.release_close.set()
        cancel_thread.join(timeout=2)
        self.assertFalse(cancel_thread.is_alive())
        sink.wait_for("run.interrupted")

        self.assertTrue(cancellation_result[0].request_accepted)
        self.assertEqual(len(post.calls), 1)
        self.assertEqual(rejected._chunks, [b"body must remain unread"])
        self.assertFalse(any(event.kind == "content.delta" for event in sink.events))

    def test_cancel_while_fallback_opens_closes_it_without_content(self) -> None:
        fallback = FakeResponse(
            200,
            _sse(
                {"type": "response.created", "response": {}},
                {"type": "response.output_text.delta", "delta": "must not publish"},
                {"type": "response.completed", "response": {}},
            ),
        )
        post = GatedFallbackPostStream(FakeResponse(404), fallback)
        sink = EventSink()
        handle = _port(post).start(_request(), sink)
        self.assertTrue(post.fallback_open_entered.wait(timeout=2))

        cancellation = handle.request_cancel(deadline=NOW)
        post.release_fallback.set()
        sink.wait_for("run.interrupted")

        self.assertTrue(cancellation.request_accepted)
        self.assertEqual(len(post.calls), 2)
        self.assertTrue(fallback.closed)
        self.assertEqual(len(fallback._chunks), 1)
        self.assertFalse(any(event.kind == "content.delta" for event in sink.events))

    def test_resolver_failures_are_fixed_and_do_not_leak_secrets(self) -> None:
        post = FakePostStream()

        def fail_endpoint(_reference: str, _revision: int):
            raise RuntimeError(SECRET)

        port = OpenAICompatibleExecutionPort(
            fail_endpoint,
            lambda _reference: SECRET,
            post_stream=post,
            clock=lambda: NOW,
        )
        sink = EventSink()
        handle = port.start(_request(), sink)

        failed = sink.wait_for("run.failed")

        self.assertEqual(post.calls, [])
        self.assertNotIn(SECRET, repr(failed))
        self.assertNotIn(SECRET, repr(handle))


class EngineProviderInjectionFixtureTests(unittest.TestCase):
    def test_real_engine_accepts_injected_http_provider_events(self) -> None:
        from model_deck.adapters.routing.registered import ProviderRouteDefinition
        from model_deck.adapters.transport.rendezvous import load_rendezvous_file
        from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
        from model_deck.bootstrap import build_engine_server
        from model_deck_contracts.paths import repo_root

        response = FakeResponse(
            200,
            _sse(
                {"type": "response.created", "response": {}},
                {"type": "response.output_text.delta", "delta": "engine result"},
                {"type": "response.completed", "response": {}},
            ),
        )
        provider = _port(FakePostStream(response))
        temporary = TemporaryDirectory(prefix="md-http-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        runtime = build_engine_server(
            state_root=root / "state",
            artifact_root=root / "artifact",
            socket_root=root / "socket",
            source_root=repo_root(),
            legacy_agents_dir=(
                Path(__file__).resolve().parents[1] / "engine" / "fixtures" / "legacy_agent"
            ),
            default_connection_id=CONNECTION_ID,
            enable_application_state=True,
            provider_execution=provider,
            provider_route_definitions={
                PROVIDER_ID: ProviderRouteDefinition(
                    execution_mode=ExecutionMode.RESPONSES,
                    capability_features=(),
                    capability_snapshot_ref="ref:test.capabilities",
                )
            },
        )
        runtime.server.start()
        self.addCleanup(runtime.server.stop)
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text().strip()

        def call(session, request_id: int, method: str, params: dict) -> dict:
            response_envelope = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": f"engine.v1.{method}",
                    "params": params,
                }
            )
            self.assertIn("result", response_envelope, response_envelope)
            return response_envelope["result"]

        with UnixSocketEngineClient(descriptor.socket_path).session() as session:
            authenticated = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "engine.v1.hello",
                    "params": {
                        "client_name": "http-provider-execution-test",
                        "offered_api": {"major": 1, "minor": 0},
                        "authentication": {
                            "engine_instance_id": descriptor.engine_instance_id,
                            "instance_nonce": descriptor.instance_nonce,
                            "credential": credential,
                        },
                    },
                }
            )
            self.assertTrue(authenticated["result"]["authenticated"])
            call(
                session,
                3,
                "connections.save",
                {
                    "expected_revision": 0,
                    "idempotency_key": "http-connection",
                    "connection": {
                        "connection_id": CONNECTION_ID,
                        "provider_id": PROVIDER_ID,
                        "endpoint_config_ref": "ref:test.endpoint",
                        "credential_ref": "ref:test.credential",
                    },
                },
            )
            model = call(
                session,
                4,
                "models.register",
                {
                    "connection_id": CONNECTION_ID,
                    "provider_model_id": "provider/model-1",
                    "display_name": "HTTP Fixture",
                    "expected_revision": 0,
                    "idempotency_key": "http-model",
                },
            )["model"]
            session_id = call(
                session,
                5,
                "sessions.create",
                {"registration_id": model["registration_id"]},
            )["session_id"]
            run = call(
                session,
                6,
                "runs.start",
                {
                    "session_id": session_id,
                    "registration_id": model["registration_id"],
                    "client_request_id": "http-provider-fixture",
                    "idempotency_key": "http-run",
                },
            )["run"]
            deadline = time.monotonic() + 2
            while run["state"] != "completed" and time.monotonic() < deadline:
                run = call(session, 7, "runs.get", {"run_id": run["run_id"]})["run"]
                time.sleep(0.01)
            self.assertEqual(run["state"], "completed")


if __name__ == "__main__":
    unittest.main()

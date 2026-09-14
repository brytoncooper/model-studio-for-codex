from __future__ import annotations

import json
import http.client
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from model_deck.engine.routing.ports import (
    CapabilityFeature,
    CapabilityTriState,
    ContinuationScope,
    ExecutionMode,
    RouteSnapshot,
)
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
from model_deck.integrations.providers.continuation.compaction import (
    SUMMARIZATION_PROMPT,
    SUMMARY_PREFIX,
)
from model_deck.integrations.providers.continuation import compaction
from model_deck.integrations.providers.continuation.store import ContinuationRouteScope, ContinuationStore


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
    session_id: str = SESSION_ID,
    route: RouteSnapshot | None = None,
    continuation_scope: ContinuationScope | None = None,
    options: RunOptions = RunOptions(),
    tools: tuple[ToolDefinition, ...] = (),
) -> RunRequest:
    return RunRequest(
        run_id=run_id,
        session_id=session_id,
        client_request_id="client-1",
        idempotency_key="execution-1",
        route_snapshot=route or _route(),
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
        continuation_scope=continuation_scope,
    )


def _continuation_scope(**overrides: object) -> ContinuationScope:
    values = {
        "connection_id": CONNECTION_ID,
        "provider_model_id": "provider/model-1",
        "provider_id": PROVIDER_ID,
        "execution_mode": ExecutionMode.RESPONSES,
        "handle": "ref:continuation-handle-1",
    }
    values.update(overrides)
    return ContinuationScope(**values)


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
    continuation_store: ContinuationStore | None = None,
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
        continuation_store=continuation_store,
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


class OpenAICompatibleContinuationIntegrationTests(unittest.TestCase):
    def _tool(self) -> ToolDefinition:
        return ToolDefinition(
            name="lookup",
            description="Lookup weather",
            input_schema={"type": "object"},
            host_execution_required=True,
        )

    def _message_response(self, text: str = "done", *, encrypted: str | None = None) -> FakeResponse:
        response_id = f"resp-message-{text}"
        item = {
            "type": "message",
            "id": "msg-provider-1",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }
        if encrypted is not None:
            item["encrypted_content"] = encrypted
        return FakeResponse(
            200,
            _sse(
                {"type": "response.created", "response": {"id": response_id}},
                {"type": "response.output_item.done", "item": item},
                {"type": "response.completed", "response": {"id": response_id}},
            ),
        )

    def _route_scope(self) -> ContinuationRouteScope:
        return ContinuationRouteScope(
            session_id=SESSION_ID,
            connection_id=CONNECTION_ID,
            connection_revision=3,
            provider_id=PROVIDER_ID,
            provider_model_id="provider/model-1",
            execution_mode=ExecutionMode.RESPONSES.value,
            endpoint_config_ref="ref:test.endpoint",
            credential_ref="ref:test.credential",
            continuation_handle="ref:continuation-handle-1",
        )

    def test_same_scope_tool_resume_restores_private_function_fields(self) -> None:
        scope = _continuation_scope()
        first = FakeResponse(
            200,
            _sse(
                {"type": "response.created", "response": {"id": "resp-tool-1"}},
                {"type": "response.output_item.done", "item": {
                    "type": "function_call", "id": "fc-provider-1", "call_id": "call-1",
                    "name": "lookup", "arguments": '{"city":"Oslo"}',
                    "encrypted_function_args": "opaque-args", "signature": "sig-1",
                }},
                {"type": "response.completed", "response": {"id": "resp-tool-1"}},
            ),
        )
        second = self._message_response()
        post = FakePostStream(first, second)
        with TemporaryDirectory(prefix="md-continuation-", dir="/tmp") as folder:
            store = ContinuationStore(Path(folder) / "continuation.sqlite3")
            port = _port(post, continuation_store=store)
            sink = EventSink()
            handle = port.start(
                _request(continuation_scope=scope, tools=(self._tool(),)), sink
            )
            sink.wait_for("tool.requested")
            self.assertEqual(
                handle.submit_tool_result("call-1", {"forecast": "sunny"}).outcome,
                SubmitToolResultProviderOutcome.ACCEPTED,
            )
            sink.wait_for("run.completed")
            resumed = json.loads(post.calls[1]["payload"])
            function_call = next(item for item in resumed["input"] if item.get("type") == "function_call")
            self.assertEqual(function_call["encrypted_function_args"], "opaque-args")
            self.assertEqual(function_call["signature"], "sig-1")

    def test_different_scope_and_missing_record_fail_before_http(self) -> None:
        scope = _continuation_scope()
        with TemporaryDirectory(prefix="md-continuation-", dir="/tmp") as folder:
            store = ContinuationStore(Path(folder) / "continuation.sqlite3")
            store.save_response(self._route_scope(), "resp-old", [("item-1", {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "old"}],
            }, {"raw_item": {"type": "message", "role": "assistant",
                               "content": [{"type": "output_text", "text": "old"}],
                               "encrypted_content": "opaque"}})])
            for request in (
                _request(continuation_scope=_continuation_scope(handle="ref:other-handle")),
                replace(_request(continuation_scope=scope), route_snapshot=replace(_route(), connection_revision=99)),
            ):
                post = FakePostStream(self._message_response())
                sink = EventSink()
                endpoint_calls: list = []
                credential_calls: list = []
                _port(
                    post,
                    continuation_store=store,
                    endpoint_calls=endpoint_calls,
                    credential_calls=credential_calls,
                ).start(request, sink)
                sink.wait_for("run.failed")
                self.assertEqual(post.calls, [])
                self.assertEqual(endpoint_calls, [])
                self.assertEqual(credential_calls, [])

            mismatch = _request(
                continuation_scope=scope,
                route=_route(),
            )
            mismatch = replace(mismatch, input=parse_normalized_messages([
                    {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "not-old"}]}
                ]))
            post = FakePostStream(self._message_response())
            sink = EventSink()
            endpoint_calls = []
            credential_calls = []
            _port(
                post,
                continuation_store=store,
                endpoint_calls=endpoint_calls,
                credential_calls=credential_calls,
            ).start(mismatch, sink)
            sink.wait_for("run.failed")
            self.assertEqual(post.calls, [])
            self.assertEqual(endpoint_calls, [])
            self.assertEqual(credential_calls, [])

    def test_store_reopen_reuses_state_and_compaction_clears_it(self) -> None:
        scope = _continuation_scope()
        with TemporaryDirectory(prefix="md-continuation-", dir="/tmp") as folder:
            path = Path(folder) / "continuation.sqlite3"
            store = ContinuationStore(path)
            first_post = FakePostStream(self._message_response("remember", encrypted="opaque"))
            first_sink = EventSink()
            _port(first_post, continuation_store=store).start(
                _request(continuation_scope=scope), first_sink
            )
            first_sink.wait_for("run.completed")

            reopened = ContinuationStore(path)
            history_request = _request(continuation_scope=scope)
            history_request = replace(history_request, input=parse_normalized_messages([
                    {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "remember"}]}
                ]))
            second_post = FakePostStream(self._message_response("next"))
            second_sink = EventSink()
            _port(second_post, continuation_store=reopened).start(history_request, second_sink)
            second_sink.wait_for("run.completed")
            restored = json.loads(second_post.calls[0]["payload"])
            self.assertEqual(restored["input"][0]["encrypted_content"], "opaque")

            barrier_request = _request(continuation_scope=scope)
            barrier_request = replace(barrier_request, input=parse_normalized_messages([
                    {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": SUMMARY_PREFIX + "\nsummary"}]}
                ]))
            barrier_post = FakePostStream(self._message_response("after"))
            barrier_sink = EventSink()
            _port(barrier_post, continuation_store=reopened).start(barrier_request, barrier_sink)
            barrier_sink.wait_for("run.completed")
            records = reopened.load_all(self._route_scope())
            self.assertTrue(records)
            self.assertTrue(all(
                record.metadata.get("raw_item", {}).get("encrypted_content") != "opaque"
                for record in records
            ))

    def test_failed_session_persistence_rolls_back_prepared_provider_reset(self) -> None:
        from model_deck.adapters.storage.sqlite_session_run_repository import (
            SQLiteSessionRunRepository,
        )
        from model_deck.engine.routing.ports import ContinuationScope
        from model_deck.engine.sessions.ports import (
            CreateSessionCommand,
            GetSessionCommand,
            SelectModelCommand,
        )

        with TemporaryDirectory(prefix="md-continuation-reset-", dir="/tmp") as folder:
            root = Path(folder)
            store = ContinuationStore(root / "provider-continuation.sqlite3")
            provider = _port(
                FakePostStream(self._message_response()), continuation_store=store
            )
            repository = SQLiteSessionRunRepository(
                root / "state.sqlite3",
                uuid_factory=lambda: SESSION_ID,
                continuation_reset=provider,
            )
            engine_scope = _continuation_scope()
            repository.create(
                CreateSessionCommand(
                    registration_id=REGISTRATION_ID,
                    continuation_scope=engine_scope,
                )
            )
            visible = {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "old"}],
            }
            store.save_response(
                self._route_scope(),
                "resp-old",
                [("old-item", visible, {"raw_item": visible})],
            )
            invalid_replacement = ContinuationScope(
                connection_id=CONNECTION_ID,
                provider_model_id="provider/model-2",
                provider_id=PROVIDER_ID,
                execution_mode="invalid",  # type: ignore[arg-type]
                handle="ref:continuation.replacement",
            )

            with self.assertRaises(AttributeError):
                repository.select_model(
                    SelectModelCommand(
                        session_id=SESSION_ID,
                        registration_id=REGISTRATION_ID,
                        expected_revision=1,
                        continuation_reset=True,
                        replacement_continuation_scope=invalid_replacement,
                    )
                )

            stored_session = repository.get(GetSessionCommand(session_id=SESSION_ID))
            self.assertEqual(stored_session.continuation_scope, engine_scope)
            self.assertEqual(store.load_all(self._route_scope())[0].response_id, "resp-old")

    def test_reset_commit_failure_is_adopted_by_replacement_scope(self) -> None:
        from model_deck.adapters.storage.sqlite_session_run_repository import (
            SQLiteSessionRunRepository,
        )
        from model_deck.engine.sessions.ports import (
            CreateSessionCommand,
            GetSessionCommand,
            SelectModelCommand,
        )

        with TemporaryDirectory(prefix="md-continuation-reset-", dir="/tmp") as folder:
            root = Path(folder)
            store = ContinuationStore(root / "provider-continuation.sqlite3")
            provider = _port(
                FakePostStream(self._message_response()), continuation_store=store
            )

            class CommitFailureReset:
                def prepare_session_continuation_reset(
                    self, session_id: str, continuation_handle: str
                ) -> str | None:
                    return provider.prepare_session_continuation_reset(
                        session_id, continuation_handle
                    )

                def commit_session_continuation_reset(self, _reset_token: str) -> None:
                    raise RuntimeError("simulated reset commit failure")

                def rollback_session_continuation_reset(self, reset_token: str) -> None:
                    provider.rollback_session_continuation_reset(reset_token)

            repository = SQLiteSessionRunRepository(
                root / "state.sqlite3",
                uuid_factory=lambda: SESSION_ID,
                continuation_reset=CommitFailureReset(),
            )
            engine_scope = _continuation_scope()
            replacement_engine_scope = _continuation_scope(
                provider_model_id="provider/model-2",
                handle="ref:continuation.replacement",
            )
            repository.create(
                CreateSessionCommand(
                    registration_id=REGISTRATION_ID,
                    continuation_scope=engine_scope,
                )
            )
            visible = {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "old"}],
            }
            old_route_scope = self._route_scope()
            store.save_response(
                old_route_scope,
                "resp-old",
                [("old-item", visible, {"raw_item": visible})],
            )

            with self.assertRaisesRegex(RuntimeError, "simulated reset commit failure"):
                repository.select_model(
                    SelectModelCommand(
                        session_id=SESSION_ID,
                        registration_id=REGISTRATION_ID,
                        expected_revision=1,
                        continuation_reset=True,
                        replacement_continuation_scope=replacement_engine_scope,
                    )
                )

            stored_session = repository.get(GetSessionCommand(session_id=SESSION_ID))
            self.assertEqual(stored_session.revision, 2)
            self.assertEqual(
                stored_session.continuation_scope, replacement_engine_scope
            )
            self.assertEqual(store.load_all(old_route_scope)[0].response_id, "resp-old")

            replacement_route_scope = ContinuationRouteScope(
                session_id=SESSION_ID,
                connection_id=CONNECTION_ID,
                connection_revision=3,
                provider_id=PROVIDER_ID,
                provider_model_id="provider/model-2",
                execution_mode=ExecutionMode.RESPONSES.value,
                endpoint_config_ref="ref:test.endpoint",
                credential_ref="ref:test.credential",
                continuation_handle="ref:continuation.replacement",
            )
            self.assertEqual(store.load_all(replacement_route_scope), [])
            store.save_response(
                replacement_route_scope,
                "resp-new",
                [("new-item", visible, {"raw_item": visible})],
            )
            self.assertEqual(
                store.load_all(replacement_route_scope)[0].response_id,
                "resp-new",
            )


class EngineProviderInjectionFixtureTests(unittest.TestCase):
    def test_real_engine_accepts_provider_events_and_resets_continuation(self) -> None:
        from model_deck.adapters.routing.registered import ProviderRouteDefinition
        from model_deck.adapters.transport.rendezvous import load_rendezvous_file
        from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
        from model_deck.bootstrap import build_engine_server
        from model_deck_contracts.paths import repo_root

        temporary = TemporaryDirectory(prefix="md-http-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        continuation_store = ContinuationStore(
            root / "state" / "engine" / "provider-continuation.sqlite3"
        )

        def provider_response(response_id: str, text: str) -> FakeResponse:
            return FakeResponse(
                200,
                _sse(
                    {"type": "response.created", "response": {"id": response_id}},
                    {"type": "response.output_text.delta", "delta": text},
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "message",
                            "id": f"msg-{response_id}",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text}],
                            "encrypted_content": f"opaque-{response_id}",
                        },
                    },
                    {"type": "response.completed", "response": {"id": response_id}},
                ),
            )

        post = FakePostStream(
            provider_response("resp-before-reset", "engine result"),
            provider_response("resp-after-reset", "fresh route result"),
        )
        provider = _port(post, continuation_store=continuation_store)
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

            replacement_model = call(
                session,
                8,
                "models.register",
                {
                    "connection_id": CONNECTION_ID,
                    "provider_model_id": "provider/model-2",
                    "display_name": "HTTP Fixture Replacement",
                    "expected_revision": 0,
                    "idempotency_key": "http-model-replacement",
                },
            )["model"]
            selected = call(
                session,
                9,
                "sessions.select_model",
                {
                    "session_id": session_id,
                    "registration_id": replacement_model["registration_id"],
                    "expected_revision": 1,
                    "continuation_reset": True,
                },
            )
            self.assertEqual(selected["revision"], 2)
            replacement_run = call(
                session,
                10,
                "runs.start",
                {
                    "session_id": session_id,
                    "registration_id": replacement_model["registration_id"],
                    "client_request_id": "http-provider-fixture-after-reset",
                    "idempotency_key": "http-run-after-reset",
                },
            )["run"]
            deadline = time.monotonic() + 2
            while replacement_run["state"] != "completed" and time.monotonic() < deadline:
                replacement_run = call(
                    session,
                    11,
                    "runs.get",
                    {"run_id": replacement_run["run_id"]},
                )["run"]
                time.sleep(0.01)
            self.assertEqual(replacement_run["state"], "completed")
            self.assertEqual(len(post.calls), 2)

    def test_codex_bridge_continues_tools_and_resumes_after_compaction(self) -> None:
        from model_deck.adapters.routing.registered import ProviderRouteDefinition
        from model_deck.adapters.transport.rendezvous import load_rendezvous_file
        from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
        from model_deck.bootstrap import build_engine_server
        from model_deck.integrations.hosts.codex.bridge import CodexResponsesBridge
        from model_deck_contracts.paths import repo_root

        def message_response(response_id: str, text: str, **private_fields: str) -> FakeResponse:
            item = {
                "type": "message",
                "id": f"msg-{response_id}",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
                **private_fields,
            }
            return FakeResponse(200, _sse(
                {"type": "response.created", "response": {"id": response_id}},
                {"type": "response.output_text.delta", "delta": text},
                {"type": "response.output_item.done", "item": item},
                {"type": "response.completed", "response": {"id": response_id}},
            ))

        tool_response = FakeResponse(200, _sse(
            {"type": "response.created", "response": {"id": "resp-tool"}},
            {"type": "response.output_item.done", "item": {
                "type": "function_call",
                "id": "fc-provider",
                "call_id": "call-weather",
                "name": "lookup",
                "arguments": '{"city":"Oslo"}',
                "encrypted_function_args": "opaque-tool-arguments",
                "signature": "synthetic-tool-signature",
            }},
            {"type": "response.completed", "response": {"id": "resp-tool"}},
        ))
        post = FakePostStream(
            message_response(
                "resp-first",
                "remembered alpha",
                encrypted_content="opaque-first-state",
                signature="synthetic-first-signature",
            ),
            tool_response,
            message_response("resp-after-tool", "tool complete", encrypted_content="opaque-tool-state"),
            message_response("resp-summary", "alpha and its tool result are retained"),
            message_response("resp-after-compact", "continued after compact", encrypted_content="fresh-state"),
        )

        temporary = TemporaryDirectory(prefix="md-b15-integrated-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        continuation_path = root / "state" / "engine" / "provider-continuation.sqlite3"
        provider = _port(post, continuation_store=ContinuationStore(continuation_path))
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
                    capability_features=(
                        CapabilityFeature("tools", CapabilityTriState.SUPPORTED),
                        CapabilityFeature(
                            "parallel_tool_calls",
                            CapabilityTriState.UNSUPPORTED,
                        ),
                    ),
                    capability_snapshot_ref="ref:test.capabilities",
                )
            },
        )
        runtime.server.start()
        self.addCleanup(runtime.server.stop)
        profile = SimpleNamespace(
            connection_id=CONNECTION_ID,
            provider_id=PROVIDER_ID,
            endpoint_config_ref="ref:test.endpoint",
            credential_ref="ref:test.credential",
            provider_model_id="provider/model-1",
            display_name="B15 integrated fixture",
            billing_description="deterministic fixture; no billing",
        )
        bridge = CodexResponsesBridge(
            rendezvous_path=runtime.rendezvous_path,
            credential_path=runtime.enrollment.credential_path,
            profile=profile,
            state_path=root / "bridge-state.json",
            token_path=root / "bridge-token",
            descriptor_path=root / "bridge.json",
            rendezvous_loader=load_rendezvous_file,
            client_factory=UnixSocketEngineClient,
            compaction_codec=compaction,
        )
        base_url = bridge.start()
        self.addCleanup(bridge.stop)
        port_number = int(base_url.split(":")[2].split("/")[0])

        def send(body: dict, *, thread_id: str = "thread-one") -> tuple[int, list[dict]]:
            connection = http.client.HTTPConnection("127.0.0.1", port_number, timeout=10)
            connection.request(
                "POST",
                "/v1/responses",
                body=json.dumps(body),
                headers={
                    "Authorization": f"Bearer {bridge.token}",
                    "Content-Type": "application/json",
                    "x-codex-turn-metadata": json.dumps({
                        "thread_id": thread_id,
                        "turn_id": f"turn-{thread_id}-{len(post.calls)}",
                    }),
                },
            )
            response = connection.getresponse()
            payload = response.read().decode("utf-8")
            status = response.status
            connection.close()
            events = [
                json.loads(block.removeprefix("data: "))
                for block in payload.strip().split("\n\n")
                if block.startswith("data: ")
            ]
            return status, events

        first_history = [
            {"type": "message", "role": "user", "content": "remember alpha"},
        ]
        status, first_events = send({"input": first_history})
        self.assertEqual(status, 200)
        self.assertTrue(any(event["type"] == "response.completed" for event in first_events))

        second_history = [
            *first_history,
            {"type": "message", "role": "assistant", "content": "remembered alpha"},
            {"type": "message", "role": "user", "content": "use lookup"},
        ]
        status, tool_events = send({
            "input": second_history,
            "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}],
        })
        self.assertEqual(status, 200)
        tool_item = next(
            event["item"] for event in tool_events
            if event["type"] == "response.output_item.done"
        )
        self.assertEqual(tool_item["call_id"], "call-weather")
        second_wire = json.loads(post.calls[1]["payload"])
        restored_first = next(
            item for item in second_wire["input"]
            if item.get("type") == "message" and item.get("role") == "assistant"
        )
        self.assertEqual(restored_first["encrypted_content"], "opaque-first-state")

        tool_history = [
            *second_history,
            tool_item,
            {"type": "function_call_output", "call_id": "call-weather", "output": "sunny"},
        ]
        status, after_tool_events = send({
            "input": tool_history,
            "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}],
        })
        self.assertEqual(status, 200)
        self.assertTrue(any(event["type"] == "response.completed" for event in after_tool_events))
        tool_wire = json.loads(post.calls[2]["payload"])
        restored_tool = next(item for item in tool_wire["input"] if item.get("type") == "function_call")
        self.assertEqual(restored_tool["encrypted_function_args"], "opaque-tool-arguments")
        self.assertEqual(restored_tool["signature"], "synthetic-tool-signature")

        full_history = [
            *tool_history,
            {"type": "message", "role": "assistant", "content": "tool complete"},
        ]
        status, compact_events = send({
            "input": [*full_history, {"type": "compaction_trigger"}],
        })
        self.assertEqual(status, 200)
        compact_item = next(
            event["item"] for event in compact_events
            if event["type"] == "response.output_item.done"
        )
        self.assertEqual(compact_item["type"], "compaction")
        compact_wire = json.loads(post.calls[3]["payload"])
        self.assertEqual(
            compact_wire["input"][-1]["content"][0]["text"],
            SUMMARIZATION_PROMPT,
        )

        status, after_compact_events = send({
            "input": [
                compact_item,
                {"type": "message", "role": "user", "content": "continue"},
            ]
        })
        self.assertEqual(status, 200)
        self.assertTrue(any(event["type"] == "response.completed" for event in after_compact_events))
        post_compact_wire = json.loads(post.calls[4]["payload"])
        self.assertNotIn("opaque-first-state", json.dumps(post_compact_wire))
        self.assertIn("alpha and its tool result are retained", json.dumps(post_compact_wire))



if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import queue
import threading
import unittest
from uuid import uuid4

from model_deck.engine.routing.ports import RouteSnapshot, ExecutionMode
from model_deck.engine.runs.ports import (NormalizedRunInput, RunRequest, ToolDefinition,
    ProviderExecutionPort, ProviderRunHandle, ProviderCancelTerminationStatus)
from model_deck.plugins.provider_proxy import ExternalProviderExecution, ProviderProxyError
from model_deck.plugins.process_runtime.channel import ProviderMethod
from model_deck_contracts import validate_schema_ref

NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)


def request(*, endpoint="11111111-2222-4333-8444-555555555555", tools=()):
    return RunRequest(str(uuid4()), str(uuid4()), "client", "idem",
        RouteSnapshot(str(uuid4()), 1, str(uuid4()), 2, "org.example.provider", "model",
                      ExecutionMode.CUSTOM, endpoint_config_ref=endpoint, credential_ref="secret-ref"),
        NormalizedRunInput(({"role": "user", "content": "hi"},)), tools)


def event(req, handle, sequence, kind="run.started", **payload):
    return {"adapter_run_handle": handle, "sequence": sequence,
            "event": {"kind": kind, "run_id": req.run_id, "session_id": req.session_id,
                      "sequence": sequence, "event_schema_version": 1,
                      "observed_at": NOW.isoformat(), **payload}}


class Sink:
    def __init__(self):
        self.events = []
        self.changed = threading.Condition()

    def publish_provider_event(self, event):
        with self.changed:
            self.events.append(event)
            self.changed.notify_all()

    def wait(self, count):
        with self.changed:
            if not self.changed.wait_for(lambda: len(self.events) >= count, timeout=3):
                raise AssertionError(f"expected {count} events, got {self.events}")
        return self.events


class Channel:
    activation_id = "activation-one"

    def __init__(self):
        self.events = queue.Queue()
        self.calls = []
        self.on_start = None
        self.cancel_result = {"accepted": True}
        self.tool_result = {"accepted": True}
        self.start_result = None

    def request(self, method, params, *, timeout_s=None):
        name = method.value.rsplit(".", 1)[-1]
        validate_schema_ref(f"contracts/plugin.v1/provider/{name}.params.schema.json", params)
        self.calls.append((method, params, timeout_s))
        if method == ProviderMethod.START:
            if self.on_start:
                self.on_start(params)
            return self.start_result or {"adapter_run_handle": params["run_request"]["run_id"]}
        if method == ProviderMethod.CANCEL:
            return self.cancel_result
        if method == ProviderMethod.SUBMIT_TOOL_RESULT:
            return self.tool_result
        if method == ProviderMethod.ACK:
            return {"credit": 999999999}
        raise AssertionError(method)

    def receive_event(self, *, timeout_s):
        try:
            value = self.events.get(timeout=timeout_s)
        except queue.Empty:
            return None
        if isinstance(value, Exception):
            raise value
        return value


class ProviderProxyTests(unittest.TestCase):
    def proxy(self, channel=None):
        channel = channel or Channel()
        proxy = ExternalProviderExecution(channel, provider_id="org.example.provider", clock=lambda: NOW)
        self.addCleanup(proxy.close)
        return proxy, channel

    def test_slow_sink_does_not_block_other_run_or_close_and_ack_waits(self):
        proxy, channel = self.proxy()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        class SlowSink(Sink):
            def publish_provider_event(self, value):
                if value.kind == "run.started":
                    entered.set()
                    release.wait(3)
                super().publish_provider_event(value)
        first, second = request(), request()
        slow, other = SlowSink(), Sink()
        proxy.start(first, slow)
        channel.events.put(event(first, first.run_id, 0))
        self.assertTrue(entered.wait(1))
        try:
            with ThreadPoolExecutor(2) as pool:
                pool.submit(proxy.start, second, other).result(1)
                channel.events.put(event(second, second.run_id, 0))
                self.assertEqual(other.wait(1)[0].kind, "run.started")
                self.assertFalse(any(method == ProviderMethod.ACK and params["adapter_run_handle"] == first.run_id
                                     for method, params, _ in channel.calls))
                pool.submit(proxy.close).result(1)
                self.assertEqual(other.wait(2)[-1].kind, "run.interrupted")
        finally:
            release.set()
        self.assertEqual([item.kind for item in slow.wait(2)], ["run.started", "run.interrupted"])

    def test_terminal_callback_reentrant_close_has_exactly_one_terminal(self):
        proxy, channel = self.proxy()
        class ClosingSink(Sink):
            def publish_provider_event(self, value):
                if value.kind == "run.completed":
                    proxy.close()
                super().publish_provider_event(value)
        req, sink = request(), ClosingSink()
        proxy.start(req, sink)
        channel.events.put(event(req, req.run_id, 0, "run.completed", terminal_result={"outcome": "completed"}))
        self.assertEqual([item.kind for item in sink.wait(1)], ["run.completed"])
        proxy.close()
        self.assertEqual([item.kind for item in sink.events], ["run.completed"])

    def test_tool_sink_can_submit_result_reentrantly(self):
        proxy, channel = self.proxy()
        class ToolSink(Sink):
            def publish_provider_event(self, value):
                if value.kind == "tool.requested":
                    outcome = handle.submit_tool_result("call", {"ok": True})
                    self.accepted = outcome.outcome.value
                super().publish_provider_event(value)
        req = request(tools=(ToolDefinition("lookup", {}, True),))
        sink = ToolSink()
        handle = proxy.start(req, sink)
        channel.events.put(event(req, req.run_id, 0, "tool.requested",
                                 tool_call={"call_id": "call", "tool_name": "lookup", "arguments": {}}))
        sink.wait(1)
        self.assertEqual(sink.accepted, "accepted")
        channel.events.put(event(req, req.run_id, 1, "run.completed", terminal_result={"outcome": "completed"}))
        self.assertEqual(sink.wait(2)[-1].kind, "run.completed")

    def test_blocked_tool_channel_does_not_hold_global_lock(self):
        proxy, channel = self.proxy()
        req = request(tools=(ToolDefinition("lookup", {}, True),))
        sink = Sink()
        handle = proxy.start(req, sink)
        channel.events.put(event(req, req.run_id, 0, "tool.requested",
                                 tool_call={"call_id": "call", "tool_name": "lookup", "arguments": {}}))
        sink.wait(1)
        entered, release = threading.Event(), threading.Event()
        original = channel.request
        def blocked(method, params, *, timeout_s=None):
            if method == ProviderMethod.SUBMIT_TOOL_RESULT:
                entered.set()
                release.wait(3)
            return original(method, params, timeout_s=timeout_s)
        channel.request = blocked
        with ThreadPoolExecutor(2) as pool:
            forwarding = pool.submit(handle.submit_tool_result, "call", {})
            self.assertTrue(entered.wait(1))
            with self.assertRaisesRegex(ProviderProxyError, "tool_not_outstanding"):
                handle.submit_tool_result("call", {})
            try:
                pool.submit(proxy.start, request(), Sink()).result(1)
                pool.submit(proxy.close).result(1)
            finally:
                release.set()
            with self.assertRaises(ProviderProxyError):
                forwarding.result(1)
        self.assertEqual(sink.wait(2)[-1].kind, "run.interrupted")

    def test_cancel_budget_includes_lock_and_rechecks_clock(self):
        proxy, channel = self.proxy()
        handle = proxy.start(request(), Sink())
        proxy._lock.acquire()
        try:
            with ThreadPoolExecutor(1) as pool:
                result = pool.submit(handle.request_cancel, deadline=(NOW + timedelta(milliseconds=30)).isoformat()).result(1)
            self.assertFalse(result.request_accepted)
        finally:
            proxy._lock.release()
        ticks = iter((NOW, NOW + timedelta(seconds=1)))
        proxy._clock = lambda: next(ticks, NOW + timedelta(seconds=1))
        result = handle.request_cancel(deadline=(NOW + timedelta(milliseconds=30)).isoformat())
        self.assertFalse(result.request_accepted)
        self.assertFalse(any(method == ProviderMethod.CANCEL for method, _, _ in channel.calls))

    def test_real_coordinator_sqlite_tool_wait_result_replay_and_completion(self):
        import dataclasses
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from model_deck.adapters.storage.sqlite_session_run_repository import SQLiteSessionRunRepository
        from model_deck.engine.sessions.ports import CreateSessionCommand
        from model_deck.engine.runs.ports import (
            StartRunCommand, RunAdmissionKey, ClaimDispatchCommand, GetRunCommand, RunState,
        )
        from model_deck.engine.runs.use_cases import RunApplicationCoordinator, SubmitToolResultUseCase

        class Publications(Sink):
            def publish_application_event(self, value):
                self.publish_provider_event(value)

        with TemporaryDirectory() as directory:
            repository = SQLiteSessionRunRepository(Path(directory) / "runs.sqlite3",
                                                     utc_clock=lambda: NOW.isoformat())
            req = request(tools=(ToolDefinition("lookup", {"type": "object"}, True),))
            session = repository.create(CreateSessionCommand(req.route_snapshot.registration_id, "ref:fixture.host"))
            admitted = repository.admit(StartRunCommand(
                admission_key=RunAdmissionKey("fixture-principal", "engine.v1.runs.start", "start-1"),
                request_hash="fixture-hash", session_id=session.session_id,
                client_request_id=req.client_request_id, registration_id=req.route_snapshot.registration_id,
                route_snapshot=req.route_snapshot, input=req.input, tools=req.tools,
                authorized_host_context_ref="ref:fixture.host"))
            req = dataclasses.replace(req, run_id=admitted.run.run_id, session_id=session.session_id)
            repository.claim_dispatch(ClaimDispatchCommand(req.run_id, "fixture-dispatch"))
            publications = Publications()
            coordinator = RunApplicationCoordinator(repository, event_publisher=publications)
            proxy, channel = self.proxy()
            try:
                handle = proxy.start(req, coordinator.provider_sink())
                coordinator.register_handle(req.run_id, handle)
                channel.events.put(event(req, req.run_id, 0))
                self.assertEqual(publications.wait(1)[0].kind, "run.started")
                channel.events.put(event(req, req.run_id, 1, "tool.requested",
                    tool_call={"call_id": "call-1", "tool_name": "lookup", "arguments": {"q": "fixture"}}))
                self.assertEqual(publications.wait(2)[1].payload,
                                 {"call_id": "call-1", "tool_name": "lookup", "arguments": {"q": "fixture"}})
                self.assertEqual(repository.get(GetRunCommand(req.run_id)).state, RunState.WAITING_FOR_TOOL)
                submit = SubmitToolResultUseCase(repository, coordinator)
                params = {"run_id": req.run_id, "call_id": "call-1", "result": {"answer": "found"},
                          "idempotency_key": "result-1"}
                first = submit.execute(params, principal_id="fixture-principal",
                                       authorized_host_context_ref="ref:fixture.host")
                replay = submit.execute(params, principal_id="fixture-principal",
                                        authorized_host_context_ref="ref:fixture.host")
                self.assertEqual(replay, first)
                self.assertEqual(repository.get(GetRunCommand(req.run_id)).state, RunState.RUNNING)
                forwarded = [params for method, params, _ in channel.calls if method == ProviderMethod.SUBMIT_TOOL_RESULT]
                self.assertEqual(forwarded, [{"adapter_run_handle": req.run_id, "call_id": "call-1", "result": {"answer": "found"}}])
                channel.events.put(event(req, req.run_id, 2, "run.completed", terminal_result={"outcome": "completed"}))
                self.assertEqual(publications.wait(3)[-1].kind, "run.completed")
                self.assertEqual(repository.get(GetRunCommand(req.run_id)).state, RunState.COMPLETED)
                self.assertEqual([item.kind for item in publications.events],
                                 ["run.started", "tool.requested", "run.completed"])
            finally:
                proxy.close()

    def test_wire_serialization_and_public_protocols(self):
        proxy, channel = self.proxy()
        req = request(tools=(ToolDefinition("lookup", {"type": "object"}, True, "Look up"),))
        handle = proxy.start(req, Sink())
        self.assertIsInstance(proxy, ProviderExecutionPort)
        self.assertIsInstance(handle, ProviderRunHandle)
        params = channel.calls[0][1]
        self.assertEqual(params["run_request"]["tools"], [{"name": "lookup", "input_schema": {"type": "object"},
                            "host_execution_required": True, "description": "Look up"}])
        self.assertNotIn("credential_ref", params["route_snapshot"])
        self.assertNotIn("secret-ref", repr(params))

    def test_missing_endpoint_ref_rejected_before_dispatch(self):
        proxy, channel = self.proxy()
        with self.assertRaisesRegex(ProviderProxyError, "unsupported_capability"):
            proxy.start(request(endpoint=None), Sink())
        self.assertEqual(channel.calls, [])

    def test_concurrent_starts_do_not_lose_pre_response_events(self):
        proxy, channel = self.proxy()
        first, second = request(), request()
        by_id = {req.run_id: req for req in (first, second)}
        barrier = threading.Barrier(2)
        def start(params):
            req = by_id[params["run_request"]["run_id"]]
            channel.events.put(event(req, req.run_id, 0))
            barrier.wait(timeout=2)
        channel.on_start = start
        sinks = [Sink(), Sink()]
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(proxy.start, req, sink) for req, sink in zip((first, second), sinks)]
            for future in futures:
                future.result(timeout=3)
        for req, sink in zip((first, second), sinks):
            self.assertEqual(sink.wait(1)[0].run_id, req.run_id)
            channel.events.put(event(req, req.run_id, 1, "run.completed", terminal_result={"outcome": "completed"}))
            self.assertEqual(sink.wait(2)[1].kind, "run.completed")

    def test_tools_validate_outstanding_and_rejected_result_can_be_resubmitted(self):
        proxy, channel = self.proxy()
        req = request(tools=(ToolDefinition("lookup", {}, True),))
        sink = Sink()
        handle = proxy.start(req, sink)
        with self.assertRaisesRegex(ProviderProxyError, "tool_not_outstanding"):
            handle.submit_tool_result("foreign", {})
        channel.events.put(event(req, req.run_id, 0, "tool.requested",
                                 tool_call={"call_id": "call", "tool_name": "lookup", "arguments": {}}))
        sink.wait(1)
        channel.tool_result = {"accepted": False}
        self.assertEqual(handle.submit_tool_result("call", {}).outcome.value, "rejected")
        channel.tool_result = {"accepted": True}
        self.assertEqual(handle.submit_tool_result("call", {}).outcome.value, "accepted")
        with self.assertRaisesRegex(ProviderProxyError, "tool_not_outstanding"):
            handle.submit_tool_result("call", {})
        channel.events.put(event(req, req.run_id, 1, "run.completed", terminal_result={"outcome": "completed"}))
        self.assertEqual(sink.wait(2)[-1].kind, "run.completed")

    def test_cancel_status_and_deadline(self):
        proxy, channel = self.proxy()
        handle = proxy.start(request(), Sink())
        for result, status in [({"accepted": False}, "unknown"),
                               ({"accepted": True, "confirmed": False}, "unconfirmed"),
                               ({"accepted": True, "confirmed": True}, "confirmed")]:
            channel.cancel_result = result
            outcome = handle.request_cancel(deadline=(NOW + timedelta(milliseconds=250)).isoformat())
            self.assertEqual(outcome.request_accepted, result["accepted"])
            self.assertEqual(outcome.termination_status.value, status)
            self.assertEqual(channel.calls[-1][1]["deadline_ms"], 250)
            self.assertEqual(channel.calls[-1][2], .25)
        before = len(channel.calls)
        self.assertEqual(handle.request_cancel(deadline=NOW.isoformat()).termination_status,
                         ProviderCancelTerminationStatus.UNKNOWN)
        self.assertEqual(len(channel.calls), before)
        handle.request_cancel(deadline=(NOW + timedelta(days=1)).isoformat())
        self.assertEqual(channel.calls[-1][1]["deadline_ms"], 60000)

    def test_bad_event_interrupts_all_owned_runs_once(self):
        for defect in ("foreign_session", "foreign_run", "foreign_handle", "accepted", "gap", "inner_gap", "terminal", "schema"):
            with self.subTest(defect=defect):
                proxy, channel = self.proxy()
                req, other = request(), request()
                sink, other_sink = Sink(), Sink()
                proxy.start(req, sink)
                proxy.start(other, other_sink)
                value = event(req, req.run_id, 0)
                if defect == "foreign_session": value["event"]["session_id"] = other.session_id
                if defect == "foreign_run": value["event"]["run_id"] = str(uuid4())
                if defect == "foreign_handle": value["adapter_run_handle"] = other.run_id
                if defect == "accepted": value["event"]["kind"] = "run.accepted"
                if defect == "gap": value["sequence"] = value["event"]["sequence"] = 1
                if defect == "inner_gap": value["event"]["sequence"] = 1
                if defect == "terminal": value = event(req, req.run_id, 0, "run.completed", terminal_result={"outcome": "failed"})
                if defect == "schema": value["event"]["extra"] = True
                channel.events.put(value)
                self.assertEqual(sink.wait(1)[-1].kind, "run.interrupted")
                self.assertEqual(other_sink.wait(1)[-1].kind, "run.interrupted")
                proxy.close()
                self.assertEqual(len(sink.events), 1)
                self.assertFalse(any(call[0] == ProviderMethod.ACK for call in channel.calls))

    def test_duplicate_and_post_terminal_event_never_republished(self):
        for terminal in (False, True):
            proxy, channel = self.proxy()
            req, sink = request(), Sink()
            proxy.start(req, sink)
            value = (event(req, req.run_id, 0, "run.completed", terminal_result={"outcome": "completed"})
                     if terminal else event(req, req.run_id, 0))
            channel.events.put(value)
            sink.wait(1)
            channel.events.put(value)
            if not terminal:
                self.assertEqual(sink.wait(2)[-1].kind, "run.interrupted")
            else:
                # Wait for the malformed event to be consumed using a bounded join.
                proxy._pump.join(timeout=2)
                self.assertFalse(proxy._pump.is_alive())
                self.assertEqual(len(sink.events), 1)

    def test_completion_with_unresolved_tool_is_interrupted(self):
        proxy, channel = self.proxy()
        req, sink = request(tools=(ToolDefinition("lookup", {}, True),)), Sink()
        proxy.start(req, sink)
        channel.events.put(event(req, req.run_id, 0, "tool.requested",
                                 tool_call={"call_id": "x", "tool_name": "lookup", "arguments": {}}))
        channel.events.put(event(req, req.run_id, 1, "run.completed", terminal_result={"outcome": "completed"}))
        self.assertEqual([item.kind for item in sink.wait(2)], ["tool.requested", "run.interrupted"])

    def test_channel_failure_and_close_have_no_process_side_effect(self):
        proxy, channel = self.proxy()
        req, sink = request(), Sink()
        proxy.start(req, sink)
        channel.events.put(RuntimeError("sensitive worker error"))
        self.assertEqual(sink.wait(1)[0].payload, {"terminal_result": {"outcome": "interrupted"}})
        proxy.close()
        self.assertEqual(len(sink.events), 1)
        self.assertEqual([call[0] for call in channel.calls], [ProviderMethod.START])
        with self.assertRaises(ProviderProxyError):
            proxy.start(request(), Sink())

    def test_early_event_window_is_bounded(self):
        proxy, channel = self.proxy()
        req, sink = request(), Sink()
        def start(params):
            for sequence in range(257):
                channel.events.put(event(req, req.run_id, sequence))
            sink.wait(1)
        channel.on_start = start
        with self.assertRaises(ProviderProxyError):
            proxy.start(req, sink)
        self.assertEqual(sink.events[0].kind, "run.interrupted")
        self.assertEqual(len(channel.calls), 1)

    def test_pre_response_byte_limit_and_wrong_handle(self):
        for defect in ("bytes", "handle"):
            with self.subTest(defect=defect):
                proxy, channel = self.proxy()
                req, sink = request(), Sink()
                def start(params):
                    if defect == "bytes":
                        for sequence in range(17):
                            channel.events.put(event(req, req.run_id, sequence, "content.delta",
                                                     delta="x" * 65536, channel="text"))
                        sink.wait(1)
                    else:
                        channel.events.put(event(req, str(uuid4()), 0))
                        # Synchronize on the pump's receipt rather than sleeping.
                        deadline = threading.Event()
                        original = channel.receive_event
                        def receive(*, timeout_s):
                            deadline.set()
                            return original(timeout_s=timeout_s)
                        channel.receive_event = receive
                        self.assertTrue(deadline.wait(2))
                channel.on_start = start
                with self.assertRaises(ProviderProxyError):
                    proxy.start(req, sink)
                self.assertEqual(sink.wait(1)[0].kind, "run.interrupted")

    def test_activation_change_interrupts_and_cannot_be_rebound(self):
        proxy, channel = self.proxy()
        req, sink = request(), Sink()
        proxy.start(req, sink)
        channel.activation_id = "another-activation"
        self.assertEqual(sink.wait(1)[0].kind, "run.interrupted")
        with self.assertRaises(ProviderProxyError):
            proxy.start(request(), Sink())

    def test_invalid_start_response_interrupts_and_never_retries(self):
        proxy, channel = self.proxy()
        channel.start_result = {"adapter_run_handle": 17}
        sink = Sink()
        with self.assertRaises(ProviderProxyError):
            proxy.start(request(), sink)
        self.assertEqual(sink.wait(1)[0].kind, "run.interrupted")
        self.assertEqual(len(channel.calls), 1)


if __name__ == "__main__":
    unittest.main()

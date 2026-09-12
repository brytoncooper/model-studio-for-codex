import copy
import unittest
from typing import Any

from model_deck.engine.routing.ports import (
    CapabilityFeature,
    CapabilityTriState,
    ExecutionMode,
    RouteSnapshot,
)
from model_deck.engine.runs.ports import (
    CancelProviderRunResult,
    NormalizedRunInput,
    ProviderCancelTerminationStatus,
    ProviderExecutionPort,
    ProviderRunEvent,
    ProviderRunEventSink,
    ProviderRunHandle,
    RunRequest,
    SubmitToolResultProviderOutcome,
    SubmitToolResultProviderResult,
    ToolCallDescriptor,
)
from model_deck.integrations.providers.cursor import (
    CURSOR_PROVIDER_ID,
    CursorCoordinatorClosedError,
    CursorDuplicateStartError,
    CursorExecutionCoordinator,
    CursorProtocolError,
    CursorSdkEvent,
    CursorStartRequest,
)

RUN_ID = "550e8400-e29b-41d4-a716-446655440011"
RUN_ID_2 = "550e8400-e29b-41d4-a716-446655440012"
SESSION_ID = "550e8400-e29b-41d4-a716-446655440013"
REGISTRATION_ID = "550e8400-e29b-41d4-a716-446655440014"
CONNECTION_ID = "550e8400-e29b-41d4-a716-446655440015"
NOW = "2026-09-12T12:00:00+00:00"
DEADLINE = "2026-09-12T12:05:00+00:00"


class RecordingSink(ProviderRunEventSink):
    def __init__(self) -> None:
        self.events: list[ProviderRunEvent] = []

    def publish_provider_event(self, event: ProviderRunEvent) -> None:
        self.events.append(event)

    def kinds(self) -> tuple[str, ...]:
        return tuple(event.kind for event in self.events)


class FakeCursorSession:
    def __init__(self) -> None:
        self.tool_submissions: list[tuple[str, Any]] = []
        self.cancel_requests: list[str] = []
        self.close_count = 0
        self.tool_outcome = SubmitToolResultProviderOutcome.ACCEPTED
        self.cancel_result = CancelProviderRunResult(
            request_accepted=True,
            termination_status=ProviderCancelTerminationStatus.UNCONFIRMED,
        )
        self.on_cancel_events: tuple[CursorSdkEvent, ...] = ()
        self._on_event = None

    def attach(self, on_event: Any) -> None:
        self._on_event = on_event

    def emit(self, event: CursorSdkEvent) -> None:
        self._on_event(event)

    def submit_tool_result(
        self, call_id: str, result: Any
    ) -> SubmitToolResultProviderResult:
        self.tool_submissions.append((call_id, copy.deepcopy(result)))
        return SubmitToolResultProviderResult(outcome=self.tool_outcome)

    def request_cancel(self, *, deadline: str) -> CancelProviderRunResult:
        self.cancel_requests.append(deadline)
        for event in self.on_cancel_events:
            self._on_event(event)
        return self.cancel_result

    def close(self) -> None:
        self.close_count += 1


class FakeCursorRuntime:
    def __init__(self) -> None:
        self.starts: list[tuple[CursorStartRequest, Any]] = []
        self.sessions: list[FakeCursorSession] = []
        self.start_events: tuple[CursorSdkEvent, ...] = ()
        self.start_error: Exception | None = None

    def start(self, request: CursorStartRequest, on_event: Any) -> FakeCursorSession:
        self.starts.append((request, on_event))
        if self.start_error is not None:
            raise self.start_error
        session = FakeCursorSession()
        session.attach(on_event)
        self.sessions.append(session)
        for event in self.start_events:
            on_event(event)
        return session


def _route_snapshot(**overrides: Any) -> RouteSnapshot:
    base: dict[str, Any] = {
        "registration_id": REGISTRATION_ID,
        "registration_revision": 1,
        "connection_id": CONNECTION_ID,
        "connection_revision": 1,
        "provider_id": CURSOR_PROVIDER_ID,
        "provider_model_id": "cursor/trial-model",
        "execution_mode": ExecutionMode.CUSTOM,
        "endpoint_config_ref": "ref:endpoint.config",
        "credential_ref": "ref:credential.token",
        "capability_snapshot_ref": "ref:cap.snapshot",
        "capability_features": (
            CapabilityFeature("tools", CapabilityTriState.SUPPORTED),
        ),
    }
    base.update(overrides)
    return RouteSnapshot(**base)


def _run_request(**overrides: Any) -> RunRequest:
    base: dict[str, Any] = {
        "run_id": RUN_ID,
        "session_id": SESSION_ID,
        "client_request_id": "client-1",
        "idempotency_key": "idem-1",
        "route_snapshot": _route_snapshot(),
        "input": NormalizedRunInput(messages=("hello", {"role": "user"})),
        "tools": (ToolCallDescriptor(call_id="call-1", tool_name="search"),),
    }
    base.update(overrides)
    return RunRequest(**base)


def _tool_event(
    call_id: str = "call-1",
    tool_name: str = "search",
    arguments: Any = None,
) -> CursorSdkEvent:
    tool_call: dict[str, Any] = {
        "call_id": call_id,
        "tool_name": tool_name,
        "arguments": {} if arguments is None else arguments,
    }
    return CursorSdkEvent(
        kind="tool.requested",
        payload={"tool_call": tool_call},
    )


def _terminal_event(kind: str = "run.completed") -> CursorSdkEvent:
    outcome = kind.split(".", 1)[1]
    return CursorSdkEvent(
        kind=kind,
        payload={"terminal_result": {"outcome": outcome}},
    )


class CoordinatorStartTest(unittest.TestCase):
    def test_structural_conformance(self) -> None:
        coordinator = CursorExecutionCoordinator(FakeCursorRuntime(), now=lambda: NOW)
        self.assertIsInstance(coordinator, ProviderExecutionPort)
        handle = coordinator.start(_run_request(), RecordingSink())
        self.assertIsInstance(handle, ProviderRunHandle)

    def test_request_mapping_and_detachment(self) -> None:
        runtime = FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(runtime, now=lambda: NOW)
        request = _run_request()
        coordinator.start(request, RecordingSink())
        adapter_request, _ = runtime.starts[0]
        self.assertEqual(adapter_request.run_id, RUN_ID)
        self.assertEqual(adapter_request.session_id, SESSION_ID)
        self.assertEqual(adapter_request.connection_id, CONNECTION_ID)
        self.assertEqual(adapter_request.provider_model_id, "cursor/trial-model")
        self.assertEqual(adapter_request.input_messages, ("hello", {"role": "user"}))
        self.assertEqual(
            adapter_request.tools,
            (ToolCallDescriptor(call_id="call-1", tool_name="search"),),
        )
        self.assertIsNone(adapter_request.continuation_handle)
        self.assertIsInstance(adapter_request, CursorStartRequest)

    def test_start_failure_cleans_up_registration(self) -> None:
        runtime = FakeCursorRuntime()
        runtime.start_error = RuntimeError("sdk unavailable")
        coordinator = CursorExecutionCoordinator(runtime, now=lambda: NOW)
        with self.assertRaises(RuntimeError):
            coordinator.start(_run_request(), RecordingSink())
        self.assertEqual(coordinator.open_run_ids, ())
        self.assertIsNone(coordinator.get_handle(RUN_ID))
        runtime.start_error = None
        handle = coordinator.start(_run_request(), RecordingSink())
        self.assertIsInstance(handle, ProviderRunHandle)
        self.assertEqual(len(runtime.starts), 2)

    def test_duplicate_start_never_redispatches(self) -> None:
        runtime = FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(runtime, now=lambda: NOW)
        sink = RecordingSink()
        coordinator.start(_run_request(), sink)
        with self.assertRaises(CursorDuplicateStartError):
            coordinator.start(_run_request(), sink)
        self.assertEqual(len(runtime.starts), 1)

    def test_synchronous_start_callbacks_are_safe(self) -> None:
        runtime = FakeCursorRuntime()
        runtime.start_events = (
            CursorSdkEvent(kind="run.started"),
            CursorSdkEvent(kind="content.delta", payload={"channel": "text", "delta": "hi"}),
        )
        coordinator = CursorExecutionCoordinator(runtime, now=lambda: NOW)
        sink = RecordingSink()
        coordinator.start(_run_request(), sink)
        self.assertEqual(sink.kinds(), ("run.started", "content.delta"))
        for event in sink.events:
            self.assertEqual(event.run_id, RUN_ID)
            self.assertEqual(event.observed_at, NOW)

    def test_synchronous_terminal_defers_close_until_bind(self) -> None:
        runtime = FakeCursorRuntime()
        runtime.start_events = (
            CursorSdkEvent(kind="run.started"),
            _terminal_event("run.completed"),
        )
        coordinator = CursorExecutionCoordinator(runtime, now=lambda: NOW)
        sink = RecordingSink()
        handle = coordinator.start(_run_request(), sink)
        self.assertEqual(sink.kinds(), ("run.started", "run.completed"))
        self.assertTrue(handle.is_terminal)
        self.assertEqual(runtime.sessions[0].close_count, 1)


class EventLifecycleTest(unittest.TestCase):
    def _started(self, runtime: FakeCursorRuntime | None = None) -> tuple[Any, Any, Any]:
        runtime = runtime or FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(runtime, now=lambda: NOW)
        sink = RecordingSink()
        handle = coordinator.start(_run_request(), sink)
        _, on_event = runtime.starts[0]
        on_event(CursorSdkEvent(kind="run.started"))
        return coordinator, sink, on_event

    def test_first_event_must_be_started(self) -> None:
        runtime = FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(runtime, now=lambda: NOW)
        sink = RecordingSink()
        coordinator.start(_run_request(), sink)
        _, on_event = runtime.starts[0]
        with self.assertRaises(CursorProtocolError):
            on_event(CursorSdkEvent(kind="content.delta", payload={"channel": "text", "delta": "x"}))
        self.assertEqual(sink.events, [])

    def test_duplicate_started_rejected(self) -> None:
        _, sink, on_event = self._started()
        with self.assertRaises(CursorProtocolError):
            on_event(CursorSdkEvent(kind="run.started"))
        self.assertEqual(sink.kinds(), ("run.started",))

    def test_unknown_kind_rejected_before_sink(self) -> None:
        _, sink, on_event = self._started()
        with self.assertRaises(CursorProtocolError):
            on_event(CursorSdkEvent(kind="run.bogus"))
        self.assertEqual(sink.kinds(), ("run.started",))

    def test_malformed_tool_request_rejected(self) -> None:
        _, sink, on_event = self._started()
        with self.assertRaises(CursorProtocolError):
            on_event(CursorSdkEvent(kind="tool.requested", payload={}))
        with self.assertRaises(CursorProtocolError):
            on_event(
                CursorSdkEvent(
                    kind="tool.requested",
                    payload={"tool_call": {"call_id": "", "tool_name": "x"}},
                )
            )
        with self.assertRaises(CursorProtocolError):
            on_event(CursorSdkEvent(kind="tool.requested", payload=None))
        self.assertEqual(sink.kinds(), ("run.started",))

    def test_non_serializable_payload_rejected(self) -> None:
        _, sink, on_event = self._started()
        with self.assertRaises(CursorProtocolError):
            on_event(CursorSdkEvent(kind="content.delta", payload={"v": object()}))
        self.assertEqual(sink.kinds(), ("run.started",))

    def test_terminal_closes_session_exactly_once(self) -> None:
        runtime = FakeCursorRuntime()
        _, sink, on_event = self._started(runtime)
        on_event(_terminal_event("run.completed"))
        self.assertEqual(sink.kinds(), ("run.started", "run.completed"))
        self.assertEqual(runtime.sessions[0].close_count, 1)
        with self.assertRaises(CursorProtocolError):
            on_event(CursorSdkEvent(kind="content.delta", payload={"channel": "text", "delta": "late"}))
        self.assertEqual(runtime.sessions[0].close_count, 1)
        self.assertEqual(sink.kinds(), ("run.started", "run.completed"))

    def test_duplicate_tool_request_while_outstanding_rejected(self) -> None:
        _, sink, on_event = self._started()
        on_event(_tool_event())
        with self.assertRaises(CursorProtocolError):
            on_event(_tool_event())
        with self.assertRaises(CursorProtocolError):
            on_event(_tool_event(call_id="call-2"))
        self.assertEqual(sink.kinds(), ("run.started", "tool.requested"))

    def test_completed_terminal_while_outstanding_rejected(self) -> None:
        _, sink, on_event = self._started()
        on_event(_tool_event())
        with self.assertRaises(CursorProtocolError):
            on_event(_terminal_event("run.completed"))
        self.assertEqual(sink.kinds(), ("run.started", "tool.requested"))

    def test_payload_detachment(self) -> None:
        _, sink, on_event = self._started()
        payload = {"channel": "text", "delta": "abc"}
        on_event(CursorSdkEvent(kind="content.delta", payload=payload))
        payload["delta"] = "mutated"
        self.assertEqual(sink.events[1].payload, {"channel": "text", "delta": "abc"})


class ToolResultTest(unittest.TestCase):
    def _suspended(self) -> tuple[Any, Any, Any, Any]:
        runtime = FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(runtime, now=lambda: NOW)
        sink = RecordingSink()
        handle = coordinator.start(_run_request(), sink)
        _, on_event = runtime.starts[0]
        on_event(CursorSdkEvent(kind="run.started"))
        on_event(_tool_event())
        return coordinator, sink, handle, runtime.sessions[0]

    def test_wrong_call_rejected(self) -> None:
        _, _, handle, session = self._suspended()
        result = handle.submit_tool_result("call-9", {"ok": True})
        self.assertEqual(result.outcome, SubmitToolResultProviderOutcome.REJECTED)
        self.assertEqual(session.tool_submissions, [])
        self.assertEqual(handle.outstanding_call_id, "call-1")

    def test_accepted_then_exact_replay_without_second_sdk_call(self) -> None:
        _, _, handle, session = self._suspended()
        first = handle.submit_tool_result("call-1", {"ok": True})
        self.assertEqual(first.outcome, SubmitToolResultProviderOutcome.ACCEPTED)
        self.assertIsNone(handle.outstanding_call_id)
        replay = handle.submit_tool_result("call-1", {"ok": True})
        self.assertEqual(replay.outcome, SubmitToolResultProviderOutcome.ACCEPTED)
        self.assertEqual(len(session.tool_submissions), 1)

    def test_changed_replay_rejected(self) -> None:
        _, _, handle, session = self._suspended()
        handle.submit_tool_result("call-1", {"ok": True})
        result = handle.submit_tool_result("call-1", {"ok": False})
        self.assertEqual(result.outcome, SubmitToolResultProviderOutcome.REJECTED)
        self.assertEqual(len(session.tool_submissions), 1)

    def test_receipt_survives_caller_mutation(self) -> None:
        _, _, handle, _ = self._suspended()
        result = {"items": [1, 2]}
        handle.submit_tool_result("call-1", result)
        result["items"].append(3)
        replay = handle.submit_tool_result("call-1", {"items": [1, 2]})
        self.assertEqual(replay.outcome, SubmitToolResultProviderOutcome.ACCEPTED)
        mismatch = handle.submit_tool_result("call-1", {"items": [1, 2, 3]})
        self.assertEqual(mismatch.outcome, SubmitToolResultProviderOutcome.REJECTED)

    def test_submit_rejected_without_sdk_call_after_terminal(self) -> None:
        runtime = FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(runtime, now=lambda: NOW)
        sink = RecordingSink()
        handle = coordinator.start(_run_request(), sink)
        _, on_event = runtime.starts[0]
        on_event(CursorSdkEvent(kind="run.started"))
        on_event(_terminal_event("run.failed"))
        result = handle.submit_tool_result("call-1", {"ok": True})
        self.assertEqual(result.outcome, SubmitToolResultProviderOutcome.REJECTED)
        self.assertEqual(runtime.sessions[0].tool_submissions, [])


class CancelTest(unittest.TestCase):
    def _running(self) -> tuple[Any, Any, Any, Any]:
        runtime = FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(runtime, now=lambda: NOW)
        sink = RecordingSink()
        handle = coordinator.start(_run_request(), sink)
        _, on_event = runtime.starts[0]
        on_event(CursorSdkEvent(kind="run.started"))
        return coordinator, sink, handle, runtime.sessions[0]

    def test_unconfirmed_cancel_delegates_without_terminal(self) -> None:
        _, sink, handle, session = self._running()
        result = handle.request_cancel(deadline=DEADLINE)
        self.assertTrue(result.request_accepted)
        self.assertEqual(
            result.termination_status, ProviderCancelTerminationStatus.UNCONFIRMED
        )
        self.assertEqual(session.cancel_requests, [DEADLINE])
        self.assertEqual(sink.kinds(), ("run.started",))
        self.assertEqual(session.close_count, 0)

    def test_confirmed_cancel_with_synchronous_terminal(self) -> None:
        _, sink, handle, session = self._running()
        session.cancel_result = CancelProviderRunResult(
            request_accepted=True,
            termination_status=ProviderCancelTerminationStatus.CONFIRMED,
        )
        session.on_cancel_events = (_terminal_event("run.cancelled"),)
        result = handle.request_cancel(deadline=DEADLINE)
        self.assertEqual(
            result.termination_status, ProviderCancelTerminationStatus.CONFIRMED
        )
        self.assertEqual(sink.kinds(), ("run.started", "run.cancelled"))
        self.assertTrue(handle.is_terminal)
        self.assertEqual(session.close_count, 1)

    def test_cancel_after_terminal_never_touches_sdk(self) -> None:
        _, _, handle, session = self._running()
        session.emit(_terminal_event("run.completed"))
        result = handle.request_cancel(deadline=DEADLINE)
        self.assertFalse(result.request_accepted)
        self.assertEqual(
            result.termination_status, ProviderCancelTerminationStatus.UNKNOWN
        )
        self.assertEqual(session.cancel_requests, [])


class ParallelIsolationTest(unittest.TestCase):
    def test_runs_stay_isolated(self) -> None:
        runtime = FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(runtime, now=lambda: NOW)
        sink_a = RecordingSink()
        sink_b = RecordingSink()
        handle_a = coordinator.start(_run_request(), sink_a)
        handle_b = coordinator.start(_run_request(run_id=RUN_ID_2), sink_b)
        _, on_event_a = runtime.starts[0]
        _, on_event_b = runtime.starts[1]
        on_event_a(CursorSdkEvent(kind="run.started"))
        on_event_b(CursorSdkEvent(kind="run.started"))
        on_event_a(_tool_event())
        handle_b.request_cancel(deadline=DEADLINE)
        self.assertEqual(runtime.sessions[0].cancel_requests, [])
        self.assertEqual(runtime.sessions[1].cancel_requests, [DEADLINE])
        self.assertEqual(handle_a.outstanding_call_id, "call-1")
        self.assertIsNone(handle_b.outstanding_call_id)
        handle_a.submit_tool_result("call-1", {"ok": True})
        on_event_a(_terminal_event("run.completed"))
        self.assertTrue(handle_a.is_terminal)
        self.assertFalse(handle_b.is_terminal)
        self.assertEqual(runtime.sessions[0].close_count, 1)
        self.assertEqual(runtime.sessions[1].close_count, 0)
        self.assertEqual(sink_b.kinds(), ("run.started",))

    def test_coordinator_close_and_closed_start(self) -> None:
        runtime = FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(runtime, now=lambda: NOW)
        coordinator.start(_run_request(), RecordingSink())
        coordinator.start(_run_request(run_id=RUN_ID_2), RecordingSink())
        coordinator.close()
        self.assertEqual(runtime.sessions[0].close_count, 1)
        self.assertEqual(runtime.sessions[1].close_count, 1)
        self.assertEqual(coordinator.open_run_ids, ())
        with self.assertRaises(CursorCoordinatorClosedError):
            coordinator.start(_run_request(run_id=RUN_ID), RecordingSink())


if __name__ == "__main__":
    unittest.main()



class BugFixRegressionTest(unittest.TestCase):
    """Direct regression tests for the five coordinator bugs the previous
    review identified, plus the four related fixes.  Each test names the
    bug it pins so future refactors cannot silently regress the
    behavior the bug list promised.

    Bugs:
      1. close reservation must happen under the coordinator lock so a
         concurrent close cannot race with a still-publishing terminal.
      2. per-run publication order must be preserved when SDK callbacks
         arrive on different threads.
      3. sink failure on a delta must not corrupt the in-progress state
         and must not prevent terminal publication / cleanup.
      4. close_session racing with runtime.start that is blocked must
         not lose the close intent: the returned session must be closed
         once after bind().
      5. terminal handles must not remain in open_run_ids forever, and
         must still be reachable via get_handle for a bounded replay
         window.

    Related fixes:
      a. SDK submit_tool_result exception must clear _forward_pending
         and report REJECTED so a retry can be attempted.
      b. SDK mutation of the forwarded result must not leak into the
         stored replay receipt.
      c. submit_tool_result result must be deep-copied before SDK call
         AND before storage so neither side can mutate the other.
      d. terminal error with unknown keys (including a stray top-level
         "error" key) must be rejected by the frozen schema parity check.
    """

    def test_per_run_publication_order_preserved_across_threads(self) -> None:
        # Bug 2: when the sink publish for a delta is blocked, a terminal
        # SDK callback arriving on another thread must not be able to
        # interleave. The terminal enqueues without waiting; the current
        # publisher delivers it only after the delta sink returns.
        delta_publish_started = threading.Event()
        release_delta_publish = threading.Event()

        class GatedSink(RecordingSink):
            def publish_provider_event(self, event: ProviderRunEvent) -> None:
                super().publish_provider_event(event)
                if event.kind == "content.delta":
                    delta_publish_started.set()
                    # Block the delta publish until the test releases it.
                    release_delta_publish.wait(timeout=10)

        runtime = FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(runtime, now=lambda: NOW)
        sink = GatedSink()
        coordinator.start(_run_request(), sink)
        _, on_event = runtime.starts[0]
        on_event(CursorSdkEvent(kind="run.started"))

        thread_errors: list[BaseException] = []

        def emit_delta() -> None:
            try:
                on_event(_delta_event())
            except BaseException as exc:  # noqa: BLE001
                thread_errors.append(exc)

        delta_thread = threading.Thread(target=emit_delta)
        delta_thread.start()
        self.assertTrue(delta_publish_started.wait(timeout=10))

        # While the delta is parked at the sink, the main thread tries to
        # enqueue the terminal. Its callback can return while delivery
        # remains ordered behind the delta.
        terminal_done = threading.Event()

        def emit_terminal() -> None:
            try:
                on_event(_terminal_event("run.completed"))
            finally:
                terminal_done.set()

        terminal_thread = threading.Thread(target=emit_terminal)
        terminal_thread.start()
        # Terminal enqueue returns without waiting on arbitrary sink code.
        self.assertTrue(terminal_done.wait(timeout=0.5))
        self.assertNotIn("run.completed", sink.kinds())

        # Release the delta publisher, which drains the queued terminal.
        release_delta_publish.set()
        delta_thread.join(timeout=10)
        terminal_thread.join(timeout=10)

        self.assertEqual(thread_errors, [])
        self.assertEqual(
            sink.kinds(),
            ("run.started", "content.delta", "run.completed"),
        )

    def test_delta_sink_failure_does_not_block_terminal_publication(self) -> None:
        # Bug 3: a delta publish failure must surface but must not stop
        # the SDK from later emitting a terminal that still needs to be
        # published and to close the session exactly once.
        class DeltaFailSink(RecordingSink):
            def __init__(self) -> None:
                super().__init__()
                self.failed_kinds: list[str] = []

            def publish_provider_event(self, event: ProviderRunEvent) -> None:
                if event.kind == "content.delta":
                    self.failed_kinds.append(event.kind)
                    raise RuntimeError("delta sink unavailable")
                super().publish_provider_event(event)

        runtime = FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(runtime, now=lambda: NOW)
        sink = DeltaFailSink()
        coordinator.start(_run_request(), sink)
        _, on_event = runtime.starts[0]
        on_event(CursorSdkEvent(kind="run.started"))
        session = runtime.sessions[0]
        with self.assertRaises(RuntimeError):
            on_event(
                CursorSdkEvent(
                    kind="content.delta",
                    payload={"channel": "text", "delta": "oops"},
                )
            )
        # Terminal still publishes, handle still closes exactly once.
        on_event(_terminal_event("run.completed"))
        self.assertEqual(sink.kinds(), ("run.started", "run.completed"))
        self.assertEqual(session.close_count, 1)
        self.assertEqual(sink.failed_kinds, ["content.delta"])

    def test_close_during_blocked_start_defers_until_bind(self) -> None:
        # Bug 4: runtime.start blocks; caller invokes close_session while
        # blocked; the returned session must be closed once after bind.
        blocked = threading.Event()
        proceed = threading.Event()

        class BlockingRuntime:
            def __init__(self) -> None:
                self.last_session = None

            def start(self, request: CursorStartRequest, on_event: Any) -> Any:
                blocked.set()
                proceed.wait(timeout=10)

                class _Session:
                    def __init__(self) -> None:
                        self.close_count = 0

                    def submit_tool_result(self, call_id: str, result: Any) -> SubmitToolResultProviderResult:
                        return SubmitToolResultProviderResult(
                            outcome=SubmitToolResultProviderOutcome.REJECTED
                        )

                    def request_cancel(self, *, deadline: str) -> CancelProviderRunResult:
                        return CancelProviderRunResult(
                            request_accepted=False,
                            termination_status=ProviderCancelTerminationStatus.UNKNOWN,
                        )

                    def close(self) -> None:
                        self.close_count += 1

                session = _Session()
                self.last_session = session
                return session

        runtime = BlockingRuntime()
        coordinator = CursorExecutionCoordinator(runtime, now=lambda: NOW)
        sink = RecordingSink()

        result: dict[str, BaseException | None] = {"err": None}

        def do_start() -> None:
            try:
                coordinator.start(_run_request(), sink)
            except BaseException as exc:  # noqa: BLE001
                result["err"] = exc

        t = threading.Thread(target=do_start)
        t.start()
        self.assertTrue(blocked.wait(timeout=10))
        # While runtime.start is blocked, get the handle and request close.
        handle = coordinator.get_handle(RUN_ID)
        assert handle is not None
        self.assertFalse(handle.is_terminal)
        handle.close_session()
        # Now let the runtime return.  bind() must honor the close intent
        # and call session.close() exactly once.
        proceed.set()
        t.join(timeout=10)
        self.assertIsNone(result["err"])
        assert runtime.last_session is not None
        self.assertEqual(runtime.last_session.close_count, 1)
        self.assertEqual(coordinator.open_run_ids, ())

    def test_terminal_handle_removed_from_open_run_ids_with_bounded_replay(self) -> None:
        # Bug 5: terminal event must move the handle out of the active
        # list, but get_handle must still resolve it within the replay
        # window.  After the window expires, get_handle returns None.
        clock = [0.0]

        def fake_monotonic() -> float:
            return clock[0]

        runtime = FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(
            runtime,
            now=lambda: NOW,
            monotonic=fake_monotonic,
            replay_retention_seconds=10.0,
        )
        coordinator.start(_run_request(), RecordingSink())
        _, on_event = runtime.starts[0]
        on_event(CursorSdkEvent(kind="run.started"))
        on_event(_terminal_event("run.completed"))
        # Active list cleared.
        self.assertEqual(coordinator.open_run_ids, ())
        # Replay window: handle still reachable.
        handle = coordinator.get_handle(RUN_ID)
        self.assertIsNotNone(handle)
        self.assertTrue(handle.is_terminal)
        # Advance past the replay window.
        clock[0] = 100.0
        self.assertIsNone(coordinator.get_handle(RUN_ID))
        self.assertEqual(coordinator.open_run_ids, ())

    def test_submit_tool_result_sdk_exception_clears_forward_pending(self) -> None:
        # Related fix (a): SDK raises during submit_tool_result; the
        # coordinator must clear _forward_pending and return REJECTED, and
        # a subsequent valid submission must succeed.
        class RaisingSession(FakeCursorSession):
            def __init__(self) -> None:
                super().__init__()
                self.raise_count = 0
                self.fail_next = True

            def submit_tool_result(
                self, call_id: str, result: Any
            ) -> SubmitToolResultProviderResult:
                if self.fail_next:
                    self.fail_next = False
                    self.raise_count += 1
                    raise RuntimeError("sdk transport lost")
                self.tool_submissions.append((call_id, copy.deepcopy(result)))
                return SubmitToolResultProviderResult(
                    outcome=SubmitToolResultProviderOutcome.ACCEPTED
                )

        class RaisingRuntime(FakeCursorRuntime):
            def start(self, request: CursorStartRequest, on_event: Any) -> Any:
                self.starts.append((request, on_event))
                session = RaisingSession()
                session.attach(on_event)
                self.sessions.append(session)
                for event in self.start_events:
                    on_event(event)
                return session

        runtime = RaisingRuntime()
        coordinator = CursorExecutionCoordinator(runtime, now=lambda: NOW)
        sink = RecordingSink()
        handle = coordinator.start(_run_request(), sink)
        _, on_event = runtime.starts[0]
        on_event(CursorSdkEvent(kind="run.started"))
        on_event(_tool_event())
        session = runtime.sessions[0]
        # First submission: SDK raises; expect REJECTED and pending cleared.
        first = handle.submit_tool_result("call-1", {"ok": True})
        self.assertEqual(first.outcome, SubmitToolResultProviderOutcome.REJECTED)
        # State must be clean enough for a retry to be accepted.
        second = handle.submit_tool_result("call-1", {"ok": True})
        self.assertEqual(second.outcome, SubmitToolResultProviderOutcome.ACCEPTED)
        # Only the retry actually reached the SDK; the raising attempt
        # raised before recording a submission.
        self.assertEqual(len(session.tool_submissions), 1)
        self.assertEqual(session.raise_count, 1)
        # And the outstanding call has been cleared by the accepted retry.
        self.assertIsNone(handle.outstanding_call_id)

    def test_receipt_isolated_from_sdk_mutation(self) -> None:
        # Related fix (b)(c): SDK mutates the forwarded result; the stored
        # receipt must remain the original value, so a future exact replay
        # still compares equal to the untouched snapshot.
        class MutatingSession(FakeCursorSession):
            def submit_tool_result(
                self, call_id: str, result: Any
            ) -> SubmitToolResultProviderResult:
                # Retain the SDK-side reference so the test can observe
                # the mutation the SDK applies to the forwarded value.
                self.tool_submissions.append((call_id, result))
                # Simulate an SDK that mutates the value it received.
                if isinstance(result, dict):
                    result["mutated"] = True
                return SubmitToolResultProviderResult(
                    outcome=SubmitToolResultProviderOutcome.ACCEPTED
                )

        class MutatingRuntime(FakeCursorRuntime):
            def start(self, request: CursorStartRequest, on_event: Any) -> Any:
                self.starts.append((request, on_event))
                session = MutatingSession()
                session.attach(on_event)
                self.sessions.append(session)
                for event in self.start_events:
                    on_event(event)
                return session

        runtime = MutatingRuntime()
        coordinator = CursorExecutionCoordinator(runtime, now=lambda: NOW)
        sink = RecordingSink()
        handle = coordinator.start(_run_request(), sink)
        _, on_event = runtime.starts[0]
        on_event(CursorSdkEvent(kind="run.started"))
        on_event(_tool_event())
        session = runtime.sessions[0]

        original = {"items": [1, 2]}
        outcome = handle.submit_tool_result("call-1", original)
        self.assertEqual(outcome.outcome, SubmitToolResultProviderOutcome.ACCEPTED)
        # The SDK saw the mutation...
        forwarded = session.tool_submissions[-1][1]
        self.assertEqual(forwarded.get("mutated"), True)
        # ...but the stored receipt must remain the pre-mutation value, so
        # an exact replay of the original snapshot is still ACCEPTED.
        replay = handle.submit_tool_result("call-1", {"items": [1, 2]})
        self.assertEqual(replay.outcome, SubmitToolResultProviderOutcome.ACCEPTED)
        # Caller-side mutation after submission also cannot break replay,
        # because the receipt was deep-copied at submission time.
        original["items"].append(99)
        replay_after_caller_mutation = handle.submit_tool_result(
            "call-1", {"items": [1, 2]}
        )
        self.assertEqual(
            replay_after_caller_mutation.outcome,
            SubmitToolResultProviderOutcome.ACCEPTED,
        )

    def test_terminal_payload_rejects_top_level_error(self) -> None:
        # Related fix (d): a terminal payload that has both terminal_result
        # and a top-level "error" key is rejected; the frozen schema only
        # permits terminal_result.error.
        runtime = FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(runtime, now=lambda: NOW)
        sink = RecordingSink()
        coordinator.start(_run_request(), sink)
        _, on_event = runtime.starts[0]
        on_event(CursorSdkEvent(kind="run.started"))
        with self.assertRaises(CursorProtocolError):
            on_event(
                CursorSdkEvent(
                    kind="run.failed",
                    payload={
                        "terminal_result": {
                            "outcome": "failed",
                            "error": {
                                "code": "internal",
                                "retryable": False,
                            },
                        },
                        "error": {
                            "code": "internal",
                            "retryable": False,
                        },
                    },
                )
            )
        self.assertEqual(sink.kinds(), ("run.started",))


import threading  # used by the bug-fix regression tests above


def _delta_event(channel: str = "text", delta: str = "hi") -> CursorSdkEvent:
    return CursorSdkEvent(
        kind="content.delta", payload={"channel": channel, "delta": delta}
    )


def _delta_with_unknown_key(**extra: Any) -> CursorSdkEvent:
    payload: dict[str, Any] = {"channel": "text", "delta": "hi"}
    payload.update(extra)
    return CursorSdkEvent(kind="content.delta", payload=payload)


def _usage_event(**overrides: Any) -> CursorSdkEvent:
    record: dict[str, Any] = {
        "run_id": RUN_ID,
        "session_id": SESSION_ID,
        "observed_at": NOW,
        "units": 7,
        "unit_kind": "input_tokens",
    }
    record.update(overrides)
    return CursorSdkEvent(kind="usage.observed", payload={"usage": record})


class ProviderMismatchTest(unittest.TestCase):
    def test_non_cursor_route_rejected_without_dispatch(self) -> None:
        runtime = FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(runtime, now=lambda: NOW)
        request = _run_request(
            route_snapshot=_route_snapshot(provider_id="com.modeldeck.provider.other")
        )
        with self.assertRaises(CursorProtocolError):
            coordinator.start(request, RecordingSink())
        self.assertEqual(runtime.starts, [])
        self.assertIsNone(coordinator.get_handle(RUN_ID))
        self.assertEqual(coordinator.open_run_ids, ())
        handle = coordinator.start(_run_request(), RecordingSink())
        self.assertIsInstance(handle, ProviderRunHandle)
        self.assertEqual(len(runtime.starts), 1)


class NonFinitePayloadTest(unittest.TestCase):
    def _started(self) -> tuple[Any, Any, Any]:
        runtime = FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(runtime, now=lambda: NOW)
        sink = RecordingSink()
        coordinator.start(_run_request(), sink)
        _, on_event = runtime.starts[0]
        on_event(CursorSdkEvent(kind="run.started"))
        return coordinator, sink, on_event

    def test_nan_usage_rejected(self) -> None:
        _, sink, on_event = self._started()
        with self.assertRaises(CursorProtocolError):
            on_event(_usage_event(units=float("nan")))
        self.assertEqual(sink.kinds(), ("run.started",))

    def test_infinity_tool_arguments_rejected(self) -> None:
        _, sink, on_event = self._started()
        with self.assertRaises(CursorProtocolError):
            on_event(
                CursorSdkEvent(
                    kind="tool.requested",
                    payload={
                        "tool_call": {
                            "call_id": "call-1",
                            "tool_name": "search",
                            "arguments": {"limit": float("inf")},
                        }
                    },
                )
            )
        self.assertEqual(sink.kinds(), ("run.started",))

    def test_terminal_error_rejects_unknown_fields(self) -> None:
        # "score" is not part of the terminal error schema, so the unknown
        # fields guard rejects the payload before the finite-JSON guard
        # could even inspect it.  The terminal error schema has no numeric
        # fields, so a NaN-in-valid-field test is not meaningful here;
        # finite-JSON coverage for numeric fields lives in test_nan_usage_rejected
        # and test_infinity_tool_arguments_rejected.
        _, sink, on_event = self._started()
        with self.assertRaises(CursorProtocolError):
            on_event(
                CursorSdkEvent(
                    kind="run.failed",
                    payload={
                        "terminal_result": {
                            "outcome": "failed",
                            "error": {
                                "code": "internal",
                                "retryable": False,
                                "score": float("nan"),
                            },
                        }
                    },
                )
            )
        self.assertEqual(sink.kinds(), ("run.started",))


class PayloadShapeTest(unittest.TestCase):
    def _started(self) -> tuple[Any, Any, Any]:
        runtime = FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(runtime, now=lambda: NOW)
        sink = RecordingSink()
        coordinator.start(_run_request(), sink)
        _, on_event = runtime.starts[0]
        on_event(CursorSdkEvent(kind="run.started"))
        return coordinator, sink, on_event

    def test_malformed_payloads_rejected_per_kind(self) -> None:
        _, sink, on_event = self._started()
        cases = [
            CursorSdkEvent(kind="run.started", payload={"unexpected": True}),
            CursorSdkEvent(kind="content.delta", payload=None),
            CursorSdkEvent(kind="content.delta", payload={"channel": "t"}),
            CursorSdkEvent(kind="content.delta", payload={"channel": "t", "delta": 3}),
            _delta_with_unknown_key(extra_key=1),
            CursorSdkEvent(kind="tool.requested", payload={"call_id": "c"}),
            CursorSdkEvent(
                kind="tool.requested",
                payload={"tool_call": {"call_id": "c"}},
            ),
            CursorSdkEvent(
                kind="tool.requested",
                payload={
                    "tool_call": {
                        "call_id": "c",
                        "tool_name": "n",
                        "extra": 1,
                    }
                },
            ),
            CursorSdkEvent(kind="usage.observed", payload=None),
            CursorSdkEvent(kind="usage.observed", payload={}),
            CursorSdkEvent(kind="usage.observed", payload={"usage": [1]}),
            CursorSdkEvent(kind="run.cancelling", payload={"phase": 1}),
            CursorSdkEvent(kind="run.completed", payload=None),
            CursorSdkEvent(kind="run.completed", payload={}),
            CursorSdkEvent(
                kind="run.completed",
                payload={"terminal_result": {"outcome": "failed"}},
            ),
            CursorSdkEvent(
                kind="run.failed",
                payload={
                    "terminal_result": {"outcome": "failed", "error": "boom"}
                },
            ),
            CursorSdkEvent(
                kind="run.cancelled",
                payload={
                    "terminal_result": {"outcome": "cancelled"},
                    "unknown": 1,
                },
            ),
        ]
        for event in cases:
            with self.subTest(kind=event.kind, payload=event.payload):
                with self.assertRaises(CursorProtocolError):
                    on_event(event)
        self.assertEqual(sink.kinds(), ("run.started",))

    def test_valid_shapes_accepted(self) -> None:
        _, sink, on_event = self._started()
        on_event(_delta_event())
        on_event(_usage_event(units=7))
        on_event(CursorSdkEvent(kind="run.cancelling", payload=None))
        on_event(_tool_event())
        self.assertEqual(
            sink.kinds(),
            (
                "run.started",
                "content.delta",
                "usage.observed",
                "run.cancelling",
                "tool.requested",
            ),
        )

    def test_valid_started_and_terminal_published(self) -> None:
        runtime = FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(runtime, now=lambda: NOW)
        sink = RecordingSink()
        coordinator.start(_run_request(), sink)
        _, on_event = runtime.starts[0]
        on_event(CursorSdkEvent(kind="run.started", payload={}))
        on_event(
            CursorSdkEvent(
                kind="run.failed",
                payload={
                    "terminal_result": {
                        "outcome": "failed",
                        "error": {
                            "code": "internal",
                            "retryable": False,
                            "message": "boom",
                        },
                    }
                },
            )
        )
        self.assertEqual(sink.kinds(), ("run.started", "run.failed"))


class ConcurrencyTest(unittest.TestCase):
    def _started(self) -> tuple[Any, Any, Any, Any]:
        runtime = FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(runtime, now=lambda: NOW)
        sink = RecordingSink()
        handle = coordinator.start(_run_request(), sink)
        _, on_event = runtime.starts[0]
        on_event(CursorSdkEvent(kind="run.started"))
        return coordinator, sink, handle, runtime.sessions[0]

    def test_concurrent_terminals_yield_one_outcome_and_one_close(self) -> None:
        import threading

        runtime = FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(runtime, now=lambda: NOW)
        sink = RecordingSink()
        coordinator.start(_run_request(), sink)
        _, on_event = runtime.starts[0]
        on_event(CursorSdkEvent(kind="run.started"))
        session = runtime.sessions[0]
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def emit(kind: str) -> None:
            try:
                barrier.wait(timeout=10)
                on_event(_terminal_event(kind))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
        threads = [
            threading.Thread(target=emit, args=("run.completed",)),
            threading.Thread(target=emit, args=("run.failed",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        protocol_errors = [e for e in errors if isinstance(e, CursorProtocolError)]
        other_errors = [e for e in errors if not isinstance(e, CursorProtocolError)]
        self.assertEqual(other_errors, [])
        self.assertEqual(len(protocol_errors), 1)
        self.assertEqual(len(sink.events), 2)
        self.assertEqual(session.close_count, 1)

    def test_concurrent_close_session_closes_once(self) -> None:
        import threading

        _, _, handle, session = self._started()
        errors: list[BaseException] = []

        def close() -> None:
            try:
                handle.close_session()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=close) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(session.close_count, 1)

    def test_concurrent_tool_submit_forwards_once(self) -> None:
        import threading

        runtime = FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(runtime, now=lambda: NOW)
        sink = RecordingSink()
        handle = coordinator.start(_run_request(), sink)
        _, on_event = runtime.starts[0]
        on_event(CursorSdkEvent(kind="run.started"))
        on_event(_tool_event())
        session = runtime.sessions[0]
        outcomes: list[Any] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(4)

        def submit() -> None:
            try:
                barrier.wait(timeout=10)
                outcomes.append(
                    handle.submit_tool_result("call-1", {"ok": True}).outcome
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=submit) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(len(session.tool_submissions), 1)
        self.assertIn(
            SubmitToolResultProviderOutcome.ACCEPTED, outcomes
        )
        for outcome in outcomes:
            self.assertIn(
                outcome,
                (
                    SubmitToolResultProviderOutcome.ACCEPTED,
                    SubmitToolResultProviderOutcome.REJECTED,
                ),
            )

    def test_cancel_terminal_race_stays_valid(self) -> None:
        import threading

        runtime = FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(runtime, now=lambda: NOW)
        sink = RecordingSink()
        handle = coordinator.start(_run_request(), sink)
        _, on_event = runtime.starts[0]
        on_event(CursorSdkEvent(kind="run.started"))
        session = runtime.sessions[0]
        results: list[Any] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def do_cancel() -> None:
            try:
                barrier.wait(timeout=10)
                results.append(handle.request_cancel(deadline=DEADLINE))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def do_terminal() -> None:
            try:
                barrier.wait(timeout=10)
                on_event(_terminal_event("run.cancelled"))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=do_cancel),
            threading.Thread(target=do_terminal),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertIn("run.cancelled", sink.kinds())
        self.assertEqual(session.close_count, 1)
        followup = handle.request_cancel(deadline=DEADLINE)
        self.assertFalse(followup.request_accepted)
        self.assertEqual(
            followup.termination_status, ProviderCancelTerminationStatus.UNKNOWN
        )
        self.assertEqual(session.close_count, 1)

class FinalReviewRegressionTest(unittest.TestCase):
    def test_sink_can_wait_for_concurrent_coordinator_read(self):
        finished = threading.Event()
        class Sink(RecordingSink):
            def publish_provider_event(inner, event):
                if event.kind == 'content.delta':
                    reader = threading.Thread(target=lambda: (coordinator.get_handle(RUN_ID), finished.set()), daemon=True)
                    reader.start()
                    self.assertTrue(finished.wait(1), 'sink retains a state lock')
                    reader.join(1)
                super().publish_provider_event(event)
        runtime = FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(runtime)
        coordinator.start(_run_request(), Sink())
        runtime.sessions[0].emit(CursorSdkEvent('run.started'))
        runtime.sessions[0].emit(CursorSdkEvent('content.delta', {'channel': 'text', 'delta': 'x'}))

    def test_sink_reentrant_event_is_ordered_after_current_event(self):
        class Sink(RecordingSink):
            def publish_provider_event(inner, event):
                if event.kind == 'content.delta':
                    runtime.sessions[0].emit(_terminal_event('run.completed'))
                super().publish_provider_event(event)
        runtime = FakeCursorRuntime()
        sink = Sink()
        coordinator = CursorExecutionCoordinator(runtime)
        coordinator.start(_run_request(), sink)
        runtime.sessions[0].emit(CursorSdkEvent('run.started'))
        runtime.sessions[0].emit(CursorSdkEvent('content.delta', {'channel': 'text', 'delta': 'x'}))
        self.assertEqual(sink.kinds(), ('run.started', 'content.delta', 'run.completed'))
        self.assertEqual(runtime.sessions[0].close_count, 1)

    def test_retained_run_id_cannot_redispatch(self):
        runtime = FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(runtime)
        handle = coordinator.start(_run_request(), RecordingSink())
        runtime.sessions[0].emit(CursorSdkEvent('run.started'))
        runtime.sessions[0].emit(_terminal_event('run.completed'))
        self.assertEqual(coordinator.open_run_ids, ())
        with self.assertRaises(CursorDuplicateStartError):
            coordinator.start(_run_request(), RecordingSink())
        self.assertIs(coordinator.get_handle(RUN_ID), handle)
        self.assertEqual(len(runtime.starts), 1)

    def test_close_defers_until_blocked_submit_returns(self):
        runtime = FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(runtime)
        handle = coordinator.start(_run_request(), RecordingSink())
        session = runtime.sessions[0]
        session.emit(CursorSdkEvent('run.started'))
        session.emit(_tool_event())
        entered, release = threading.Event(), threading.Event()
        results = []
        def submit(call_id, result):
            entered.set()
            self.assertTrue(release.wait(1))
            self.assertEqual(session.close_count, 0)
            return SubmitToolResultProviderResult(outcome=SubmitToolResultProviderOutcome.ACCEPTED)
        session.submit_tool_result = submit
        worker = threading.Thread(target=lambda: results.append(handle.submit_tool_result('call-1', {})), daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            coordinator.close()
            self.assertTrue(handle.is_closed)
            self.assertEqual(session.close_count, 0)
            self.assertEqual(handle.submit_tool_result('call-1', {}).outcome, SubmitToolResultProviderOutcome.REJECTED)
        finally:
            release.set()
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0].outcome, SubmitToolResultProviderOutcome.ACCEPTED)
        self.assertEqual(session.close_count, 1)

    def test_frozen_schema_bounds_and_usage_formats(self):
        usage = dict(run_id=RUN_ID, session_id=SESSION_ID, observed_at=NOW, units=1, unit_kind='requests')
        invalid = [CursorSdkEvent('content.delta', {'channel':'text', 'delta':'x'*65537}),
                   _tool_event(tool_name='x'*129),
                   CursorSdkEvent('run.failed', {'terminal_result': {'outcome':'failed','error': {'code':'internal','retryable':False,'message':'x'*2049}}})]
        for field, value in [('provider_model_id',None), ('provider_model_id','x'*257), ('currency','x'*9), ('run_id','bad'), ('observed_at','bad'), ('registration_id','bad')]:
            invalid.append(CursorSdkEvent('usage.observed', {'usage': dict(usage, **{field:value})}))
        for event in invalid:
            with self.subTest(event=event.kind):
                runtime = FakeCursorRuntime()
                sink = RecordingSink()
                coordinator = CursorExecutionCoordinator(runtime)
                coordinator.start(_run_request(), sink)
                runtime.sessions[0].emit(CursorSdkEvent('run.started'))
                with self.assertRaises(CursorProtocolError):
                    runtime.sessions[0].emit(event)
                self.assertEqual(sink.kinds(), ('run.started',))

    def test_sink_failure_drains_reentrant_terminal_without_resurrection(self):
        class Sink(RecordingSink):
            def publish_provider_event(inner, event):
                if event.kind == 'content.delta':
                    runtime.sessions[0].emit(_terminal_event('run.completed'))
                    raise RuntimeError('sink failed')
                super().publish_provider_event(event)
        runtime = FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(runtime)
        sink = Sink()
        handle = coordinator.start(_run_request(), sink)
        session = runtime.sessions[0]
        session.emit(CursorSdkEvent('run.started'))
        with self.assertRaisesRegex(RuntimeError, 'sink failed'):
            session.emit(CursorSdkEvent('content.delta', {'channel':'text','delta':'x'}))
        self.assertTrue(handle.is_terminal)
        self.assertEqual(session.close_count, 1)
        self.assertEqual(sink.kinds(), ('run.started', 'run.completed'))

    def test_synchronous_cancel_terminal_defers_physical_close(self):
        runtime = FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(runtime)
        handle = coordinator.start(_run_request(), RecordingSink())
        session = runtime.sessions[0]
        session.emit(CursorSdkEvent('run.started'))
        def cancel(*, deadline):
            session.emit(_terminal_event('run.cancelled'))
            self.assertEqual(session.close_count, 0)
            return CancelProviderRunResult(request_accepted=True, termination_status=ProviderCancelTerminationStatus.CONFIRMED)
        session.request_cancel = cancel
        self.assertTrue(handle.request_cancel(deadline=DEADLINE).request_accepted)
        self.assertEqual(session.close_count, 1)

    def test_completed_tool_call_id_cannot_be_requested_again(self):
        runtime = FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(runtime)
        sink = RecordingSink()
        handle = coordinator.start(_run_request(), sink)
        session = runtime.sessions[0]
        session.emit(CursorSdkEvent('run.started'))
        session.emit(_tool_event())
        self.assertEqual(handle.submit_tool_result('call-1', {'answer':1}).outcome, SubmitToolResultProviderOutcome.ACCEPTED)
        with self.assertRaisesRegex(CursorProtocolError, 'already completed'):
            session.emit(_tool_event())
        self.assertIsNone(handle.outstanding_call_id)
        self.assertEqual(handle.submit_tool_result('call-1', {'answer':1}).outcome, SubmitToolResultProviderOutcome.ACCEPTED)
        self.assertEqual(len(session.tool_submissions), 1)
        session.emit(_terminal_event('run.completed'))
        self.assertEqual(sink.kinds(), ('run.started','tool.requested','run.completed'))

    def test_start_failure_removes_reentrantly_harvested_handle(self):
        class Sink(RecordingSink):
            def publish_provider_event(inner, event):
                if event.kind == 'run.completed':
                    self.assertIsNotNone(coordinator.get_handle(RUN_ID))
                    self.assertEqual(coordinator.open_run_ids, ())
                super().publish_provider_event(event)
        class Runtime(FakeCursorRuntime):
            def start(inner, request, on_event):
                on_event(CursorSdkEvent('run.started'))
                on_event(_terminal_event('run.completed'))
                raise RuntimeError('start failed after events')
        coordinator = CursorExecutionCoordinator(Runtime())
        with self.assertRaisesRegex(RuntimeError, 'start failed'):
            coordinator.start(_run_request(), Sink())
        self.assertIsNone(coordinator.get_handle(RUN_ID))
        self.assertEqual(coordinator.open_run_ids, ())

    def test_shutdown_failure_does_not_skip_other_sessions(self):
        runtime = FakeCursorRuntime()
        coordinator = CursorExecutionCoordinator(runtime)
        coordinator.start(_run_request(), RecordingSink())
        coordinator.start(_run_request(run_id=RUN_ID_2), RecordingSink())
        def failing_close():
            runtime.sessions[0].close_count += 1
            raise RuntimeError('close failed')
        runtime.sessions[0].close = failing_close
        with self.assertRaisesRegex(RuntimeError, 'close failed'):
            coordinator.close()
        self.assertEqual([session.close_count for session in runtime.sessions], [1,1])
        self.assertEqual(coordinator.open_run_ids, ())
        coordinator.close()
        self.assertEqual([session.close_count for session in runtime.sessions], [1,1])

    def test_usage_rejects_out_of_range_offset_minutes(self):
        runtime = FakeCursorRuntime()
        sink = RecordingSink()
        coordinator = CursorExecutionCoordinator(runtime)
        coordinator.start(_run_request(), sink)
        runtime.sessions[0].emit(CursorSdkEvent('run.started'))
        for timestamp in ('2026-09-12T12:00:00+00:99','2026-09-12T12:00:00+01:60'):
            with self.subTest(timestamp=timestamp), self.assertRaises(CursorProtocolError):
                runtime.sessions[0].emit(CursorSdkEvent('usage.observed', {'usage':dict(run_id=RUN_ID,session_id=SESSION_ID,observed_at=timestamp,units=1,unit_kind='requests')}))
        self.assertEqual(sink.kinds(), ('run.started',))

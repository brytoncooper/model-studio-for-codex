import unittest
from typing import Any

from model_deck.adapters.providers.deterministic import (
    CrashFailed,
    CrashInterrupted,
    DETERMINISTIC_PROVIDER_ID,
    DeterministicCapabilityRejectedError,
    DeterministicDuplicateStartError,
    DeterministicProviderExecutionPort,
    DeterministicProviderRunHandle,
    DeterministicRouteMismatchError,
    DeterministicRunClosedError,
    EmitContent,
    EmitStarted,
    EmitTerminalCancelled,
    EmitTerminalCompleted,
    EmitTerminalFailed,
    EmitTerminalInterrupted,
    EmitToolRequested,
    EmitUsage,
    HoldCancellation,
)
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
    RunRequest,
    SubmitToolResultProviderOutcome,
)

RUN_ID = "550e8400-e29b-41d4-a716-446655440004"
SESSION_ID = "550e8400-e29b-41d4-a716-446655440003"
REGISTRATION_ID = "550e8400-e29b-41d4-a716-446655440001"
CONNECTION_ID = "550e8400-e29b-41d4-a716-446655440002"


class RecordingSink(ProviderRunEventSink):
    def __init__(self) -> None:
        self.events: list[ProviderRunEvent] = []

    def publish_provider_event(self, event: ProviderRunEvent) -> None:
        self.events.append(event)


def _route_snapshot(**overrides: Any) -> RouteSnapshot:
    base = {
        "registration_id": REGISTRATION_ID,
        "registration_revision": 1,
        "connection_id": CONNECTION_ID,
        "connection_revision": 1,
        "provider_id": DETERMINISTIC_PROVIDER_ID,
        "provider_model_id": "fixture/model",
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
    base = {
        "run_id": RUN_ID,
        "session_id": SESSION_ID,
        "client_request_id": "client-1",
        "idempotency_key": "idem-1",
        "route_snapshot": _route_snapshot(),
        "input": NormalizedRunInput(messages=("hello",)),
    }
    base.update(overrides)
    return RunRequest(**base)


def _start(
    script: tuple[Any, ...],
    *,
    route_snapshot: RouteSnapshot | None = None,
) -> tuple[DeterministicProviderRunHandle, RecordingSink]:
    provider = DeterministicProviderExecutionPort(script=script)
    sink = RecordingSink()
    request = _run_request(
        route_snapshot=route_snapshot or _route_snapshot(),
    )
    handle = provider.start(request, sink)
    assert isinstance(handle, DeterministicProviderRunHandle)
    return handle, sink



TERMINAL_KINDS = frozenset(
    {
        "run.completed",
        "run.failed",
        "run.cancelled",
        "run.interrupted",
    }
)


def _terminal_events(sink: RecordingSink) -> list[ProviderRunEvent]:
    return [event for event in sink.events if event.kind in TERMINAL_KINDS]


class DeterministicProviderTests(unittest.TestCase):
    def test_implements_provider_execution_port(self) -> None:
        provider = DeterministicProviderExecutionPort()
        self.assertIsInstance(provider, ProviderExecutionPort)

    def test_exact_event_order(self) -> None:
        script = (
            EmitStarted(),
            EmitContent("hello"),
            EmitUsage({"input_tokens": 1, "output_tokens": 2}),
            EmitTerminalCompleted(),
        )
        handle, sink = _start(script)
        while not handle.is_terminal:
            handle.emit_next()
        kinds = [event.kind for event in sink.events]
        self.assertEqual(
            kinds,
            ["run.started", "content.delta", "usage.observed", "run.completed"],
        )

    def test_terminal_exclusivity(self) -> None:
        script = (EmitStarted(), EmitTerminalCompleted())
        handle, sink = _start(script)
        handle.emit_next()
        handle.emit_next()
        self.assertTrue(handle.is_terminal)
        with self.assertRaises(DeterministicRunClosedError):
            handle.emit_next()
        self.assertEqual(len(sink.events), 2)

    def test_duplicate_start_rejected(self) -> None:
        provider = DeterministicProviderExecutionPort(script=(EmitTerminalCompleted(),))
        sink = RecordingSink()
        request = _run_request()
        provider.start(request, sink)
        with self.assertRaises(DeterministicDuplicateStartError):
            provider.start(request, sink)

    def test_records_single_run_request(self) -> None:
        provider = DeterministicProviderExecutionPort(script=(EmitTerminalCompleted(),))
        sink = RecordingSink()
        request = _run_request()
        provider.start(request, sink)
        recorded = provider.recorded_request(RUN_ID)
        self.assertEqual(recorded.run_id, RUN_ID)
        self.assertEqual(recorded.session_id, SESSION_ID)

    def test_tool_suspension_and_result(self) -> None:
        script = (
            EmitToolRequested("call-1", "lookup", {"q": "x"}),
            EmitContent("after-tool"),
            EmitTerminalCompleted(),
        )
        handle, sink = _start(script)
        handle.emit_next()
        self.assertEqual(handle.outstanding_call_id, "call-1")
        self.assertFalse(handle.emit_next())
        outcome = handle.submit_tool_result("call-1", {"value": 1})
        self.assertEqual(outcome.outcome, SubmitToolResultProviderOutcome.ACCEPTED)
        handle.emit_next()
        handle.emit_next()
        kinds = [event.kind for event in sink.events]
        self.assertEqual(kinds[0], "tool.requested")
        self.assertIn("content.delta", kinds)
        self.assertEqual(kinds[-1], "run.completed")

    def test_tool_result_idempotent_replay(self) -> None:
        script = (
            EmitToolRequested("call-1", "lookup"),
            EmitTerminalCompleted(),
        )
        handle, _ = _start(script)
        handle.emit_next()
        first = handle.submit_tool_result("call-1", {"ok": True})
        second = handle.submit_tool_result("call-1", {"ok": True})
        self.assertEqual(first.outcome, SubmitToolResultProviderOutcome.ACCEPTED)
        self.assertEqual(second.outcome, SubmitToolResultProviderOutcome.ACCEPTED)

    def test_tool_result_mismatch_rejected(self) -> None:
        script = (EmitToolRequested("call-1", "lookup"), EmitTerminalCompleted())
        handle, _ = _start(script)
        handle.emit_next()
        handle.submit_tool_result("call-1", {"ok": True})
        replay = handle.submit_tool_result("call-1", {"ok": False})
        self.assertEqual(replay.outcome, SubmitToolResultProviderOutcome.REJECTED)

    def test_tool_result_wrong_call_rejected(self) -> None:
        script = (EmitToolRequested("call-1", "lookup"), EmitTerminalCompleted())
        handle, _ = _start(script)
        handle.emit_next()
        outcome = handle.submit_tool_result("other", {"ok": True})
        self.assertEqual(outcome.outcome, SubmitToolResultProviderOutcome.REJECTED)

    def test_cancel_unconfirmed_when_hold(self) -> None:
        script = (
            HoldCancellation(),
            EmitContent("still-running"),
            EmitTerminalCancelled(),
        )
        handle, sink = _start(script)
        handle.emit_next()
        cancel = handle.request_cancel(deadline="2026-01-01T00:01:00Z")
        self.assertTrue(cancel.request_accepted)
        self.assertEqual(
            cancel.termination_status, ProviderCancelTerminationStatus.UNCONFIRMED
        )
        handle.emit_next()
        self.assertEqual(sink.events[-1].kind, "run.cancelled")
        with self.assertRaises(DeterministicRunClosedError):
            handle.emit_next()

    def test_cancel_confirmed_without_hold(self) -> None:
        script = (EmitStarted(), EmitTerminalCompleted())
        handle, sink = _start(script)
        handle.emit_next()
        cancel = handle.request_cancel(deadline="2026-01-01T00:01:00Z")
        self.assertTrue(cancel.request_accepted)
        self.assertEqual(
            cancel.termination_status, ProviderCancelTerminationStatus.CONFIRMED
        )
        self.assertTrue(handle.is_terminal)
        terminals = _terminal_events(sink)
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0].kind, "run.cancelled")
        with self.assertRaises(DeterministicRunClosedError):
            handle.emit_next()

    def test_crash_interrupted_no_retry(self) -> None:
        script = (
            EmitStarted(),
            CrashInterrupted(),
            EmitTerminalCompleted(),
        )
        handle, sink = _start(script)
        handle.emit_next()
        handle.emit_next()
        self.assertTrue(handle.is_terminal)
        with self.assertRaises(DeterministicRunClosedError):
            handle.emit_next()
        self.assertEqual(sink.events[-1].kind, "run.interrupted")

    def test_crash_failed(self) -> None:
        script = (CrashFailed(), EmitTerminalCompleted())
        handle, sink = _start(script)
        handle.emit_next()
        self.assertEqual(sink.events[0].kind, "run.failed")
        with self.assertRaises(DeterministicRunClosedError):
            handle.emit_next()

    def test_terminal_payload_completed_omits_error(self) -> None:
        script = (EmitTerminalCompleted(),)
        handle, sink = _start(script)
        handle.emit_next()
        terminal_result = sink.events[-1].payload["terminal_result"]
        self.assertEqual(terminal_result, {"outcome": "completed"})
        self.assertNotIn("error", terminal_result)

    def test_terminal_payload_failed_structured_error(self) -> None:
        script = (EmitTerminalFailed(),)
        handle, sink = _start(script)
        handle.emit_next()
        terminal_result = sink.events[-1].payload["terminal_result"]
        self.assertEqual(terminal_result["outcome"], "failed")
        error = terminal_result["error"]
        self.assertEqual(error["code"], "internal")
        self.assertIn("message", error)
        self.assertFalse(error["retryable"])

    def test_race_completion_first_single_terminal(self) -> None:
        script = (
            EmitStarted(),
            HoldCancellation(),
            EmitContent("between"),
            EmitTerminalCompleted(),
        )
        handle, sink = _start(script)
        handle.emit_next()
        handle.emit_next()
        cancel = handle.request_cancel(deadline="2026-01-01T00:01:00Z")
        self.assertEqual(
            cancel.termination_status, ProviderCancelTerminationStatus.UNCONFIRMED
        )
        handle.emit_next()
        terminals = _terminal_events(sink)
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0].kind, "run.completed")

    def test_race_cancel_first_single_terminal(self) -> None:
        script = (
            EmitStarted(),
            HoldCancellation(),
            EmitContent("between"),
            EmitTerminalCancelled(),
        )
        handle, sink = _start(script)
        handle.emit_next()
        handle.emit_next()
        cancel = handle.request_cancel(deadline="2026-01-01T00:01:00Z")
        self.assertEqual(
            cancel.termination_status, ProviderCancelTerminationStatus.UNCONFIRMED
        )
        handle.emit_next()
        terminals = _terminal_events(sink)
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0].kind, "run.cancelled")

    def test_race_immediate_cancel_blocks_completion(self) -> None:
        script = (EmitStarted(), EmitTerminalCompleted())
        handle, sink = _start(script)
        handle.emit_next()
        cancel = handle.request_cancel(deadline="2026-01-01T00:02:00Z")
        self.assertEqual(
            cancel.termination_status, ProviderCancelTerminationStatus.CONFIRMED
        )
        terminals = _terminal_events(sink)
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0].kind, "run.cancelled")
        with self.assertRaises(DeterministicRunClosedError):
            handle.emit_next()

    def test_race_completion_before_cancel_request(self) -> None:
        script = (EmitStarted(), EmitTerminalCompleted())
        handle, sink = _start(script)
        handle.emit_next()
        handle.emit_next()
        cancel = handle.request_cancel(deadline="2026-01-01T00:02:00Z")
        self.assertFalse(cancel.request_accepted)
        self.assertEqual(
            cancel.termination_status, ProviderCancelTerminationStatus.UNKNOWN
        )
        terminals = _terminal_events(sink)
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0].kind, "run.completed")

    def test_start_default_manual_no_auto_progress(self) -> None:
        script = (
            EmitStarted(),
            EmitContent("hello"),
            EmitTerminalCompleted(),
        )
        handle, sink = _start(script)
        self.assertFalse(handle.is_terminal)
        self.assertEqual(sink.events, [])

    def test_auto_advance_text_script_completes_during_start(self) -> None:
        script = (
            EmitStarted(),
            EmitContent("hello"),
            EmitTerminalCompleted(),
        )
        provider = DeterministicProviderExecutionPort(
            script=script,
            auto_advance=True,
        )
        sink = RecordingSink()
        handle = provider.start(_run_request(), sink)
        assert isinstance(handle, DeterministicProviderRunHandle)
        self.assertTrue(handle.is_terminal)
        kinds = [event.kind for event in sink.events]
        self.assertEqual(
            kinds,
            ["run.started", "content.delta", "run.completed"],
        )
        with self.assertRaises(DeterministicRunClosedError):
            handle.emit_next()

    def test_auto_advance_stops_at_tool_request_and_resumes_manually(self) -> None:
        script = (
            EmitToolRequested("call-1", "lookup", {"q": "x"}),
            EmitContent("after-tool"),
            EmitTerminalCompleted(),
        )
        provider = DeterministicProviderExecutionPort(
            script=script,
            auto_advance=True,
        )
        sink = RecordingSink()
        handle = provider.start(_run_request(), sink)
        assert isinstance(handle, DeterministicProviderRunHandle)
        self.assertFalse(handle.is_terminal)
        self.assertEqual(handle.outstanding_call_id, "call-1")
        self.assertEqual([event.kind for event in sink.events], ["tool.requested"])
        outcome = handle.submit_tool_result("call-1", {"value": 1})
        self.assertEqual(outcome.outcome, SubmitToolResultProviderOutcome.ACCEPTED)
        handle.emit_next()
        handle.emit_next()
        kinds = [event.kind for event in sink.events]
        self.assertEqual(
            kinds,
            ["tool.requested", "content.delta", "run.completed"],
        )

    def test_route_provider_mismatch_rejected(self) -> None:
        provider = DeterministicProviderExecutionPort(script=(EmitTerminalCompleted(),))
        sink = RecordingSink()
        request = _run_request(
            route_snapshot=_route_snapshot(provider_id="com.other.provider"),
        )
        with self.assertRaises(DeterministicRouteMismatchError):
            provider.start(request, sink)

    def test_unknown_tools_capability_rejected(self) -> None:
        provider = DeterministicProviderExecutionPort(
            script=(EmitToolRequested("call-1", "lookup"), EmitTerminalCompleted())
        )
        sink = RecordingSink()
        request = _run_request(
            route_snapshot=_route_snapshot(
                capability_features=(
                    CapabilityFeature("tools", CapabilityTriState.UNKNOWN),
                )
            )
        )
        with self.assertRaises(DeterministicCapabilityRejectedError):
            provider.start(request, sink)

    def test_opaque_route_refs_only(self) -> None:
        snapshot = _route_snapshot()
        self.assertTrue(str(snapshot.credential_ref).startswith("ref:"))
        self.assertTrue(str(snapshot.endpoint_config_ref).startswith("ref:"))
        provider = DeterministicProviderExecutionPort(script=(EmitTerminalCompleted(),))
        sink = RecordingSink()
        request = _run_request(route_snapshot=snapshot)
        provider.start(request, sink)


if __name__ == "__main__":
    unittest.main()

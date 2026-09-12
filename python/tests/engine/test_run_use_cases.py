import dataclasses
import unittest
from typing import Any

from model_deck.engine.routing.ports import (
    CapabilityTriState,
    ExecutionMode,
    RouteResolveRequest,
    RouteResolver,
    RouteSnapshot,
    UnsupportedCapabilityError,
)
from model_deck.engine.runs.ports import (
    ActiveRunState,
    AppendApplicationEventCommand,
    AppendApplicationEventResult,
    ApplicationRunEvent,
    CancelRunCommand,
    CancelRunResult,
    ClaimDispatchCommand,
    CompleteTerminalCommand,
    CompleteTerminalResult,
    GetRunCommand,
    NormalizedRunInput,
    ProviderExecutionPort,
    ProviderCancelTerminationStatus,
    ProviderRunEvent,
    ProviderRunEventSink,
    ProviderRunHandle,
    RestartRecoveryResult,
    RunAdmissionKey,
    RunAdmissionRequestHashConflictError,
    RunAdmissionResult,
    RunRecord,
    RunRepository,
    RunRequest,
    RunState,
    RunStateConflictError,
    RunTerminalConflictError,
    StartRunCommand,
    SubmitToolResultCommand,
    SubmitToolResultResult,
    SubmitToolResultProviderOutcome,
    SubmitToolResultProviderResult,
    TERMINAL_RUN_STATES,
    TerminalOutcome,
    TerminalResult,
    ToolCallDescriptor,
    ToolCallNotOutstandingError,
    ToolResultIdempotencyConflictError,
)
from model_deck.engine.runs.use_cases import (
    CancelRunUseCase,
    GetRunUseCase,
    RunApplicationCoordinator,
    StartRunUseCase,
    SubmitToolResultUseCase,
)
from model_deck.engine.sessions.ports import GetSessionCommand, SessionRecord, SessionRepository

REGISTRATION_ID = "550e8400-e29b-41d4-a716-446655440001"
CONNECTION_ID = "550e8400-e29b-41d4-a716-446655440002"
SESSION_ID = "550e8400-e29b-41d4-a716-446655440003"
RUN_ID = "550e8400-e29b-41d4-a716-446655440004"
PRINCIPAL_ID = "principal-550e8400-e29b-41d4-a716-446655440005"
HOST_CTX = "ref:host.context"


def _route_snapshot(**overrides: Any) -> RouteSnapshot:
    base = {
        "registration_id": REGISTRATION_ID,
        "registration_revision": 2,
        "connection_id": CONNECTION_ID,
        "connection_revision": 1,
        "provider_id": "com.example.provider",
        "provider_model_id": "provider/model",
        "execution_mode": ExecutionMode.CHAT_COMPLETIONS,
        "endpoint_config_ref": "ref:endpoint.config",
        "credential_ref": "ref:credential.token",
    }
    base.update(overrides)
    return RouteSnapshot(**base)


def _run_record(**overrides: Any) -> RunRecord:
    base = {
        "run_id": RUN_ID,
        "session_id": SESSION_ID,
        "state": RunState.ACCEPTED,
        "client_request_id": "client-1",
        "registration_id": REGISTRATION_ID,
        "route_snapshot": _route_snapshot(),
        "principal_id": PRINCIPAL_ID,
        "authorized_host_context_ref": HOST_CTX,
    }
    base.update(overrides)
    return RunRecord(**base)


def _start_params(**overrides: Any) -> dict[str, Any]:
    base = {
        "session_id": SESSION_ID,
        "client_request_id": "client-1",
        "idempotency_key": "idem-1",
        "registration_id": REGISTRATION_ID,
        "input": {"messages": [{"role": "user", "content": "hi"}]},
    }
    base.update(overrides)
    return base


class RecordingSessionRepository:
    def __init__(self, record: SessionRecord | None = None) -> None:
        self.get_calls: list[GetSessionCommand] = []
        self.record = record or SessionRecord(
            session_id=SESSION_ID,
            registration_id=REGISTRATION_ID,
            revision=1,
            host_context_ref=HOST_CTX,
        )

    def create(self, command):
        raise NotImplementedError

    def get(self, command: GetSessionCommand) -> SessionRecord:
        self.get_calls.append(command)
        return self.record

    def select_model(self, command):
        raise NotImplementedError


class RecordingRouteResolver:
    def __init__(self) -> None:
        self.requests: list[RouteResolveRequest] = []
        self.result: RouteSnapshot = _route_snapshot()
        self.error: BaseException | None = None

    def resolve_active_registration(self, request: RouteResolveRequest) -> RouteSnapshot:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.result


class RecordingRunRepository:
    def __init__(self) -> None:
        self.lookup_calls: list[tuple[RunAdmissionKey, str]] = []
        self.admit_calls: list[StartRunCommand] = []
        self.claim_calls: list[ClaimDispatchCommand] = []
        self.complete_calls: list[CompleteTerminalCommand] = []
        self.cancel_calls: list[CancelRunCommand] = []
        self.submit_calls: list[SubmitToolResultCommand] = []
        self.append_calls: list[AppendApplicationEventCommand] = []
        self.store: dict[tuple[str, str, str], tuple[str, RunAdmissionResult]] = {}
        self.runs: dict[str, RunRecord] = {}
        self.sequence = 0
        self.complete_terminal_raises: BaseException | None = None
        self.outstanding_tool: str | None = None
        self.tool_receipts: dict[tuple[str, str, str], Any] = {}
        self.recovery_result = RestartRecoveryResult(
            observed_at="2026-01-01T00:00:00Z",
            dispatchable_requests=(),
            interrupted_run_ids=(),
        )

    def lookup_admission(
        self, admission_key: RunAdmissionKey, request_hash: str
    ) -> RunAdmissionResult | None:
        self.lookup_calls.append((admission_key, request_hash))
        entry = self.store.get(
            (
                admission_key.principal_id,
                admission_key.operation_id,
                admission_key.idempotency_key,
            )
        )
        if entry is None:
            return None
        stored_hash, result = entry
        if stored_hash != request_hash:
            raise RunAdmissionRequestHashConflictError
        return RunAdmissionResult(run=result.run, dispatch_required=False)

    def admit(self, command: StartRunCommand) -> RunAdmissionResult:
        self.admit_calls.append(command)
        prior = self.lookup_admission(command.admission_key, command.request_hash)
        if prior is not None:
            return prior
        run = _run_record(
            session_id=command.session_id,
            client_request_id=command.client_request_id,
            registration_id=command.registration_id,
            route_snapshot=command.route_snapshot,
            principal_id=command.admission_key.principal_id,
            authorized_host_context_ref=command.authorized_host_context_ref,
        )
        result = RunAdmissionResult(run=run, dispatch_required=True)
        self.runs[run.run_id] = run
        self.store[
            (
                command.admission_key.principal_id,
                command.admission_key.operation_id,
                command.admission_key.idempotency_key,
            )
        ] = (command.request_hash, result)
        return result

    def claim_dispatch(self, command: ClaimDispatchCommand) -> RunRecord:
        self.claim_calls.append(command)
        run = self.runs[command.run_id]
        updated = dataclasses.replace(run, state=RunState.RUNNING)
        self.runs[run.run_id] = updated
        return updated

    def append_application_event(
        self, command: AppendApplicationEventCommand
    ) -> AppendApplicationEventResult:
        self.append_calls.append(command)
        run = self.runs[command.run_id]
        if run.state.value != command.expected_state.value:
            raise RunStateConflictError("state mismatch")
        self.sequence += 1
        updated = dataclasses.replace(
            run,
            state=RunState(command.new_state.value),
            last_sequence=self.sequence,
        )
        self.runs[command.run_id] = updated
        event = ApplicationRunEvent(
            kind=command.kind,
            run_id=run.run_id,
            session_id=run.session_id,
            sequence=self.sequence,
            event_schema_version=1,
            observed_at="2026-01-01T00:00:00Z",
            payload=command.payload,
        )
        return AppendApplicationEventResult(run=updated, event=event)

    def request_cancel(self, command: CancelRunCommand) -> CancelRunResult:
        self.cancel_calls.append(command)
        run = self.runs[command.run_id]
        if run.state in TERMINAL_RUN_STATES:
            return CancelRunResult(run=run, event=None)
        self.sequence += 1
        updated = dataclasses.replace(
            run,
            state=RunState.CANCELLING,
            last_sequence=self.sequence,
        )
        self.runs[command.run_id] = updated
        return CancelRunResult(
            run=updated,
            event=ApplicationRunEvent(
                kind="run.cancelling",
                run_id=run.run_id,
                session_id=run.session_id,
                sequence=self.sequence,
                event_schema_version=1,
                observed_at="2026-01-01T00:00:00Z",
            ),
        )

    def submit_tool_result(
        self, command: SubmitToolResultCommand
    ) -> SubmitToolResultResult:
        run = self.runs[command.run_id]
        receipt_key = (command.run_id, command.call_id, command.idempotency_key)
        if receipt_key in self.tool_receipts:
            if self.tool_receipts[receipt_key] != command.result:
                raise ToolResultIdempotencyConflictError
            return SubmitToolResultResult(
                run=self.runs[command.run_id],
                provider_submission_required=False,
            )
        self.submit_calls.append(command)
        if run.principal_id != command.principal_id:
            raise RunStateConflictError("principal mismatch")
        if run.authorized_host_context_ref != command.host_context_ref:
            raise RunStateConflictError("host mismatch")
        if self.outstanding_tool != command.call_id:
            raise ToolCallNotOutstandingError
        self.tool_receipts[receipt_key] = command.result
        self.outstanding_tool = None
        updated = dataclasses.replace(run, state=RunState.RUNNING)
        self.runs[command.run_id] = updated
        return SubmitToolResultResult(
            run=updated,
            provider_submission_required=True,
        )

    def complete_terminal(self, command: CompleteTerminalCommand) -> CompleteTerminalResult:
        self.complete_calls.append(command)
        if self.complete_terminal_raises is not None:
            raise self.complete_terminal_raises
        run = self.runs[command.run_id]
        if self.outstanding_tool is not None:
            raise RunTerminalConflictError("outstanding tool")
        self.sequence += 1
        updated = dataclasses.replace(
            run,
            state=RunState(command.terminal_result.outcome.value),
            terminal_result=command.terminal_result,
            last_sequence=self.sequence,
        )
        self.runs[command.run_id] = updated
        return CompleteTerminalResult(
            run=updated,
            event=ApplicationRunEvent(
                kind=command.final_event_kind,
                run_id=run.run_id,
                session_id=run.session_id,
                sequence=self.sequence,
                event_schema_version=1,
                observed_at=command.observed_at or "2026-01-01T00:00:00Z",
                payload=command.final_event_payload,
            ),
        )

    def get(self, command: GetRunCommand) -> RunRecord:
        return self.runs[command.run_id]

    def recover_after_restart(self, observed_at: str) -> RestartRecoveryResult:
        return dataclasses.replace(self.recovery_result, observed_at=observed_at)


class RecordingProviderHandle:
    def __init__(self) -> None:
        self.cancel_deadlines: list[str] = []
        self.tool_calls: list[tuple[str, Any]] = []
        self.reject_tool = False
        self.cancel_status = ProviderCancelTerminationStatus.UNCONFIRMED

    def submit_tool_result(self, call_id: str, result: Any) -> SubmitToolResultProviderResult:
        self.tool_calls.append((call_id, result))
        if self.reject_tool:
            return SubmitToolResultProviderResult(SubmitToolResultProviderOutcome.REJECTED)
        return SubmitToolResultProviderResult(SubmitToolResultProviderOutcome.ACCEPTED)

    def request_cancel(self, *, deadline: str):
        from model_deck.engine.runs.ports import CancelProviderRunResult, ProviderCancelTerminationStatus

        self.cancel_deadlines.append(deadline)
        return CancelProviderRunResult(
            request_accepted=True,
            termination_status=self.cancel_status,
        )


class RecordingProvider:
    def __init__(self) -> None:
        self.starts: list[RunRequest] = []
        self.fail_start = False
        self.handle = RecordingProviderHandle()

    def start(self, request: RunRequest, sink: ProviderRunEventSink) -> ProviderRunHandle:
        self.starts.append(request)
        if self.fail_start:
            raise RuntimeError("provider start failed")
        return self.handle


class RecordingEventPublisher:
    def __init__(self) -> None:
        self.events: list[ApplicationRunEvent] = []

    def publish_application_event(self, event: ApplicationRunEvent) -> None:
        self.events.append(event)


class RunUseCaseTests(unittest.TestCase):
    def _build(self):
        runs = RecordingRunRepository()
        sessions = RecordingSessionRepository()
        routes = RecordingRouteResolver()
        provider = RecordingProvider()
        coordinator = RunApplicationCoordinator(runs)
        start = StartRunUseCase(runs, sessions, routes, provider, coordinator)
        return runs, sessions, routes, provider, coordinator, start

    def test_lookup_before_session_and_route_on_replay(self) -> None:
        runs, sessions, routes, provider, _, start = self._build()
        start.execute(_start_params(), principal_id=PRINCIPAL_ID, authorized_host_context_ref=HOST_CTX)
        sessions.get_calls.clear()
        routes.requests.clear()
        provider.starts.clear()
        result = start.execute(_start_params(), principal_id=PRINCIPAL_ID, authorized_host_context_ref=HOST_CTX)
        self.assertEqual(result["run"]["run_id"], RUN_ID)
        self.assertEqual(len(sessions.get_calls), 0)
        self.assertEqual(len(routes.requests), 0)
        self.assertEqual(len(provider.starts), 0)

    def test_changed_hash_conflict(self) -> None:
        runs, _, _, _, _, start = self._build()
        key = RunAdmissionKey(PRINCIPAL_ID, "engine.v1.runs.start", "idem-1")
        runs.store[(PRINCIPAL_ID, "engine.v1.runs.start", "idem-1")] = (
            "other-hash",
            RunAdmissionResult(run=_run_record(), dispatch_required=False),
        )
        with self.assertRaises(RunAdmissionRequestHashConflictError):
            start.execute(_start_params(), principal_id=PRINCIPAL_ID)

    def test_single_dispatch_and_captured_snapshot(self) -> None:
        runs, _, routes, provider, _, start = self._build()
        routes.result = _route_snapshot(provider_model_id="provider/captured")
        first = start.execute(_start_params(), principal_id=PRINCIPAL_ID, authorized_host_context_ref=HOST_CTX)
        second = start.execute(_start_params(), principal_id=PRINCIPAL_ID, authorized_host_context_ref=HOST_CTX)
        self.assertEqual(len(provider.starts), 1)
        self.assertEqual(len(runs.claim_calls), 1)
        self.assertEqual(provider.starts[0].route_snapshot.provider_model_id, "provider/captured")
        self.assertEqual(first["run"]["run_id"], second["run"]["run_id"])

    def test_provider_start_failure_interrupts_without_retry(self) -> None:
        runs, _, _, provider, _, start = self._build()
        provider.fail_start = True
        result = start.execute(
            _start_params(), principal_id=PRINCIPAL_ID, authorized_host_context_ref=HOST_CTX
        )
        self.assertEqual(len(provider.starts), 1)
        self.assertEqual(len(runs.complete_calls), 1)
        self.assertEqual(result["run"]["state"], "interrupted")

    def test_recovery_claims_and_starts_durable_request_without_readmission(self) -> None:
        runs, _, _, provider, coordinator, _ = self._build()
        request = RunRequest(
            run_id=RUN_ID,
            session_id=SESSION_ID,
            client_request_id="client-recovery",
            idempotency_key="idem-recovery",
            route_snapshot=_route_snapshot(provider_model_id="provider/captured"),
            input=NormalizedRunInput(messages=({"role": "user", "content": "resume"},)),
            tools=(),
        )
        runs.runs[RUN_ID] = _run_record(
            client_request_id=request.client_request_id,
            route_snapshot=request.route_snapshot,
        )
        runs.recovery_result = RestartRecoveryResult(
            observed_at="ignored",
            dispatchable_requests=(request,),
            interrupted_run_ids=(),
        )

        result = coordinator.recover_after_restart(
            provider,
            observed_at="2026-01-02T00:00:00Z",
        )

        self.assertEqual(result.dispatchable_requests, (request,))
        self.assertEqual(runs.admit_calls, [])
        self.assertEqual(runs.claim_calls, [ClaimDispatchCommand(RUN_ID, RUN_ID)])
        self.assertEqual(provider.starts, [request])
        self.assertIs(coordinator.get_handle(RUN_ID), provider.handle)

    def test_recovery_start_failure_interrupts_once_at_recovery_time(self) -> None:
        runs, _, _, provider, coordinator, _ = self._build()
        request = RunRequest(
            run_id=RUN_ID,
            session_id=SESSION_ID,
            client_request_id="client-recovery",
            idempotency_key="idem-recovery",
            route_snapshot=_route_snapshot(),
            input=NormalizedRunInput(),
        )
        runs.runs[RUN_ID] = _run_record(client_request_id=request.client_request_id)
        runs.recovery_result = RestartRecoveryResult(
            observed_at="ignored",
            dispatchable_requests=(request,),
            interrupted_run_ids=(),
        )
        provider.fail_start = True

        coordinator.recover_after_restart(
            provider,
            observed_at="2026-01-02T00:00:00Z",
        )

        self.assertEqual(len(provider.starts), 1)
        self.assertEqual(len(runs.complete_calls), 1)
        completion = runs.complete_calls[0]
        self.assertEqual(completion.observed_at, "2026-01-02T00:00:00Z")
        self.assertEqual(completion.terminal_result.outcome, TerminalOutcome.INTERRUPTED)
        self.assertEqual(runs.runs[RUN_ID].state, RunState.INTERRUPTED)
        self.assertIsNone(coordinator.get_handle(RUN_ID))

    def test_recovery_sync_terminal_does_not_register_stale_handle(self) -> None:
        runs, _, _, provider, coordinator, _ = self._build()
        request = RunRequest(
            run_id=RUN_ID,
            session_id=SESSION_ID,
            client_request_id="client-recovery",
            idempotency_key="idem-recovery",
            route_snapshot=_route_snapshot(),
            input=NormalizedRunInput(),
        )
        runs.runs[RUN_ID] = _run_record(client_request_id=request.client_request_id)
        runs.recovery_result = RestartRecoveryResult(
            observed_at="ignored",
            dispatchable_requests=(request,),
            interrupted_run_ids=(),
        )

        class TerminalOnRecoveryProvider(RecordingProvider):
            def start(self, recovered, sink):
                self.starts.append(recovered)
                sink.publish_provider_event(
                    ProviderRunEvent(
                        kind="run.completed",
                        run_id=recovered.run_id,
                        observed_at="2026-01-02T00:00:00Z",
                        payload={"terminal_result": {"outcome": "completed"}},
                    )
                )
                return self.handle

        terminal_provider = TerminalOnRecoveryProvider()
        coordinator.recover_after_restart(
            terminal_provider,
            observed_at="2026-01-02T00:00:00Z",
        )

        self.assertEqual(len(terminal_provider.starts), 1)
        self.assertEqual(runs.runs[RUN_ID].state, RunState.COMPLETED)
        self.assertIsNone(coordinator.get_handle(RUN_ID))

    def test_recovery_publishes_durable_interrupted_event(self) -> None:
        runs = RecordingRunRepository()
        publisher = RecordingEventPublisher()
        coordinator = RunApplicationCoordinator(runs, event_publisher=publisher)
        runs.runs[RUN_ID] = _run_record(
            state=RunState.INTERRUPTED,
            terminal_result=TerminalResult(TerminalOutcome.INTERRUPTED),
            last_sequence=4,
        )
        runs.recovery_result = RestartRecoveryResult(
            observed_at="2026-01-02T00:00:00Z",
            dispatchable_requests=(),
            interrupted_run_ids=(RUN_ID,),
        )

        coordinator.recover_after_restart(
            RecordingProvider(),
            observed_at="2026-01-02T00:00:00Z",
        )

        self.assertEqual(
            [(event.kind, event.sequence) for event in publisher.events],
            [("run.interrupted", 4)],
        )

    def test_malformed_params_make_no_repository_mutations(self) -> None:
        runs, sessions, routes, provider, _, start = self._build()
        with self.assertRaises(ValueError):
            start.execute({"session_id": SESSION_ID, "extra": 1}, principal_id=PRINCIPAL_ID)
        self.assertEqual(len(runs.admit_calls), 0)
        self.assertEqual(len(sessions.get_calls), 0)
        self.assertEqual(len(routes.requests), 0)
        self.assertEqual(len(provider.starts), 0)

    def test_registration_mismatch_rejected(self) -> None:
        runs, sessions, _, _, _, start = self._build()
        sessions.record = SessionRecord(
            session_id=SESSION_ID,
            registration_id="550e8400-e29b-41d4-a716-446655440099",
            revision=1,
        )
        with self.assertRaises(ValueError):
            start.execute(_start_params(), principal_id=PRINCIPAL_ID)
        self.assertEqual(len(runs.admit_calls), 0)

    def test_get_returns_run_summary(self) -> None:
        runs, _, _, _, _, _ = self._build()
        runs.runs[RUN_ID] = _run_record(state=RunState.RUNNING)
        result = GetRunUseCase(runs).execute({"run_id": RUN_ID})
        summary = result["run"]
        self.assertEqual(set(summary.keys()), {"run_id", "session_id", "state", "client_request_id"})

    def test_cancel_records_cancelling_and_requests_provider(self) -> None:
        runs, _, _, provider, coordinator, _ = self._build()
        runs.runs[RUN_ID] = _run_record(state=RunState.RUNNING)
        coordinator.register_handle(RUN_ID, provider.handle)
        cancel = CancelRunUseCase(runs, coordinator, cancel_deadline="2026-01-01T00:01:00Z")
        result = cancel.execute({"run_id": RUN_ID, "idempotency_key": "cancel-1"})
        self.assertTrue(result["accepted"])
        self.assertEqual(result["state"], "cancelling")
        self.assertEqual(provider.handle.cancel_deadlines, ["2026-01-01T00:01:00Z"])

    def test_submit_forwards_principal_and_host(self) -> None:
        runs, _, _, provider, coordinator, _ = self._build()
        runs.runs[RUN_ID] = _run_record(state=RunState.WAITING_FOR_TOOL)
        runs.outstanding_tool = "call-1"
        coordinator.register_handle(RUN_ID, provider.handle)
        submit = SubmitToolResultUseCase(runs, coordinator)
        result = submit.execute(
            {
                "run_id": RUN_ID,
                "call_id": "call-1",
                "result": {"ok": True},
                "idempotency_key": "tool-1",
            },
            principal_id=PRINCIPAL_ID,
            authorized_host_context_ref=HOST_CTX,
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(runs.submit_calls[0].principal_id, PRINCIPAL_ID)
        self.assertEqual(runs.submit_calls[0].host_context_ref, HOST_CTX)

    def test_provider_tool_rejection_is_conflict(self) -> None:
        runs, _, _, provider, coordinator, _ = self._build()
        runs.runs[RUN_ID] = _run_record(state=RunState.WAITING_FOR_TOOL)
        runs.outstanding_tool = "call-1"
        provider.handle.reject_tool = True
        coordinator.register_handle(RUN_ID, provider.handle)
        submit = SubmitToolResultUseCase(runs, coordinator)
        with self.assertRaises(RunStateConflictError):
            submit.execute(
                {
                    "run_id": RUN_ID,
                    "call_id": "call-1",
                    "result": {"ok": True},
                    "idempotency_key": "tool-1",
                },
                principal_id=PRINCIPAL_ID,
                authorized_host_context_ref=HOST_CTX,
            )
        self.assertEqual(len(runs.complete_calls), 1)
        self.assertEqual(
            runs.complete_calls[0].terminal_result.outcome, TerminalOutcome.INTERRUPTED
        )
        self.assertEqual(runs.runs[RUN_ID].state, RunState.INTERRUPTED)
        self.assertIsNone(coordinator.get_handle(RUN_ID))

    def test_completion_with_outstanding_tool_surfaces_conflict(self) -> None:
        runs, _, _, _, coordinator, _ = self._build()
        runs.runs[RUN_ID] = _run_record(state=RunState.WAITING_FOR_TOOL)
        runs.outstanding_tool = "call-1"
        sink = coordinator.provider_sink()

        with self.assertRaises(RunTerminalConflictError):
            sink.publish_provider_event(
                ProviderRunEvent(
                    kind="run.completed",
                    run_id=RUN_ID,
                    observed_at="2026-01-01T00:00:00Z",
                )
            )


    def test_rejects_non_finite_json_in_start_input(self) -> None:
        _, _, _, _, _, start = self._build()
        with self.assertRaises(ValueError):
            start.execute(
                _start_params(input={"messages": [{"x": float("nan")}]}),
                principal_id=PRINCIPAL_ID,
                authorized_host_context_ref=HOST_CTX,
            )

    def test_rejects_explicit_null_input_and_tools(self) -> None:
        _, _, _, _, _, start = self._build()
        with self.assertRaises(ValueError):
            start.execute(
                _start_params(input=None),
                principal_id=PRINCIPAL_ID,
                authorized_host_context_ref=HOST_CTX,
            )
        with self.assertRaises(ValueError):
            start.execute(
                _start_params(tools=None),
                principal_id=PRINCIPAL_ID,
                authorized_host_context_ref=HOST_CTX,
            )

    def test_host_context_must_match_session(self) -> None:
        runs, _, _, _, _, start = self._build()
        with self.assertRaises(ValueError):
            start.execute(
                _start_params(),
                principal_id=PRINCIPAL_ID,
                authorized_host_context_ref="ref:other.host",
            )
        self.assertEqual(len(runs.admit_calls), 0)

    def test_tools_require_supported_capability_at_route_resolve(self) -> None:
        _, _, routes, _, _, start = self._build()
        start.execute(
            _start_params(tools=[{"call_id": "c1", "tool_name": "t", "arguments": {}}]),
            principal_id=PRINCIPAL_ID,
            authorized_host_context_ref=HOST_CTX,
        )
        self.assertEqual(len(routes.requests), 1)
        requirements = routes.requests[0].capability_requirements
        self.assertIsNotNone(requirements)
        self.assertEqual(requirements[0].name, "tools")
        self.assertEqual(requirements[0].state, CapabilityTriState.SUPPORTED)

    def test_unknown_provider_event_kind_is_rejected(self) -> None:
        runs, _, _, _, coordinator, _ = self._build()
        runs.runs[RUN_ID] = _run_record(state=RunState.RUNNING)
        sink = coordinator.provider_sink()
        with self.assertRaises(ValueError):
            sink.publish_provider_event(
                ProviderRunEvent(
                    kind="run.unknown",
                    run_id=RUN_ID,
                    observed_at="2026-01-01T00:00:00Z",
                )
            )
        self.assertEqual(len(runs.append_calls), 0)

    def test_sync_terminal_during_start_does_not_register_handle(self) -> None:
        runs, _, _, provider, coordinator, start = self._build()

        class TerminalOnStartProvider(RecordingProvider):
            def start(self, request, sink):
                sink.publish_provider_event(
                    ProviderRunEvent(
                        kind="run.completed",
                        run_id=request.run_id,
                        observed_at="2026-01-01T00:00:00Z",
                        payload={"terminal_result": {"outcome": "completed"}},
                    )
                )
                return provider.handle

        terminal_provider = TerminalOnStartProvider()
        start = StartRunUseCase(runs, RecordingSessionRepository(), RecordingRouteResolver(), terminal_provider, coordinator)
        result = start.execute(
            _start_params(),
            principal_id=PRINCIPAL_ID,
            authorized_host_context_ref=HOST_CTX,
        )
        self.assertEqual(result["run"]["state"], "completed")
        self.assertIsNone(coordinator.get_handle(RUN_ID))

    def test_provider_start_cleanup_error_preserves_sync_completed_terminal(self) -> None:
        runs, _, _, provider, coordinator, start = self._build()

        class CompleteThenRaiseProvider(RecordingProvider):
            def start(self, request, sink):
                self.starts.append(request)
                sink.publish_provider_event(
                    ProviderRunEvent(
                        kind="run.completed",
                        run_id=request.run_id,
                        observed_at="2026-01-01T00:00:00Z",
                        payload={"terminal_result": {"outcome": "completed"}},
                    )
                )
                raise RuntimeError("provider cleanup failed")

        start = StartRunUseCase(
            runs,
            RecordingSessionRepository(),
            RecordingRouteResolver(),
            CompleteThenRaiseProvider(),
            coordinator,
        )
        result = start.execute(
            _start_params(),
            principal_id=PRINCIPAL_ID,
            authorized_host_context_ref=HOST_CTX,
        )
        self.assertEqual(result["run"]["state"], "completed")
        self.assertEqual(len(runs.complete_calls), 1)
        self.assertEqual(runs.complete_calls[0].final_event_kind, "run.completed")
        self.assertEqual(
            runs.complete_calls[0].terminal_result.outcome,
            TerminalOutcome.COMPLETED,
        )

    def test_provider_start_failure_payload_uses_terminal_result(self) -> None:
        runs, _, _, provider, _, start = self._build()
        provider.fail_start = True
        start.execute(_start_params(), principal_id=PRINCIPAL_ID, authorized_host_context_ref=HOST_CTX)
        payload = runs.complete_calls[0].final_event_payload
        self.assertIn("terminal_result", payload)
        self.assertEqual(payload["terminal_result"]["outcome"], "interrupted")

    def test_cancel_replay_on_cancelled_vs_other_terminal(self) -> None:
        runs, _, _, _, coordinator, _ = self._build()
        cancel = CancelRunUseCase(runs, coordinator, cancel_deadline="2026-01-01T00:01:00Z")
        runs.runs[RUN_ID] = _run_record(state=RunState.CANCELLED)
        cancelled = cancel.execute({"run_id": RUN_ID, "idempotency_key": "cancel-1"})
        self.assertTrue(cancelled["accepted"])
        runs.runs[RUN_ID] = _run_record(state=RunState.COMPLETED)
        completed = cancel.execute({"run_id": RUN_ID, "idempotency_key": "cancel-2"})
        self.assertFalse(completed["accepted"])

    def test_confirmed_cancel_without_provider_event_terminalizes_once(self) -> None:
        runs, _, _, provider, coordinator, _ = self._build()
        runs.runs[RUN_ID] = _run_record(state=RunState.CANCELLING)
        provider.handle.cancel_status = ProviderCancelTerminationStatus.CONFIRMED
        coordinator.register_handle(RUN_ID, provider.handle)
        cancel = CancelRunUseCase(runs, coordinator, cancel_deadline="2026-01-01T00:01:00Z")
        result = cancel.execute({"run_id": RUN_ID, "idempotency_key": "cancel-3"})
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(len(runs.complete_calls), 1)
        self.assertIsNone(coordinator.get_handle(RUN_ID))

    def test_cancel_publishes_committed_cancelling_and_terminal_events(self) -> None:
        runs = RecordingRunRepository()
        provider = RecordingProvider()
        publisher = RecordingEventPublisher()
        coordinator = RunApplicationCoordinator(runs, event_publisher=publisher)
        runs.runs[RUN_ID] = _run_record(state=RunState.RUNNING)
        provider.handle.cancel_status = ProviderCancelTerminationStatus.CONFIRMED
        coordinator.register_handle(RUN_ID, provider.handle)
        cancel = CancelRunUseCase(
            runs,
            coordinator,
            cancel_deadline="2026-01-01T00:01:00Z",
        )

        result = cancel.execute({"run_id": RUN_ID, "idempotency_key": "cancel-events"})

        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(
            [(event.kind, event.sequence) for event in publisher.events],
            [("run.cancelling", 1), ("run.cancelled", 2)],
        )

    def test_submit_requires_result_key_and_allows_null_value(self) -> None:
        runs, _, _, provider, coordinator, _ = self._build()
        runs.runs[RUN_ID] = _run_record(state=RunState.WAITING_FOR_TOOL)
        runs.outstanding_tool = "call-1"
        coordinator.register_handle(RUN_ID, provider.handle)
        submit = SubmitToolResultUseCase(runs, coordinator)
        with self.assertRaises(ValueError):
            submit.execute(
                {
                    "run_id": RUN_ID,
                    "call_id": "call-1",
                    "idempotency_key": "tool-1",
                },
                principal_id=PRINCIPAL_ID,
                authorized_host_context_ref=HOST_CTX,
            )
        result = submit.execute(
            {
                "run_id": RUN_ID,
                "call_id": "call-1",
                "result": None,
                "idempotency_key": "tool-2",
            },
            principal_id=PRINCIPAL_ID,
            authorized_host_context_ref=HOST_CTX,
        )
        self.assertTrue(result["accepted"])

    def test_submit_without_handle_terminalizes_on_first_admission(self) -> None:
        runs, _, _, provider, coordinator, _ = self._build()
        runs.runs[RUN_ID] = _run_record(state=RunState.WAITING_FOR_TOOL)
        runs.outstanding_tool = "call-1"
        submit = SubmitToolResultUseCase(runs, coordinator)
        params = {
            "run_id": RUN_ID,
            "call_id": "call-1",
            "result": {"ok": True},
            "idempotency_key": "tool-no-handle",
        }
        with self.assertRaises(RunStateConflictError):
            submit.execute(
                params,
                principal_id=PRINCIPAL_ID,
                authorized_host_context_ref=HOST_CTX,
            )
        self.assertEqual(len(runs.complete_calls), 1)
        self.assertEqual(
            runs.complete_calls[0].terminal_result.outcome, TerminalOutcome.INTERRUPTED
        )
        self.assertEqual(runs.runs[RUN_ID].state, RunState.INTERRUPTED)
        self.assertEqual(len(provider.handle.tool_calls), 0)
        replay = submit.execute(
            params,
            principal_id=PRINCIPAL_ID,
            authorized_host_context_ref=HOST_CTX,
        )
        self.assertTrue(replay["accepted"])
        self.assertEqual(len(provider.handle.tool_calls), 0)
        self.assertEqual(len(runs.complete_calls), 1)

    def test_submit_idempotent_replay_skips_provider(self) -> None:
        runs, _, _, provider, coordinator, _ = self._build()
        runs.runs[RUN_ID] = _run_record(state=RunState.WAITING_FOR_TOOL)
        runs.outstanding_tool = "call-1"
        coordinator.register_handle(RUN_ID, provider.handle)
        submit = SubmitToolResultUseCase(runs, coordinator)
        params = {
            "run_id": RUN_ID,
            "call_id": "call-1",
            "result": {"ok": True},
            "idempotency_key": "tool-3",
        }
        submit.execute(params, principal_id=PRINCIPAL_ID, authorized_host_context_ref=HOST_CTX)
        self.assertEqual(len(provider.handle.tool_calls), 1)
        submit.execute(params, principal_id=PRINCIPAL_ID, authorized_host_context_ref=HOST_CTX)
        self.assertEqual(len(provider.handle.tool_calls), 1)

    def test_capability_unsupported_propagates_before_admit(self) -> None:
        runs, _, routes, _, _, start = self._build()
        routes.error = UnsupportedCapabilityError("tools unsupported")
        with self.assertRaises(UnsupportedCapabilityError):
            start.execute(
                _start_params(tools=[{"call_id": "c1", "tool_name": "t", "arguments": {}}]),
                principal_id=PRINCIPAL_ID,
                authorized_host_context_ref=HOST_CTX,
            )
        self.assertEqual(len(runs.admit_calls), 0)

if __name__ == "__main__":
    unittest.main()

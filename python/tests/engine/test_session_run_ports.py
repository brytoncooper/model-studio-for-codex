import dataclasses
import unittest
from typing import Any

from model_deck.engine.routing.ports import (
    CapabilityFeature,
    CapabilityTriState,
    ExecutionMode,
    RouteResolveRequest,
    RouteResolver,
    RouteSnapshot,
)
from model_deck.engine.runs.ports import (
    ADMISSION_RECORD_RETENTION_MIN_HOURS,
    RUN_LIVE_REPLAY_MAX_BYTES,
    RUN_LIVE_REPLAY_MAX_DURATION_SECONDS,
    SUBSCRIBER_QUEUE_MAX_BYTES,
    SUBSCRIBER_QUEUE_MAX_EVENTS,
    ActiveRunState,
    AppendApplicationEventCommand,
    ApplicationRunEvent,
    CancelProviderRunResult,
    CancelRunCommand,
    CancelRunResult,
    ClaimDispatchCommand,
    CompleteTerminalCommand,
    CompleteTerminalResult,
    EventReplayOutcome,
    EventReplayPage,
    ProviderCancelTerminationStatus,
    ProviderExecutionPort,
    ProviderRunEventSink,
    ProviderRunHandle,
    ReplaySubscriptionHandle,
    RestartRecoveryResult,
    RunAdmissionKey,
    RunAdmissionRequestHashConflictError,
    RunAdmissionResult,
    RunEventReplayPort,
    NormalizedRunInput,
    RunRecord,
    RunRepository,
    RunRequest,
    RunState,
    StartRunCommand,
    SubmitToolResultCommand,
    SubmitToolResultResult,
    SubmitToolResultProviderOutcome,
    SubmitToolResultProviderResult,
    TERMINAL_RUN_STATES,
    TerminalOutcome,
    TerminalResult,
    ToolCallDescriptor,
)
from model_deck.engine.sessions.ports import (
    CreateSessionCommand,
    SessionRecord,
    SessionRepository,
)

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
        "capability_snapshot_ref": "ref:cap.snapshot",
        "capability_features": (
            CapabilityFeature("tools", CapabilityTriState.UNKNOWN),
        ),
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


class SessionRunPortsContractTests(unittest.TestCase):
    def test_session_record_is_immutable(self) -> None:
        record = SessionRecord(
            session_id=SESSION_ID,
            registration_id=REGISTRATION_ID,
            revision=1,
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            record.revision = 2  # type: ignore[misc]

    def test_route_snapshot_has_only_opaque_refs(self) -> None:
        snap = _route_snapshot()
        field_names = {f.name for f in dataclasses.fields(RouteSnapshot)}
        self.assertNotIn("credential_value", field_names)
        self.assertNotIn("endpoint_config", field_names)
        self.assertIsNotNone(snap.credential_ref)
        self.assertTrue(str(snap.credential_ref).startswith("ref:"))

    def test_capability_features_are_immutable_tuple(self) -> None:
        feature = CapabilityFeature("tools", CapabilityTriState.UNKNOWN)
        self.assertEqual(CapabilityTriState.UNKNOWN.value, "unknown")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            feature.name = "other"  # type: ignore[misc]

    def test_route_resolver_accepts_capability_request(self) -> None:
        class FakeResolver:
            def resolve_active_registration(self, request: RouteResolveRequest) -> RouteSnapshot:
                self.last = request
                return _route_snapshot(registration_id=request.registration_id)

        resolver = FakeResolver()
        self.assertIsInstance(resolver, RouteResolver)
        req = RouteResolveRequest(
            registration_id=REGISTRATION_ID,
            capability_snapshot_ref="ref:cap.req",
            capability_requirements=(
                CapabilityFeature("tools", CapabilityTriState.UNKNOWN),
            ),
        )
        resolver.resolve_active_registration(req)
        self.assertEqual(resolver.last.capability_snapshot_ref, "ref:cap.req")
        self.assertIsInstance(req.capability_requirements, tuple)

    def test_lookup_admission_before_admit(self) -> None:
        class RecordingRunRepo:
            def __init__(self) -> None:
                self.store: dict[tuple[str, str, str], tuple[str, RunAdmissionResult]] = {}

            def lookup_admission(
                self, admission_key: RunAdmissionKey, request_hash: str
            ) -> RunAdmissionResult | None:
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
                return result

            def admit(self, command: StartRunCommand) -> RunAdmissionResult:
                prior = self.lookup_admission(command.admission_key, command.request_hash)
                if prior is not None:
                    return RunAdmissionResult(run=prior.run, dispatch_required=False)
                run = _run_record(
                    session_id=command.session_id,
                    client_request_id=command.client_request_id,
                    registration_id=command.registration_id,
                    route_snapshot=command.route_snapshot,
                    principal_id=command.admission_key.principal_id,
                    authorized_host_context_ref=command.authorized_host_context_ref,
                )
                result = RunAdmissionResult(run=run, dispatch_required=True)
                self.store[
                    (
                        command.admission_key.principal_id,
                        command.admission_key.operation_id,
                        command.admission_key.idempotency_key,
                    )
                ] = (command.request_hash, result)
                return result

            def claim_dispatch(self, command: ClaimDispatchCommand) -> RunRecord:
                raise NotImplementedError

            def append_application_event(self, command: AppendApplicationEventCommand):
                raise NotImplementedError

            def request_cancel(self, command: CancelRunCommand) -> CancelRunResult:
                raise NotImplementedError

            def submit_tool_result(self, command: SubmitToolResultCommand) -> SubmitToolResultResult:
                raise NotImplementedError

            def complete_terminal(self, command: CompleteTerminalCommand) -> CompleteTerminalResult:
                raise NotImplementedError

            def get(self, command):
                raise NotImplementedError

            def recover_after_restart(self, observed_at: str) -> RestartRecoveryResult:
                return RestartRecoveryResult(observed_at, (), ())

        repo = RecordingRunRepo()
        key = RunAdmissionKey(
            principal_id=PRINCIPAL_ID,
            operation_id="runs.start",
            idempotency_key="idem-1",
        )
        command = StartRunCommand(
            admission_key=key,
            request_hash="hash-1",
            session_id=SESSION_ID,
            client_request_id="client-1",
            registration_id=REGISTRATION_ID,
            route_snapshot=_route_snapshot(),
            input=NormalizedRunInput(messages=("plain-string-message",)),
            tools=(ToolCallDescriptor(call_id="c1", tool_name="search", arguments={"q": 1}),),
            authorized_host_context_ref=HOST_CTX,
        )
        self.assertIsNone(repo.lookup_admission(key, "hash-1"))
        first = repo.admit(command)
        replay = repo.lookup_admission(key, "hash-1")
        self.assertIsNotNone(replay)
        self.assertEqual(replay.run.run_id, first.run.run_id)
        second = repo.admit(command)
        self.assertTrue(first.dispatch_required)
        self.assertFalse(second.dispatch_required)
        with self.assertRaises(RunAdmissionRequestHashConflictError):
            repo.lookup_admission(key, "other-hash")

    def test_run_record_captures_authorization(self) -> None:
        fields = {f.name for f in dataclasses.fields(RunRecord)}
        self.assertIn("principal_id", fields)
        self.assertIn("authorized_host_context_ref", fields)
        record = _run_record()
        self.assertEqual(record.principal_id, PRINCIPAL_ID)
        self.assertEqual(record.authorized_host_context_ref, HOST_CTX)

    def test_submit_tool_result_command_shape(self) -> None:
        fields = {f.name for f in dataclasses.fields(SubmitToolResultCommand)}
        self.assertIn("call_id", fields)
        self.assertIn("principal_id", fields)
        self.assertIn("host_context_ref", fields)
        self.assertNotIn("tool_call_id", fields)

    def test_submit_tool_result_result_shape(self) -> None:
        fields = {f.name for f in dataclasses.fields(SubmitToolResultResult)}
        self.assertEqual(fields, {"run", "provider_submission_required"})

    def test_cancel_and_terminal_results_return_committed_events(self) -> None:
        self.assertEqual(
            {field.name for field in dataclasses.fields(CancelRunResult)},
            {"run", "event"},
        )
        self.assertEqual(
            {field.name for field in dataclasses.fields(CompleteTerminalResult)},
            {"run", "event"},
        )

    def test_restart_recovery_result_shape(self) -> None:
        fields = {f.name for f in dataclasses.fields(RestartRecoveryResult)}
        self.assertEqual(
            fields,
            {"observed_at", "dispatchable_requests", "interrupted_run_ids"},
        )

    def test_active_run_state_used_for_append(self) -> None:
        cmd = AppendApplicationEventCommand(
            run_id=RUN_ID,
            expected_state=ActiveRunState.ACCEPTED,
            new_state=ActiveRunState.RUNNING,
            kind="run.started",
        )
        self.assertEqual(cmd.expected_state, ActiveRunState.ACCEPTED)
        self.assertEqual(cmd.new_state, ActiveRunState.RUNNING)

    def test_recover_after_restart_contract(self) -> None:
        class RecoveryRepo:
            def lookup_admission(self, admission_key, request_hash):
                return None

            def admit(self, command):
                raise NotImplementedError

            def claim_dispatch(self, command):
                raise NotImplementedError

            def append_application_event(self, command):
                raise NotImplementedError

            def request_cancel(self, command):
                raise NotImplementedError

            def submit_tool_result(self, command):
                raise NotImplementedError

            def complete_terminal(self, command):
                raise NotImplementedError

            def get(self, command):
                raise NotImplementedError

            def recover_after_restart(self, observed_at: str) -> RestartRecoveryResult:
                request = RunRequest(
                    run_id="accepted-unclaimed",
                    session_id=SESSION_ID,
                    client_request_id="client-recovery",
                    idempotency_key="idem-recovery",
                    route_snapshot=_route_snapshot(),
                    input=NormalizedRunInput(messages=()),
                    tools=(),
                )
                return RestartRecoveryResult(
                    observed_at=observed_at,
                    dispatchable_requests=(request,),
                    interrupted_run_ids=("claimed-running",),
                )

        repo = RecoveryRepo()
        self.assertIsInstance(repo, RunRepository)
        result = repo.recover_after_restart("2026-01-01T00:00:00Z")
        self.assertEqual(len(result.dispatchable_requests), 1)
        self.assertEqual(result.dispatchable_requests[0].run_id, "accepted-unclaimed")
        self.assertEqual(result.dispatchable_requests[0].idempotency_key, "idem-recovery")
        self.assertEqual(result.dispatchable_requests[0].route_snapshot.registration_id, REGISTRATION_ID)
        self.assertEqual(result.interrupted_run_ids, ("claimed-running",))

    def test_replay_limit_constants_are_separated(self) -> None:
        self.assertEqual(SUBSCRIBER_QUEUE_MAX_EVENTS, 256)
        self.assertEqual(SUBSCRIBER_QUEUE_MAX_BYTES, 1_048_576)
        self.assertEqual(RUN_LIVE_REPLAY_MAX_BYTES, 8_388_608)
        self.assertEqual(RUN_LIVE_REPLAY_MAX_DURATION_SECONDS, 60)

    def test_event_replay_subscribe_ack_unsubscribe(self) -> None:
        class FakeReplay(RunEventReplayPort):
            def subscribe(self, run_id: str, *, after_sequence: int | None, grant_credit: int):
                if grant_credit <= 0:
                    page = EventReplayPage((), None, EventReplayOutcome.SLOW_READER, 0, 0)
                    return ReplaySubscriptionHandle("sub-1", run_id), page
                event = ApplicationRunEvent(
                    kind="run.accepted",
                    run_id=run_id,
                    session_id=SESSION_ID,
                    sequence=1,
                    event_schema_version=1,
                    observed_at="2026-01-01T00:00:00Z",
                )
                page = EventReplayPage((event,), 2, EventReplayOutcome.DELIVERED, 128, grant_credit - 1)
                return ReplaySubscriptionHandle("sub-1", run_id), page

            def ack(self, handle, through_sequence: int, return_credit: int):
                from model_deck.engine.runs.ports import EventReplayAckResult

                return EventReplayAckResult(EventReplayOutcome.DELIVERED, return_credit)

            def read_available(self, handle):
                return EventReplayPage((), None, EventReplayOutcome.DELIVERED, 0, 0)

            def unsubscribe(self, handle) -> None:
                return None

        replay = FakeReplay()
        handle, page = replay.subscribe(RUN_ID, after_sequence=0, grant_credit=10)
        self.assertEqual(page.outcome, EventReplayOutcome.DELIVERED)
        replay.ack(handle, 1, 5)
        replay.unsubscribe(handle)

    def test_provider_handle_cancel_and_tool_result(self) -> None:
        class Handle:
            def submit_tool_result(self, call_id: str, result: Any) -> SubmitToolResultProviderResult:
                self.last_call = (call_id, result)
                return SubmitToolResultProviderResult(SubmitToolResultProviderOutcome.ACCEPTED)

            def request_cancel(self, *, deadline: str) -> CancelProviderRunResult:
                self.deadline = deadline
                return CancelProviderRunResult(
                    request_accepted=True,
                    termination_status=ProviderCancelTerminationStatus.UNCONFIRMED,
                )

        class Sink:
            def publish_provider_event(self, event) -> None:
                return None

        class FakeProvider:
            def start(self, request: RunRequest, sink: ProviderRunEventSink) -> ProviderRunHandle:
                return Handle()

        provider = FakeProvider()
        handle = provider.start(
            RunRequest(
                run_id=RUN_ID,
                session_id=SESSION_ID,
                client_request_id="client-1",
                idempotency_key="idem",
                route_snapshot=_route_snapshot(),
                input=NormalizedRunInput(),
            ),
            Sink(),
        )
        self.assertIsInstance(provider, ProviderExecutionPort)
        outcome = handle.submit_tool_result("call-1", {"ok": True})
        self.assertEqual(outcome.outcome, SubmitToolResultProviderOutcome.ACCEPTED)
        cancel = handle.request_cancel(deadline="2026-01-01T00:01:00Z")
        self.assertTrue(cancel.request_accepted)
        self.assertEqual(cancel.termination_status, ProviderCancelTerminationStatus.UNCONFIRMED)


    def test_admission_record_retention_constant(self) -> None:
        self.assertEqual(ADMISSION_RECORD_RETENTION_MIN_HOURS, 24)

    def test_claim_dispatch_doc_contract(self) -> None:
        doc = RunRepository.claim_dispatch.__doc__ or ""
        self.assertIn("accepted to running", doc)
        self.assertIn("no claimed run may remain accepted", doc)

    def test_recover_after_restart_doc_contract(self) -> None:
        doc = RunRepository.recover_after_restart.__doc__ or ""
        self.assertIn("dispatchable_requests", doc)
        self.assertIn("reconstructed", doc)
        self.assertIn("RouteSnapshot", doc)
        self.assertIn("does not re-resolve active registrations", doc)
        self.assertIn("claims dispatch once", doc)
        self.assertIn("Every claimed non-terminal run", doc)
        self.assertIn("redispatched", doc)
        self.assertIn("provider work is not restarted", doc)

    def test_run_event_replay_port_doc_contract(self) -> None:
        doc = RunEventReplayPort.__doc__ or ""
        self.assertIn("RESUME_UNAVAILABLE", doc)
        self.assertIn("RUN_LIVE_REPLAY_MAX_BYTES", doc)
        self.assertIn("RUN_LIVE_REPLAY_MAX_DURATION_SECONDS", doc)
        self.assertIn("SUBSCRIBER_QUEUE_MAX_EVENTS", doc)
        self.assertIn("never redispatch provider", doc)
        self.assertIn("not regenerated", doc)

    def test_terminal_state_vocabulary(self) -> None:
        self.assertIn(RunState.COMPLETED, TERMINAL_RUN_STATES)
        self.assertEqual(len(RunState), 8)


if __name__ == "__main__":
    unittest.main()

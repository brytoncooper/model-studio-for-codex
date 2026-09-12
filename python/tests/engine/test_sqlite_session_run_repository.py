import json
import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from model_deck.adapters.storage.sqlite_session_run_repository import SQLiteSessionRunRepository
from model_deck.engine.routing.ports import (
    CapabilityFeature,
    CapabilityTriState,
    ContinuationScope,
    ExecutionMode,
    RouteSnapshot,
)
from model_deck.engine.runs.ports import (
    ActiveRunState,
    AppendApplicationEventCommand,
    CancelRunCommand,
    ClaimDispatchCommand,
    CompleteTerminalCommand,
    GetRunCommand,
    NormalizedRunInput,
    RunAdmissionKey,
    RunAdmissionRequestHashConflictError,
    RunDispatchClaimError,
    RunNotFoundError,
    RunRecord,
    RunState,
    RunStateConflictError,
    RunTerminalConflictError,
    StartRunCommand,
    SubmitToolResultCommand,
    TerminalOutcome,
    TerminalResult,
    ToolCallDescriptor,
    ToolCallNotOutstandingError,
    ToolResultIdempotencyConflictError,
    RunAuthorizationMismatchError,
)
from model_deck.engine.sessions.ports import (
    CreateSessionCommand,
    GetSessionCommand,
    SelectModelCommand,
    SessionActiveRunConflictError,
    SessionNotFoundError,
    SessionRevisionConflictError,
)

REGISTRATION_ID = "550e8400-e29b-41d4-a716-446655440001"
REGISTRATION_ID_ALT = "550e8400-e29b-41d4-a716-446655440011"
CONNECTION_ID = "550e8400-e29b-41d4-a716-446655440002"
SESSION_ID = "550e8400-e29b-41d4-a716-446655440003"
RUN_ID = "550e8400-e29b-41d4-a716-446655440004"
RUN_ID_2 = "550e8400-e29b-41d4-a716-446655440014"
PRINCIPAL_ID = "principal-550e8400-e29b-41d4-a716-446655440005"
HOST_CTX = "ref:host.context"
OPERATION_ID = "engine.v1.runs.start"


def _route_snapshot(**overrides: Any) -> RouteSnapshot:
    base = {
        "registration_id": REGISTRATION_ID,
        "registration_revision": 2,
        "connection_id": CONNECTION_ID,
        "connection_revision": 1,
        "provider_id": "com.example.provider",
        "provider_model_id": "provider/captured-model",
        "execution_mode": ExecutionMode.CHAT_COMPLETIONS,
        "endpoint_config_ref": "ref:endpoint.config",
        "credential_ref": "ref:credential.token",
    }
    base.update(overrides)
    return RouteSnapshot(**base)


def _continuation_scope(**overrides: Any) -> ContinuationScope:
    base = {
        "connection_id": CONNECTION_ID,
        "provider_model_id": "provider/captured-model",
        "provider_id": "com.example.provider",
        "execution_mode": ExecutionMode.CHAT_COMPLETIONS,
        "handle": "ref:continuation.handle",
    }
    base.update(overrides)
    return ContinuationScope(**base)


def _repo(
    temp_dir: str,
    *,
    uuids: list[str] | None = None,
    clock: list[str] | None = None,
    connect: Any = None,
) -> SQLiteSessionRunRepository:
    ids = iter(uuids or [SESSION_ID, RUN_ID, RUN_ID_2])
    times = iter(clock or ["2026-01-01T00:00:00Z"])
    return SQLiteSessionRunRepository(
        Path(temp_dir) / "state.sqlite3",
        uuid_factory=lambda: next(ids),
        utc_clock=lambda: next(times, "2026-01-01T00:00:00Z"),
        connect=connect,
    )


def _create_session(repo: SQLiteSessionRunRepository) -> None:
    repo.create(
        CreateSessionCommand(
            registration_id=REGISTRATION_ID,
            host_context_ref=HOST_CTX,
        )
    )


def _start_command(**overrides: Any) -> StartRunCommand:
    base = {
        "admission_key": RunAdmissionKey(
            principal_id=PRINCIPAL_ID,
            operation_id=OPERATION_ID,
            idempotency_key="idem-1",
        ),
        "request_hash": "hash-1",
        "session_id": SESSION_ID,
        "client_request_id": "client-1",
        "registration_id": REGISTRATION_ID,
        "route_snapshot": _route_snapshot(),
        "input": NormalizedRunInput(messages=({"role": "user", "content": "hi"},)),
        "tools": (ToolCallDescriptor(call_id="c1", tool_name="search", arguments={"q": 1}),),
        "authorized_host_context_ref": HOST_CTX,
    }
    base.update(overrides)
    return StartRunCommand(**base)


class SQLiteSessionRunRepositoryTests(unittest.TestCase):
    def test_session_create_get_and_select_model_cas(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo = _repo(temp_dir)
            created = repo.create(
                CreateSessionCommand(
                    registration_id=REGISTRATION_ID,
                    host_context_ref=HOST_CTX,
                )
            )
            self.assertEqual(created.session_id, SESSION_ID)
            self.assertEqual(created.revision, 1)
            loaded = repo.get(GetSessionCommand(session_id=SESSION_ID))
            self.assertEqual(loaded.registration_id, REGISTRATION_ID)
            selected = repo.select_model(
                SelectModelCommand(
                    session_id=SESSION_ID,
                    registration_id=REGISTRATION_ID_ALT,
                    expected_revision=1,
                    continuation_reset=True,
                )
            )
            self.assertEqual(selected.revision, 2)
            self.assertIsNone(selected.continuation_scope)
            with self.assertRaises(SessionRevisionConflictError):
                repo.select_model(
                    SelectModelCommand(
                        session_id=SESSION_ID,
                        registration_id=REGISTRATION_ID_ALT,
                        expected_revision=1,
                    )
                )

    def test_select_model_rejects_active_run(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo = _repo(temp_dir, uuids=[SESSION_ID, RUN_ID])
            _create_session(repo)
            repo.admit(_start_command())
            with self.assertRaises(SessionActiveRunConflictError):
                repo.select_model(
                    SelectModelCommand(
                        session_id=SESSION_ID,
                        registration_id=REGISTRATION_ID,
                        expected_revision=1,
                    )
                )

    def test_admission_persistence_replay_and_hash_conflict(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo = _repo(temp_dir, uuids=[SESSION_ID, RUN_ID])
            _create_session(repo)
            command = _start_command()
            first = repo.admit(command)
            self.assertTrue(first.dispatch_required)
            self.assertEqual(first.run.state, RunState.ACCEPTED)
            self.assertEqual(first.run.last_sequence, 0)
            replay = repo.lookup_admission(command.admission_key, command.request_hash)
            self.assertIsNotNone(replay)
            self.assertFalse(replay.dispatch_required)
            second = repo.admit(command)
            self.assertFalse(second.dispatch_required)
            self.assertEqual(first.run.run_id, second.run.run_id)
            with self.assertRaises(RunAdmissionRequestHashConflictError):
                repo.lookup_admission(command.admission_key, "other-hash")

    def test_admission_is_retained_at_exactly_twenty_four_hours(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo = _repo(
                temp_dir,
                uuids=[SESSION_ID, RUN_ID],
                clock=["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"],
            )
            _create_session(repo)
            command = _start_command()
            first = repo.admit(command)
            replay = repo.lookup_admission(command.admission_key, command.request_hash)
            self.assertIsNotNone(replay)
            self.assertEqual(replay.run.run_id, first.run.run_id)

    def test_admit_rejects_second_active_run_for_session(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo = _repo(temp_dir, uuids=[SESSION_ID, RUN_ID, RUN_ID_2])
            _create_session(repo)
            repo.admit(_start_command())
            with self.assertRaises(RunStateConflictError):
                repo.admit(
                    _start_command(
                        admission_key=RunAdmissionKey(
                            principal_id=PRINCIPAL_ID,
                            operation_id=OPERATION_ID,
                            idempotency_key="idem-2",
                        ),
                        request_hash="hash-2",
                    )
                )

    def test_captured_route_survives_without_registration_lookup(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo = _repo(temp_dir, uuids=[SESSION_ID, RUN_ID])
            _create_session(repo)
            admitted = repo.admit(_start_command())
            loaded = repo.get(GetRunCommand(run_id=admitted.run.run_id))
            self.assertEqual(
                loaded.route_snapshot.provider_model_id,
                "provider/captured-model",
            )
            self.assertTrue(str(loaded.route_snapshot.credential_ref).startswith("ref:"))
            recovery = repo.recover_after_restart("2026-01-01T00:01:00Z")
            self.assertEqual(len(recovery.dispatchable_requests), 1)
            request = recovery.dispatchable_requests[0]
            self.assertEqual(request.route_snapshot.provider_model_id, "provider/captured-model")

    def test_claim_dispatch_is_single_use(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo = _repo(temp_dir, uuids=[SESSION_ID, RUN_ID])
            _create_session(repo)
            admitted = repo.admit(_start_command())
            claimed = repo.claim_dispatch(
                ClaimDispatchCommand(run_id=admitted.run.run_id, dispatch_token=admitted.run.run_id)
            )
            self.assertEqual(claimed.state, RunState.RUNNING)
            with self.assertRaises(RunDispatchClaimError):
                repo.claim_dispatch(
                    ClaimDispatchCommand(run_id=admitted.run.run_id, dispatch_token=admitted.run.run_id)
                )

    def test_application_events_are_monotonic(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo = _repo(temp_dir, uuids=[SESSION_ID, RUN_ID])
            _create_session(repo)
            admitted = repo.admit(_start_command())
            repo.claim_dispatch(
                ClaimDispatchCommand(run_id=admitted.run.run_id, dispatch_token=admitted.run.run_id)
            )
            first = repo.append_application_event(
                AppendApplicationEventCommand(
                    run_id=admitted.run.run_id,
                    expected_state=ActiveRunState.RUNNING,
                    new_state=ActiveRunState.RUNNING,
                    kind="run.started",
                )
            )
            second = repo.append_application_event(
                AppendApplicationEventCommand(
                    run_id=admitted.run.run_id,
                    expected_state=ActiveRunState.RUNNING,
                    new_state=ActiveRunState.RUNNING,
                    kind="content.delta",
                    payload={"delta": "a"},
                )
            )
            self.assertEqual(first.event.sequence, 1)
            self.assertEqual(second.event.sequence, 2)
            conn = sqlite3.connect(Path(temp_dir) / "state.sqlite3")
            try:
                stored_payload = conn.execute(
                    "SELECT payload_json FROM run_application_events "
                    "WHERE run_id = ? AND sequence = ?",
                    (admitted.run.run_id, second.event.sequence),
                ).fetchone()[0]
                self.assertIsNone(stored_payload)
                self.assertEqual(second.event.payload, {"delta": "a"})
            finally:
                conn.close()

    def test_submit_tool_result_auth_and_idempotency(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo = _repo(temp_dir, uuids=[SESSION_ID, RUN_ID])
            _create_session(repo)
            admitted = repo.admit(_start_command())
            run_id = admitted.run.run_id
            repo.claim_dispatch(ClaimDispatchCommand(run_id=run_id, dispatch_token=run_id))
            repo.append_application_event(
                AppendApplicationEventCommand(
                    run_id=run_id,
                    expected_state=ActiveRunState.RUNNING,
                    new_state=ActiveRunState.WAITING_FOR_TOOL,
                    kind="tool.requested",
                    payload={"call_id": "call-1", "tool_name": "search"},
                )
            )
            with self.assertRaises(RunAuthorizationMismatchError):
                repo.submit_tool_result(
                    SubmitToolResultCommand(
                        run_id=run_id,
                        call_id="call-1",
                        idempotency_key="tool-1",
                        result={"ok": True},
                        principal_id="other-principal",
                        host_context_ref=HOST_CTX,
                    )
                )
            first = repo.submit_tool_result(
                SubmitToolResultCommand(
                    run_id=run_id,
                    call_id="call-1",
                    idempotency_key="tool-1",
                    result={"ok": True},
                    principal_id=PRINCIPAL_ID,
                    host_context_ref=HOST_CTX,
                )
            )
            self.assertTrue(first.provider_submission_required)
            replay = repo.submit_tool_result(
                SubmitToolResultCommand(
                    run_id=run_id,
                    call_id="call-1",
                    idempotency_key="tool-1",
                    result={"ok": True},
                    principal_id=PRINCIPAL_ID,
                    host_context_ref=HOST_CTX,
                )
            )
            self.assertFalse(replay.provider_submission_required)
            with self.assertRaises(ToolResultIdempotencyConflictError):
                repo.submit_tool_result(
                    SubmitToolResultCommand(
                        run_id=run_id,
                        call_id="call-1",
                        idempotency_key="tool-1",
                        result={"ok": False},
                        principal_id=PRINCIPAL_ID,
                        host_context_ref=HOST_CTX,
                    )
                )

    def test_cancel_and_complete_race_yields_one_terminal(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            repo_a = _repo(temp_dir, uuids=[SESSION_ID, RUN_ID])
            _create_session(repo_a)
            admitted = repo_a.admit(_start_command())
            run_id = admitted.run.run_id
            repo_a.claim_dispatch(ClaimDispatchCommand(run_id=run_id, dispatch_token=run_id))
            repo_b = _repo(temp_dir, uuids=[SESSION_ID, RUN_ID])
            barrier = threading.Barrier(2)
            outcomes: list[str] = []

            def cancel() -> None:
                barrier.wait()
                try:
                    repo_a.request_cancel(
                        CancelRunCommand(run_id=run_id, idempotency_key="cancel-1")
                    )
                    outcomes.append("cancel")
                except Exception as exc:
                    outcomes.append(f"cancel:{type(exc).__name__}")

            def complete() -> None:
                barrier.wait()
                try:
                    repo_b.complete_terminal(
                        CompleteTerminalCommand(
                            run_id=run_id,
                            expected_state=ActiveRunState.RUNNING,
                            terminal_result=TerminalResult(outcome=TerminalOutcome.COMPLETED),
                            final_event_kind="run.completed",
                        )
                    )
                    outcomes.append("complete")
                except Exception as exc:
                    outcomes.append(f"complete:{type(exc).__name__}")

            t1 = threading.Thread(target=cancel)
            t2 = threading.Thread(target=complete)
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            final = repo_b.get(GetRunCommand(run_id=run_id))
            self.assertIn(final.state, {RunState.CANCELLING, RunState.COMPLETED, RunState.CANCELLED})
            self.assertEqual(len({o.split(":")[0] for o in outcomes}), 2)

    def test_recovery_reconstructs_accepted_and_interrupts_claimed(self) -> None:
        with TemporaryDirectory() as temp_dir:
            session_two = "550e8400-e29b-41d4-a716-446655440012"
            run_two = "550e8400-e29b-41d4-a716-446655440013"
            repo = _repo(temp_dir, uuids=[SESSION_ID, RUN_ID, session_two, run_two])
            _create_session(repo)
            second_session = repo.create(
                CreateSessionCommand(registration_id=REGISTRATION_ID, host_context_ref=HOST_CTX)
            )
            accepted = repo.admit(_start_command())
            claimed = repo.admit(
                _start_command(
                    session_id=second_session.session_id,
                    admission_key=RunAdmissionKey(
                        principal_id=PRINCIPAL_ID,
                        operation_id=OPERATION_ID,
                        idempotency_key="idem-2",
                    ),
                    request_hash="hash-2",
                )
            )
            repo.claim_dispatch(
                ClaimDispatchCommand(run_id=claimed.run.run_id, dispatch_token=claimed.run.run_id)
            )
            recovery = repo.recover_after_restart("2026-01-02T00:00:00Z")
            self.assertEqual(recovery.dispatchable_requests[0].run_id, accepted.run.run_id)
            self.assertEqual(recovery.interrupted_run_ids, (claimed.run.run_id,))
            interrupted = repo.get(GetRunCommand(run_id=claimed.run.run_id))
            self.assertEqual(interrupted.state, RunState.INTERRUPTED)

    def test_completion_rejects_outstanding_tool(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo = _repo(temp_dir, uuids=[SESSION_ID, RUN_ID])
            _create_session(repo)
            admitted = repo.admit(_start_command())
            run_id = admitted.run.run_id
            repo.claim_dispatch(ClaimDispatchCommand(run_id=run_id, dispatch_token=run_id))
            repo.append_application_event(
                AppendApplicationEventCommand(
                    run_id=run_id,
                    expected_state=ActiveRunState.RUNNING,
                    new_state=ActiveRunState.WAITING_FOR_TOOL,
                    kind="tool.requested",
                    payload={"call_id": "call-1", "tool_name": "search"},
                )
            )
            with self.assertRaises(RunTerminalConflictError):
                repo.complete_terminal(
                    CompleteTerminalCommand(
                        run_id=run_id,
                        expected_state=ActiveRunState.WAITING_FOR_TOOL,
                        terminal_result=TerminalResult(outcome=TerminalOutcome.COMPLETED),
                        final_event_kind="run.completed",
                    )
                )

    def test_admit_concurrency_returns_single_dispatch_required(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo = _repo(temp_dir, uuids=[SESSION_ID, RUN_ID, RUN_ID_2, RUN_ID_2])
            _create_session(repo)
            command = _start_command()
            dispatch_flags: list[bool] = []

            def worker() -> None:
                worker_repo = _repo(temp_dir, uuids=[SESSION_ID, RUN_ID, RUN_ID_2, RUN_ID_2])
                result = worker_repo.admit(command)
                dispatch_flags.append(result.dispatch_required)

            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(worker) for _ in range(4)]
                for future in as_completed(futures):
                    future.result()
            self.assertEqual(dispatch_flags.count(True), 1)
            self.assertEqual(len(set(dispatch_flags)), 2)

    def test_schema_stores_refs_not_secret_values(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            repo = _repo(temp_dir, uuids=[SESSION_ID, RUN_ID])
            _create_session(repo)
            repo.admit(_start_command())
            conn = sqlite3.connect(db_path)
            try:
                forbidden = {"credential_value", "endpoint_config", "secret", "token_value"}
                for table in (
                    "sessions",
                    "runs",
                    "run_admissions",
                    "run_application_events",
                    "run_outstanding_tool_calls",
                    "run_tool_result_receipts",
                    "run_cancel_idempotency",
                ):
                    columns = {
                        row[1].lower()
                        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                    }
                    self.assertTrue(forbidden.isdisjoint(columns), table)
                blob = conn.execute(
                    "SELECT route_snapshot_json FROM runs LIMIT 1"
                ).fetchone()[0]
                self.assertNotIn("sk-live", blob)
                self.assertIn("ref:credential.token", blob)
            finally:
                conn.close()

    def test_rollback_injection_surfaces_without_persisting(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"

            class FailingConnection:
                def __init__(self, inner: sqlite3.Connection) -> None:
                    self._inner = inner

                def __getattr__(self, name: str) -> Any:
                    return getattr(self._inner, name)

                def commit(self) -> None:
                    self._inner.rollback()
                    raise sqlite3.OperationalError("injected rollback")

            def failing_connect(path: Path) -> sqlite3.Connection:
                path.parent.mkdir(parents=True, exist_ok=True)
                inner = sqlite3.connect(path)
                inner.execute("PRAGMA foreign_keys = ON")
                return FailingConnection(inner)  # type: ignore[return-value]

            repo = _repo(temp_dir, uuids=[SESSION_ID, RUN_ID], connect=failing_connect)
            with self.assertRaises(sqlite3.OperationalError):
                repo.create(CreateSessionCommand(registration_id=REGISTRATION_ID))
            conn = sqlite3.connect(db_path)
            try:
                count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
                self.assertEqual(count, 0)
            finally:
                conn.close()

    def test_get_session_not_found(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo = _repo(temp_dir)
            with self.assertRaises(SessionNotFoundError):
                repo.get(GetSessionCommand(session_id=SESSION_ID))

    def test_get_run_not_found(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo = _repo(temp_dir)
            with self.assertRaises(RunNotFoundError):
                repo.get(GetRunCommand(run_id=RUN_ID))


if __name__ == "__main__":
    unittest.main()

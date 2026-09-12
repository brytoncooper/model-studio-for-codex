from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from model_deck.adapters.storage.sqlite_outbox import ensure_projection_outbox_schema
from model_deck.engine.routing.ports import (
    CapabilityFeature,
    CapabilityTriState,
    ContinuationScope,
    ExecutionMode,
    RouteSnapshot,
)
from model_deck.engine.runs.ports import (
    ADMISSION_RECORD_RETENTION_MIN_HOURS,
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
    RestartRecoveryResult,
    RunAdmissionKey,
    RunAdmissionRequestHashConflictError,
    RunAdmissionResult,
    RunAuthorizationMismatchError,
    RunDispatchClaimError,
    RunNotFoundError,
    RunRecord,
    RunRequest,
    RunState,
    RunStateConflictError,
    RunTerminalConflictError,
    StartRunCommand,
    SubmitToolResultCommand,
    SubmitToolResultResult,
    TERMINAL_RUN_STATES,
    TerminalOutcome,
    TerminalResult,
    ToolCallDescriptor,
    ToolCallNotOutstandingError,
    ToolResultIdempotencyConflictError,
)
from model_deck.engine.sessions.ports import (
    CreateSessionCommand,
    GetSessionCommand,
    SelectModelCommand,
    SessionActiveRunConflictError,
    SessionNotFoundError,
    SessionRecord,
    SessionRevisionConflictError,
)

_EVENT_SCHEMA_VERSION = 1
_ACTIVE_RUN_STATES: frozenset[str] = frozenset(
    {
        RunState.ACCEPTED.value,
        RunState.RUNNING.value,
        RunState.WAITING_FOR_TOOL.value,
        RunState.CANCELLING.value,
    }
)

_NONTERMINAL_EVENT_KINDS: frozenset[str] = frozenset(
    {
        "run.started",
        "content.delta",
        "tool.requested",
        "usage.observed",
        "run.cancelling",
    }
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    registration_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    host_context_ref TEXT,
    continuation_scope_json TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    state TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    registration_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    authorized_host_context_ref TEXT,
    route_snapshot_json TEXT NOT NULL,
    input_json TEXT NOT NULL,
    tools_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    last_sequence INTEGER NOT NULL DEFAULT 0,
    terminal_outcome TEXT,
    terminal_error_json TEXT,
    dispatch_claimed INTEGER NOT NULL DEFAULT 0,
    dispatch_token TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
CREATE INDEX IF NOT EXISTS idx_runs_session_state ON runs(session_id, state);
CREATE TABLE IF NOT EXISTS run_admissions (
    principal_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    run_id TEXT NOT NULL,
    admitted_at TEXT NOT NULL,
    PRIMARY KEY (principal_id, operation_id, idempotency_key),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
CREATE TABLE IF NOT EXISTS run_application_events (
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    kind TEXT NOT NULL,
    event_schema_version INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    payload_json TEXT,
    new_state TEXT NOT NULL,
    PRIMARY KEY (run_id, sequence),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
CREATE TABLE IF NOT EXISTS run_outstanding_tool_calls (
    run_id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_json TEXT,
    submitted INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
CREATE TABLE IF NOT EXISTS run_tool_result_receipts (
    run_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY (run_id, call_id, idempotency_key),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
CREATE TABLE IF NOT EXISTS run_cancel_idempotency (
    run_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    applied INTEGER NOT NULL,
    PRIMARY KEY (run_id, idempotency_key),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
"""


class SQLiteSessionRunRepository:
    def __init__(
        self,
        db_path: Path,
        *,
        uuid_factory: Callable[[], str] | None = None,
        utc_clock: Callable[[], str] | None = None,
        connect: Callable[[Path], sqlite3.Connection] | None = None,
    ) -> None:
        self._db_path = db_path
        self._uuid_factory = uuid_factory or (lambda: str(uuid.uuid4()))
        self._utc_clock = utc_clock or (lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self._connect_factory = connect or self._default_connect

    def create(self, command: CreateSessionCommand) -> SessionRecord:
        conn = self._connect_factory(self._db_path)
        try:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                session_id = self._new_uuid()
                conn.execute(
                    "INSERT INTO sessions (session_id, registration_id, revision, host_context_ref, continuation_scope_json) "
                    "VALUES (?, ?, ?, ?, NULL)",
                    (
                        session_id,
                        command.registration_id,
                        1,
                        command.host_context_ref,
                    ),
                )
                conn.commit()
                return SessionRecord(
                    session_id=session_id,
                    registration_id=command.registration_id,
                    revision=1,
                    host_context_ref=command.host_context_ref,
                )
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def select_model(self, command: SelectModelCommand) -> SessionRecord:
        conn = self._connect_factory(self._db_path)
        try:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT session_id, registration_id, revision, host_context_ref, continuation_scope_json "
                    "FROM sessions WHERE session_id = ?",
                    (command.session_id,),
                ).fetchone()
                if row is None:
                    raise SessionNotFoundError(f"session {command.session_id} not found")
                if row[2] != command.expected_revision:
                    raise SessionRevisionConflictError("stale session revision")
                active = conn.execute(
                    "SELECT run_id FROM runs WHERE session_id = ? AND state IN (?, ?, ?, ?)",
                    (
                        command.session_id,
                        RunState.ACCEPTED.value,
                        RunState.RUNNING.value,
                        RunState.WAITING_FOR_TOOL.value,
                        RunState.CANCELLING.value,
                    ),
                ).fetchone()
                if active is not None:
                    raise SessionActiveRunConflictError("session has an active run")
                new_revision = row[2] + 1
                continuation_json = None if command.continuation_reset else row[4]
                conn.execute(
                    "UPDATE sessions SET registration_id = ?, revision = ?, continuation_scope_json = ? "
                    "WHERE session_id = ?",
                    (
                        command.registration_id,
                        new_revision,
                        continuation_json,
                        command.session_id,
                    ),
                )
                conn.commit()
                return SessionRecord(
                    session_id=row[0],
                    registration_id=command.registration_id,
                    revision=new_revision,
                    host_context_ref=row[3],
                    continuation_scope=_deserialize_continuation(row[4])
                    if not command.continuation_reset
                    else None,
                )
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def lookup_admission(
        self, admission_key: RunAdmissionKey, request_hash: str
    ) -> RunAdmissionResult | None:
        conn = self._connect_factory(self._db_path)
        try:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT request_hash, run_id, admitted_at FROM run_admissions "
                "WHERE principal_id = ? AND operation_id = ? AND idempotency_key = ?",
                (
                    admission_key.principal_id,
                    admission_key.operation_id,
                    admission_key.idempotency_key,
                ),
            ).fetchone()
            if row is None:
                return None
            stored_hash, run_id, admitted_at = row
            if not _admission_retained(admitted_at, self._utc_clock()):
                return None
            if stored_hash != request_hash:
                raise RunAdmissionRequestHashConflictError
            run = self._load_run(conn, run_id)
            return RunAdmissionResult(run=run, dispatch_required=False)
        finally:
            conn.close()

    def admit(self, command: StartRunCommand) -> RunAdmissionResult:
        conn = self._connect_factory(self._db_path)
        try:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT request_hash, run_id, admitted_at FROM run_admissions "
                    "WHERE principal_id = ? AND operation_id = ? AND idempotency_key = ?",
                    (
                        command.admission_key.principal_id,
                        command.admission_key.operation_id,
                        command.admission_key.idempotency_key,
                    ),
                ).fetchone()
                if existing is not None:
                    stored_hash, run_id, admitted_at = existing
                    if not _admission_retained(admitted_at, self._utc_clock()):
                        conn.execute(
                            "DELETE FROM run_admissions WHERE principal_id = ? AND operation_id = ? AND idempotency_key = ?",
                            (
                                command.admission_key.principal_id,
                                command.admission_key.operation_id,
                                command.admission_key.idempotency_key,
                            ),
                        )
                    elif stored_hash != command.request_hash:
                        raise RunAdmissionRequestHashConflictError
                    else:
                        run = self._load_run(conn, run_id)
                        conn.commit()
                        return RunAdmissionResult(run=run, dispatch_required=False)
                active = conn.execute(
                    "SELECT run_id FROM runs WHERE session_id = ? AND state IN (?, ?, ?, ?)",
                    (
                        command.session_id,
                        RunState.ACCEPTED.value,
                        RunState.RUNNING.value,
                        RunState.WAITING_FOR_TOOL.value,
                        RunState.CANCELLING.value,
                    ),
                ).fetchone()
                if active is not None:
                    raise RunStateConflictError("session already has an active run")
                run_id = self._new_uuid()
                observed_at = self._utc_clock()
                route_json = _serialize_route_snapshot(command.route_snapshot)
                input_json = _serialize_input(command.input)
                tools_json = _serialize_tools(command.tools)
                conn.execute(
                    "INSERT INTO runs (run_id, session_id, state, client_request_id, registration_id, "
                    "principal_id, authorized_host_context_ref, route_snapshot_json, input_json, tools_json, "
                    "idempotency_key, last_sequence, terminal_outcome, terminal_error_json, dispatch_claimed, dispatch_token) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, 0, NULL)",
                    (
                        run_id,
                        command.session_id,
                        RunState.ACCEPTED.value,
                        command.client_request_id,
                        command.registration_id,
                        command.admission_key.principal_id,
                        command.authorized_host_context_ref,
                        route_json,
                        input_json,
                        tools_json,
                        command.admission_key.idempotency_key,
                    ),
                )
                conn.execute(
                    "INSERT INTO run_application_events (run_id, sequence, kind, event_schema_version, observed_at, payload_json, new_state) "
                    "VALUES (?, 0, ?, ?, ?, NULL, ?)",
                    (
                        run_id,
                        "run.accepted",
                        _EVENT_SCHEMA_VERSION,
                        observed_at,
                        RunState.ACCEPTED.value,
                    ),
                )
                conn.execute(
                    "UPDATE runs SET last_sequence = 0 WHERE run_id = ?",
                    (run_id,),
                )
                conn.execute(
                    "INSERT INTO run_admissions (principal_id, operation_id, idempotency_key, request_hash, run_id, admitted_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        command.admission_key.principal_id,
                        command.admission_key.operation_id,
                        command.admission_key.idempotency_key,
                        command.request_hash,
                        run_id,
                        observed_at,
                    ),
                )
                run = self._load_run(conn, run_id)
                conn.commit()
                return RunAdmissionResult(run=run, dispatch_required=True)
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def claim_dispatch(self, command: ClaimDispatchCommand) -> RunRecord:
        conn = self._connect_factory(self._db_path)
        try:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT state, dispatch_claimed, dispatch_token FROM runs WHERE run_id = ?",
                    (command.run_id,),
                ).fetchone()
                if row is None:
                    raise RunNotFoundError(f"run {command.run_id} not found")
                state, claimed, token = row
                if state in {s.value for s in TERMINAL_RUN_STATES}:
                    raise RunDispatchClaimError("run is terminal")
                if claimed:
                    raise RunDispatchClaimError("dispatch already claimed")
                if state != RunState.ACCEPTED.value:
                    raise RunDispatchClaimError("run is not accepted")
                updated = conn.execute(
                    "UPDATE runs SET state = ?, dispatch_claimed = 1, dispatch_token = ? "
                    "WHERE run_id = ? AND state = ? AND dispatch_claimed = 0",
                    (
                        RunState.RUNNING.value,
                        command.dispatch_token,
                        command.run_id,
                        RunState.ACCEPTED.value,
                    ),
                )
                if updated.rowcount != 1:
                    raise RunDispatchClaimError("dispatch claim lost race")
                conn.commit()
                return self._load_run(conn, command.run_id)
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def append_application_event(
        self, command: AppendApplicationEventCommand
    ) -> AppendApplicationEventResult:
        conn = self._connect_factory(self._db_path)
        try:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                run = self._load_run(conn, command.run_id)
                if run.state in TERMINAL_RUN_STATES:
                    raise RunStateConflictError("run is terminal")
                current_active = _ACTIVE_STATE_BY_RUN_STATE[run.state]
                if current_active != command.expected_state:
                    raise RunStateConflictError("unexpected run state")
                if command.kind not in _NONTERMINAL_EVENT_KINDS:
                    raise RunStateConflictError(f"unsupported event kind: {command.kind}")
                resolved = _resolve_transition(
                    command.expected_state, command.kind, command.new_state
                )
                if resolved != command.new_state:
                    raise RunStateConflictError("invalid state transition")
                observed_at = command.observed_at or self._utc_clock()
                next_sequence = run.last_sequence + 1
                if command.kind == "tool.requested":
                    outstanding = conn.execute(
                        "SELECT call_id FROM run_outstanding_tool_calls WHERE run_id = ?",
                        (command.run_id,),
                    ).fetchone()
                    if outstanding is not None:
                        raise RunStateConflictError("tool call already outstanding")
                    call_id, tool_name, arguments = _tool_request_fields(command.payload)
                    conn.execute(
                        "INSERT INTO run_outstanding_tool_calls (run_id, call_id, tool_name, arguments_json, submitted) "
                        "VALUES (?, ?, ?, ?, 0)",
                        (
                            command.run_id,
                            call_id,
                            tool_name,
                            _canonical_json(arguments) if arguments is not None else None,
                        ),
                    )
                conn.execute(
                    "INSERT INTO run_application_events (run_id, sequence, kind, event_schema_version, observed_at, payload_json, new_state) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        command.run_id,
                        next_sequence,
                        command.kind,
                        _EVENT_SCHEMA_VERSION,
                        observed_at,
                        _persisted_event_payload(command.kind, command.payload),
                        resolved.value,
                    ),
                )
                conn.execute(
                    "UPDATE runs SET state = ?, last_sequence = ? WHERE run_id = ?",
                    (resolved.value, next_sequence, command.run_id),
                )
                updated = self._load_run(conn, command.run_id)
                event = ApplicationRunEvent(
                    kind=command.kind,
                    run_id=command.run_id,
                    session_id=updated.session_id,
                    sequence=next_sequence,
                    event_schema_version=_EVENT_SCHEMA_VERSION,
                    observed_at=observed_at,
                    payload=command.payload,
                )
                conn.commit()
                return AppendApplicationEventResult(run=updated, event=event)
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def request_cancel(self, command: CancelRunCommand) -> CancelRunResult:
        conn = self._connect_factory(self._db_path)
        try:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                run = self._load_run(conn, command.run_id)
                if run.state in TERMINAL_RUN_STATES:
                    conn.commit()
                    return CancelRunResult(run=run, event=None)
                prior = conn.execute(
                    "SELECT applied FROM run_cancel_idempotency WHERE run_id = ? AND idempotency_key = ?",
                    (command.run_id, command.idempotency_key),
                ).fetchone()
                if prior is not None and prior[0]:
                    conn.commit()
                    return CancelRunResult(
                        run=self._load_run(conn, command.run_id),
                        event=None,
                    )
                observed_at = self._utc_clock()
                event = None
                if run.state != RunState.CANCELLING.value:
                    next_sequence = run.last_sequence + 1
                    conn.execute(
                        "INSERT INTO run_application_events (run_id, sequence, kind, event_schema_version, observed_at, payload_json, new_state) "
                        "VALUES (?, ?, ?, ?, ?, NULL, ?)",
                        (
                            command.run_id,
                            next_sequence,
                            "run.cancelling",
                            _EVENT_SCHEMA_VERSION,
                            observed_at,
                            RunState.CANCELLING.value,
                        ),
                    )
                    conn.execute(
                        "UPDATE runs SET state = ?, last_sequence = ? WHERE run_id = ?",
                        (RunState.CANCELLING.value, next_sequence, command.run_id),
                    )
                    event = ApplicationRunEvent(
                        kind="run.cancelling",
                        run_id=command.run_id,
                        session_id=run.session_id,
                        sequence=next_sequence,
                        event_schema_version=_EVENT_SCHEMA_VERSION,
                        observed_at=observed_at,
                    )
                conn.execute(
                    "INSERT INTO run_cancel_idempotency (run_id, idempotency_key, applied) VALUES (?, ?, 1) "
                    "ON CONFLICT(run_id, idempotency_key) DO UPDATE SET applied = 1",
                    (command.run_id, command.idempotency_key),
                )
                conn.commit()
                return CancelRunResult(
                    run=self._load_run(conn, command.run_id),
                    event=event,
                )
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def submit_tool_result(
        self, command: SubmitToolResultCommand
    ) -> SubmitToolResultResult:
        conn = self._connect_factory(self._db_path)
        try:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                run = self._load_run(conn, command.run_id)
                if run.principal_id != command.principal_id:
                    raise RunAuthorizationMismatchError
                if run.authorized_host_context_ref != command.host_context_ref:
                    raise RunAuthorizationMismatchError
                receipt = conn.execute(
                    "SELECT result_json FROM run_tool_result_receipts "
                    "WHERE run_id = ? AND call_id = ? AND idempotency_key = ?",
                    (command.run_id, command.call_id, command.idempotency_key),
                ).fetchone()
                if receipt is not None:
                    stored = json.loads(receipt[0])
                    if stored != command.result:
                        raise ToolResultIdempotencyConflictError
                    conn.commit()
                    return SubmitToolResultResult(
                        run=self._load_run(conn, command.run_id),
                        provider_submission_required=False,
                    )
                outstanding = conn.execute(
                    "SELECT call_id, submitted FROM run_outstanding_tool_calls WHERE run_id = ?",
                    (command.run_id,),
                ).fetchone()
                if outstanding is None or outstanding[0] != command.call_id:
                    raise ToolCallNotOutstandingError
                if outstanding[1]:
                    raise ToolCallNotOutstandingError
                conn.execute(
                    "INSERT INTO run_tool_result_receipts (run_id, call_id, idempotency_key, result_json) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        command.run_id,
                        command.call_id,
                        command.idempotency_key,
                        _canonical_json(command.result),
                    ),
                )
                conn.execute(
                    "UPDATE run_outstanding_tool_calls SET submitted = 1 WHERE run_id = ?",
                    (command.run_id,),
                )
                if run.state == RunState.WAITING_FOR_TOOL:
                    conn.execute(
                        "UPDATE runs SET state = ? WHERE run_id = ?",
                        (RunState.RUNNING.value, command.run_id),
                    )
                conn.execute(
                    "DELETE FROM run_outstanding_tool_calls WHERE run_id = ?",
                    (command.run_id,),
                )
                updated = self._load_run(conn, command.run_id)
                conn.commit()
                return SubmitToolResultResult(
                    run=updated,
                    provider_submission_required=True,
                )
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def complete_terminal(self, command: CompleteTerminalCommand) -> CompleteTerminalResult:
        conn = self._connect_factory(self._db_path)
        try:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                run = self._load_run(conn, command.run_id)
                if run.state in TERMINAL_RUN_STATES:
                    raise RunTerminalConflictError("run already terminal")
                current_active = _ACTIVE_STATE_BY_RUN_STATE[run.state]
                if current_active != command.expected_state:
                    raise RunStateConflictError("unexpected run state")
                outstanding = conn.execute(
                    "SELECT call_id FROM run_outstanding_tool_calls WHERE run_id = ?",
                    (command.run_id,),
                ).fetchone()
                if outstanding is not None:
                    if command.terminal_result.outcome == TerminalOutcome.COMPLETED:
                        raise RunTerminalConflictError("outstanding tool call")
                    conn.execute(
                        "DELETE FROM run_outstanding_tool_calls WHERE run_id = ?",
                        (command.run_id,),
                    )
                observed_at = command.observed_at or self._utc_clock()
                next_sequence = run.last_sequence + 1
                terminal_state = RunState(command.terminal_result.outcome.value)
                conn.execute(
                    "INSERT INTO run_application_events (run_id, sequence, kind, event_schema_version, observed_at, payload_json, new_state) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        command.run_id,
                        next_sequence,
                        command.final_event_kind,
                        _EVENT_SCHEMA_VERSION,
                        observed_at,
                        _canonical_json(command.final_event_payload)
                        if command.final_event_payload is not None
                        else None,
                        terminal_state.value,
                    ),
                )
                conn.execute(
                    "UPDATE runs SET state = ?, last_sequence = ?, terminal_outcome = ?, terminal_error_json = ? "
                    "WHERE run_id = ?",
                    (
                        terminal_state.value,
                        next_sequence,
                        command.terminal_result.outcome.value,
                        _canonical_json(command.terminal_result.error)
                        if command.terminal_result.error is not None
                        else None,
                        command.run_id,
                    ),
                )
                event = ApplicationRunEvent(
                    kind=command.final_event_kind,
                    run_id=command.run_id,
                    session_id=run.session_id,
                    sequence=next_sequence,
                    event_schema_version=_EVENT_SCHEMA_VERSION,
                    observed_at=observed_at,
                    payload=command.final_event_payload,
                )
                conn.commit()
                return CompleteTerminalResult(
                    run=self._load_run(conn, command.run_id),
                    event=event,
                )
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def get(
        self, command: GetSessionCommand | GetRunCommand
    ) -> SessionRecord | RunRecord:
        if isinstance(command, GetRunCommand):
            conn = self._connect_factory(self._db_path)
            try:
                self._ensure_schema(conn)
                return self._load_run(conn, command.run_id)
            finally:
                conn.close()
        if isinstance(command, GetSessionCommand):
            conn = self._connect_factory(self._db_path)
            try:
                self._ensure_schema(conn)
                row = conn.execute(
                    "SELECT session_id, registration_id, revision, host_context_ref, continuation_scope_json "
                    "FROM sessions WHERE session_id = ?",
                    (command.session_id,),
                ).fetchone()
                if row is None:
                    raise SessionNotFoundError(f"session {command.session_id} not found")
                return _session_from_row(row)
            finally:
                conn.close()
        raise TypeError("unsupported get command")

    def recover_after_restart(self, observed_at: str) -> RestartRecoveryResult:
        conn = self._connect_factory(self._db_path)
        try:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                dispatchable: list[RunRequest] = []
                rows = conn.execute(
                    "SELECT run_id, session_id, client_request_id, idempotency_key, route_snapshot_json, input_json, tools_json "
                    "FROM runs WHERE state = ? AND dispatch_claimed = 0",
                    (RunState.ACCEPTED.value,),
                ).fetchall()
                for row in rows:
                    dispatchable.append(
                        RunRequest(
                            run_id=row[0],
                            session_id=row[1],
                            client_request_id=row[2],
                            idempotency_key=row[3],
                            route_snapshot=_deserialize_route_snapshot(row[4]),
                            input=_deserialize_input(row[5]),
                            tools=_deserialize_tools(row[6]),
                        )
                    )
                interrupted: list[str] = []
                active_rows = conn.execute(
                    "SELECT run_id, last_sequence FROM runs WHERE dispatch_claimed = 1 AND state IN (?, ?, ?)",
                    (
                        RunState.RUNNING.value,
                        RunState.WAITING_FOR_TOOL.value,
                        RunState.CANCELLING.value,
                    ),
                ).fetchall()
                for run_id, last_sequence in active_rows:
                    next_sequence = last_sequence + 1
                    payload = {
                        "terminal_result": {
                            "outcome": TerminalOutcome.INTERRUPTED.value,
                        }
                    }
                    conn.execute(
                        "INSERT INTO run_application_events (run_id, sequence, kind, event_schema_version, observed_at, payload_json, new_state) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            run_id,
                            next_sequence,
                            "run.interrupted",
                            _EVENT_SCHEMA_VERSION,
                            observed_at,
                            _canonical_json(payload),
                            RunState.INTERRUPTED.value,
                        ),
                    )
                    conn.execute(
                        "UPDATE runs SET state = ?, last_sequence = ?, terminal_outcome = ?, terminal_error_json = NULL "
                        "WHERE run_id = ?",
                        (
                            RunState.INTERRUPTED.value,
                            next_sequence,
                            TerminalOutcome.INTERRUPTED.value,
                            run_id,
                        ),
                    )
                    conn.execute(
                        "DELETE FROM run_outstanding_tool_calls WHERE run_id = ?",
                        (run_id,),
                    )
                    interrupted.append(run_id)
                conn.commit()
                return RestartRecoveryResult(
                    observed_at=observed_at,
                    dispatchable_requests=tuple(dispatchable),
                    interrupted_run_ids=tuple(interrupted),
                )
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()


    def _new_uuid(self) -> str:
        value = self._uuid_factory()
        if isinstance(value, str) and value:
            try:
                return str(uuid.UUID(value))
            except ValueError:
                pass
        return str(uuid.uuid4())

    def _default_connect(self, db_path: Path) -> sqlite3.Connection:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(_SCHEMA_SQL)
        ensure_projection_outbox_schema(conn)

    def _load_run(self, conn: sqlite3.Connection, run_id: str) -> RunRecord:
        row = conn.execute(
            "SELECT run_id, session_id, state, client_request_id, registration_id, principal_id, "
            "authorized_host_context_ref, route_snapshot_json, last_sequence, terminal_outcome, terminal_error_json "
            "FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise RunNotFoundError(f"run {run_id} not found")
        terminal_result = None
        if row[9] is not None:
            error = json.loads(row[10]) if row[10] is not None else None
            terminal_result = TerminalResult(
                outcome=TerminalOutcome(row[9]),
                error=error,
            )
        return RunRecord(
            run_id=row[0],
            session_id=row[1],
            state=RunState(row[2]),
            client_request_id=row[3],
            registration_id=row[4],
            route_snapshot=_deserialize_route_snapshot(row[7]),
            principal_id=row[5],
            authorized_host_context_ref=row[6],
            terminal_result=terminal_result,
            last_sequence=row[8],
        )


_ACTIVE_STATE_BY_RUN_STATE: dict[RunState, ActiveRunState] = {
    RunState.ACCEPTED: ActiveRunState.ACCEPTED,
    RunState.RUNNING: ActiveRunState.RUNNING,
    RunState.WAITING_FOR_TOOL: ActiveRunState.WAITING_FOR_TOOL,
    RunState.CANCELLING: ActiveRunState.CANCELLING,
}


def _resolve_transition(
    expected: ActiveRunState, kind: str, requested: ActiveRunState
) -> ActiveRunState:
    if kind == "run.started" and expected == ActiveRunState.ACCEPTED:
        return ActiveRunState.RUNNING
    if kind == "run.started" and expected == ActiveRunState.RUNNING:
        return ActiveRunState.RUNNING
    if kind == "tool.requested":
        return ActiveRunState.WAITING_FOR_TOOL
    if kind == "run.cancelling":
        return ActiveRunState.CANCELLING
    if kind in {"content.delta", "usage.observed"}:
        return expected
    raise RunStateConflictError(f"unsupported transition for {kind}")


def _tool_request_fields(payload: Any) -> tuple[str, str, Any]:
    if not isinstance(payload, dict):
        raise RunStateConflictError("tool.requested payload must be an object")
    call_id = payload.get("call_id")
    tool_name = payload.get("tool_name")
    if not isinstance(call_id, str) or not call_id:
        raise RunStateConflictError("tool.requested requires call_id")
    if not isinstance(tool_name, str) or not tool_name:
        raise RunStateConflictError("tool.requested requires tool_name")
    return call_id, tool_name, payload.get("arguments")


def _admission_retained(admitted_at: str, now_iso: str) -> bool:
    admitted = datetime.fromisoformat(admitted_at.replace("Z", "+00:00"))
    now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    return now - admitted <= timedelta(hours=ADMISSION_RECORD_RETENTION_MIN_HOURS)


def _session_from_row(row: tuple[Any, ...]) -> SessionRecord:
    return SessionRecord(
        session_id=row[0],
        registration_id=row[1],
        revision=row[2],
        host_context_ref=row[3],
        continuation_scope=_deserialize_continuation(row[4]),
    )


def _serialize_route_snapshot(snapshot: RouteSnapshot) -> str:
    payload: dict[str, Any] = {
        "registration_id": snapshot.registration_id,
        "registration_revision": snapshot.registration_revision,
        "connection_id": snapshot.connection_id,
        "connection_revision": snapshot.connection_revision,
        "provider_id": snapshot.provider_id,
        "provider_model_id": snapshot.provider_model_id,
        "execution_mode": snapshot.execution_mode.value,
        "endpoint_config_ref": snapshot.endpoint_config_ref,
        "credential_ref": snapshot.credential_ref,
        "capability_snapshot_ref": snapshot.capability_snapshot_ref,
    }
    if snapshot.capability_features is not None:
        payload["capability_features"] = [
            {"name": feature.name, "state": feature.state.value}
            for feature in snapshot.capability_features
        ]
    return _canonical_json(payload)


def _deserialize_route_snapshot(payload: str) -> RouteSnapshot:
    data = json.loads(payload)
    features = None
    if data.get("capability_features") is not None:
        features = tuple(
            CapabilityFeature(item["name"], CapabilityTriState(item["state"]))
            for item in data["capability_features"]
        )
    return RouteSnapshot(
        registration_id=data["registration_id"],
        registration_revision=data["registration_revision"],
        connection_id=data["connection_id"],
        connection_revision=data["connection_revision"],
        provider_id=data["provider_id"],
        provider_model_id=data["provider_model_id"],
        execution_mode=ExecutionMode(data["execution_mode"]),
        endpoint_config_ref=data.get("endpoint_config_ref"),
        credential_ref=data.get("credential_ref"),
        capability_snapshot_ref=data.get("capability_snapshot_ref"),
        capability_features=features,
    )


def _serialize_input(value: NormalizedRunInput) -> str:
    return _canonical_json({"messages": list(value.messages)})


def _deserialize_input(payload: str) -> NormalizedRunInput:
    data = json.loads(payload)
    messages = data.get("messages", [])
    if not isinstance(messages, list):
        raise ValueError("invalid stored input")
    return NormalizedRunInput(messages=tuple(messages))


def _serialize_tools(tools: tuple[ToolCallDescriptor, ...]) -> str:
    return _canonical_json(
        [
            {
                "call_id": tool.call_id,
                "tool_name": tool.tool_name,
                "arguments": tool.arguments,
            }
            for tool in tools
        ]
    )


def _deserialize_tools(payload: str) -> tuple[ToolCallDescriptor, ...]:
    data = json.loads(payload)
    if not isinstance(data, list):
        raise ValueError("invalid stored tools")
    return tuple(
        ToolCallDescriptor(
            call_id=item["call_id"],
            tool_name=item["tool_name"],
            arguments=item.get("arguments"),
        )
        for item in data
    )


def _serialize_continuation(scope: ContinuationScope | None) -> str | None:
    if scope is None:
        return None
    return _canonical_json(
        {
            "connection_id": scope.connection_id,
            "provider_model_id": scope.provider_model_id,
            "provider_id": scope.provider_id,
            "execution_mode": scope.execution_mode.value,
            "handle": scope.handle,
        }
    )


def _deserialize_continuation(payload: str | None) -> ContinuationScope | None:
    if payload is None:
        return None
    data = json.loads(payload)
    return ContinuationScope(
        connection_id=data["connection_id"],
        provider_model_id=data["provider_model_id"],
        provider_id=data["provider_id"],
        execution_mode=ExecutionMode(data["execution_mode"]),
        handle=data["handle"],
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _persisted_event_payload(kind: str, payload: Any) -> str | None:
    if payload is None or kind == "content.delta":
        return None
    return _canonical_json(payload)

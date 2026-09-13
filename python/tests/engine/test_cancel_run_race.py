"""Deterministic cancellation-versus-provider terminal races on real SQLite."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from model_deck.adapters.storage.sqlite_session_run_repository import SQLiteSessionRunRepository
from model_deck.engine.routing.ports import ExecutionMode, RouteSnapshot
from model_deck.engine.runs.ports import (
    CancelProviderRunResult, ClaimDispatchCommand, GetRunCommand, NormalizedRunInput,
    ProviderCancelTerminationStatus, ProviderRunEvent, RunAdmissionKey, RunState,
    RunStateConflictError, RunTerminalConflictError, StartRunCommand,
)
from model_deck.engine.runs.use_cases import CancelRunUseCase, RunApplicationCoordinator
from model_deck.engine.sessions.ports import CreateSessionCommand

NOW = "2026-09-12T00:00:00Z"
DEADLINE = "2026-09-12T00:01:00Z"


class ConfirmedHandle:
    def request_cancel(self, *, deadline):
        return CancelProviderRunResult(True, ProviderCancelTerminationStatus.CONFIRMED)


class Publications:
    def __init__(self):
        self.events = []

    def publish_application_event(self, event):
        self.events.append(event)


class CancelRunRaceTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "runs.sqlite3"
        self.repository = SQLiteSessionRunRepository(self.path, utc_clock=lambda: NOW)
        registration = str(uuid4())
        route = RouteSnapshot(registration, 1, str(uuid4()), 1, "org.example.fixture",
                              "fixture-model", ExecutionMode.CUSTOM)
        session = self.repository.create(CreateSessionCommand(registration))
        admission = self.repository.admit(StartRunCommand(
            RunAdmissionKey("operator", "engine.v1.runs.start", "start-1"), "request-hash",
            session.session_id, "client-1", registration, route, NormalizedRunInput()))
        self.run_id = admission.run.run_id
        self.repository.claim_dispatch(ClaimDispatchCommand(self.run_id, "dispatch-1"))
        self.publications = Publications()
        self.coordinator = RunApplicationCoordinator(self.repository, event_publisher=self.publications)
        self.coordinator.register_handle(self.run_id, ConfirmedHandle())
        self.cancel = CancelRunUseCase(self.repository, self.coordinator, cancel_deadline=DEADLINE)
        self.params = {"run_id": self.run_id, "idempotency_key": "cancel-1"}

    def race_provider_terminal(self, outcome):
        rendezvous = threading.Barrier(2, timeout=3)
        original = self.repository.complete_terminal

        def before_commit(command):
            if threading.current_thread().name.startswith("cancel-racer"):
                rendezvous.wait()  # Cancel has read a nonterminal and prepared its write.
                rendezvous.wait()  # Provider terminal must commit before cancel writes.
            return original(command)

        with patch.object(self.repository, "complete_terminal", side_effect=before_commit):
            with ThreadPoolExecutor(1, thread_name_prefix="cancel-racer") as pool:
                cancelled = pool.submit(self.cancel.execute, self.params)
                try:
                    rendezvous.wait()
                    self.coordinator.provider_sink().publish_provider_event(ProviderRunEvent(
                        "run." + outcome, self.run_id, NOW,
                        {"terminal_result": {"outcome": outcome}}))
                    rendezvous.wait()
                    result = cancelled.result(timeout=3)
                finally:
                    rendezvous.abort()
        self.assertEqual(result, {"accepted": True, "state": outcome})
        self.assertEqual(self.repository.get(GetRunCommand(self.run_id)).state.value, outcome)
        self.assertIsNone(self.coordinator.get_handle(self.run_id))
        terminals = [event.kind for event in self.publications.events if event.kind != "run.cancelling"]
        self.assertEqual(terminals, ["run." + outcome])
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute(
                "SELECT kind FROM run_application_events WHERE run_id = ? AND kind IN "
                "('run.cancelled','run.completed','run.failed','run.interrupted')", (self.run_id,)).fetchall()
        self.assertEqual(rows, [("run." + outcome,)])

    def test_provider_cancelled_wins_between_read_and_complete(self):
        self.race_provider_terminal("cancelled")

    def test_competing_completion_is_preserved(self):
        self.race_provider_terminal("completed")

    def test_terminal_conflict_without_persisted_terminal_is_not_hidden(self):
        failure = RunTerminalConflictError("fixture conflict")
        with patch.object(self.repository, "complete_terminal", side_effect=failure):
            with self.assertRaises(RunTerminalConflictError) as caught:
                self.cancel.execute(self.params)
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.repository.get(GetRunCommand(self.run_id)).state, RunState.CANCELLING)
        self.assertEqual([event.kind for event in self.publications.events], ["run.cancelling"])

    def test_unrelated_completion_failures_propagate(self):
        for failure in (RunStateConflictError("fixture state conflict"), OSError("fixture storage failure")):
            with self.subTest(error=type(failure)), patch.object(self.repository, "complete_terminal", side_effect=failure):
                with self.assertRaises(type(failure)) as caught:
                    self.cancel.execute(self.params)
                self.assertIs(caught.exception, failure)
        self.assertEqual(self.repository.get(GetRunCommand(self.run_id)).state, RunState.CANCELLING)
        self.assertEqual([event.kind for event in self.publications.events], ["run.cancelling"])

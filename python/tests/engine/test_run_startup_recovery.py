from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from model_deck.adapters.platform.macos.instance_lock import FileInstanceLock
from model_deck.adapters.providers.deterministic import DeterministicProviderExecutionPort
from model_deck.adapters.storage.sqlite_session_run_repository import SQLiteSessionRunRepository
from model_deck.bootstrap import build_engine_server
from model_deck.engine.server import EngineServer
from model_deck.engine.routing.ports import ExecutionMode, RouteSnapshot
from model_deck.engine.runs.ports import (
    ActiveRunState, AppendApplicationEventCommand, CancelRunCommand,
    ClaimDispatchCommand, CompleteTerminalCommand, GetRunCommand, NormalizedRunInput,
    RunAdmissionKey, RunState, StartRunCommand, StoredToolDefinitionsCompatibilityError,
    TerminalOutcome, TerminalResult,
)
from model_deck.engine.sessions.ports import CreateSessionCommand
from model_deck_contracts.paths import repo_root


class StartupCallbackTests(unittest.TestCase):
    def test_callback_runs_under_lock_before_listener_and_only_once(self):
        events = []
        class Lock:
            def acquire(self, timeout):
                events.append("lock")
                return True
            def release(self):
                events.append("release")
        class Listener:
            def start(self):
                events.append("listen")
            def stop(self):
                events.append("stop")
        server = EngineServer(Lock(), Listener(), lambda: {}, lambda _: events.append("publish"),
                              startup_callback=lambda: events.append("recover"))
        server.start()
        server.start()
        self.assertEqual(events, ["lock", "recover", "listen", "publish"])
        server.stop()
        self.assertEqual(events[-2:], ["stop", "release"])

    def test_callback_failure_releases_lock_without_listener_or_publish(self):
        events = []
        class Lock:
            def acquire(self, timeout):
                events.append("lock")
                return True
            def release(self):
                events.append("release")
        class Listener:
            def start(self):
                events.append("listen")
            def stop(self):
                events.append("stop")
        def recover():
            events.append("recover")
            raise RuntimeError("recovery failed")
        server = EngineServer(Lock(), Listener(), lambda: {}, lambda _: events.append("publish"), startup_callback=recover)
        with self.assertRaises(RuntimeError):
            server.start()
        server.stop()
        self.assertEqual(events, ["lock", "recover", "release"])


class RunStartupRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temps = []
        self.runtimes = []
        self.state = self.directory()
        self.artifacts = self.directory()
        self.sockets = self.directory()
        self.legacy = self.directory()
        self.database = self.state / "engine/state.sqlite3"
        self.database.parent.mkdir()
        self.repo = SQLiteSessionRunRepository(self.database)

    def directory(self):
        temporary = tempfile.TemporaryDirectory()
        self.temps.append(temporary)
        return Path(temporary.name).resolve()

    def tearDown(self):
        for runtime in reversed(self.runtimes):
            runtime.server.stop()
        for temporary in reversed(self.temps):
            temporary.cleanup()

    def build(self):
        runtime = build_engine_server(state_root=self.state, artifact_root=self.artifacts,
                                     socket_root=self.sockets, legacy_agents_dir=self.legacy,
                                     default_connection_id=str(uuid4()), source_root=repo_root(),
                                     enable_application_state=True, enable_fixture_runs=True)
        self.runtimes.append(runtime)
        return runtime

    def seed(self, state):
        registration = str(uuid4())
        session = self.repo.create(CreateSessionCommand(registration))
        route = RouteSnapshot(registration_id=registration, registration_revision=1,
                              connection_id=str(uuid4()), connection_revision=1,
                              provider_id="com.example.provider", provider_model_id="fixture",
                              execution_mode=ExecutionMode.CHAT_COMPLETIONS,
                              endpoint_config_ref="ref:fixture.endpoint", credential_ref="ref:fixture.credential")
        admitted = self.repo.admit(StartRunCommand(
            RunAdmissionKey("fixture-operator", "engine.v1.runs.start", str(uuid4())),
            "fixture-hash", session.session_id, str(uuid4()), registration, route,
            NormalizedRunInput(messages=({"role": "user", "content": "fixture"},)),
        ))
        run_id = admitted.run.run_id
        if state != RunState.ACCEPTED:
            self.repo.claim_dispatch(ClaimDispatchCommand(run_id, str(uuid4())))
        if state == RunState.WAITING_FOR_TOOL:
            self.repo.append_application_event(AppendApplicationEventCommand(
                run_id, ActiveRunState.RUNNING, ActiveRunState.WAITING_FOR_TOOL,
                "tool.requested", {"call_id": "fixture-call", "tool_name": "fixture-tool"}))
        elif state == RunState.CANCELLING:
            self.repo.request_cancel(CancelRunCommand(run_id, str(uuid4())))
        elif state in (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.INTERRUPTED):
            self.repo.complete_terminal(CompleteTerminalCommand(
                run_id, ActiveRunState.RUNNING, TerminalResult(TerminalOutcome(state.value)), "run." + state.value))
        return run_id

    def run_rows(self):
        with sqlite3.connect(self.database) as connection:
            return connection.execute("SELECT run_id, state, last_sequence, terminal_outcome FROM runs ORDER BY run_id").fetchall()

    def test_startup_recovers_claimed_only_without_provider_retry(self):
        states = (RunState.ACCEPTED, RunState.RUNNING, RunState.WAITING_FOR_TOOL, RunState.CANCELLING,
                  RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.INTERRUPTED)
        runs = {state: self.seed(state) for state in states}
        before = self.run_rows()
        runtime = self.build()
        self.assertEqual(self.run_rows(), before, "build must not recover")
        started_after = datetime.now(timezone.utc)
        with patch.object(DeterministicProviderExecutionPort, "start", side_effect=AssertionError("startup dispatched provider")) as provider:
            runtime.server.start()
            recovered = self.run_rows()
            runtime.server.start()
            self.assertEqual(self.run_rows(), recovered)
            provider.assert_not_called()
        for state, run_id in runs.items():
            record = self.repo.get(GetRunCommand(run_id))
            expected = RunState.INTERRUPTED if state in (RunState.RUNNING, RunState.WAITING_FOR_TOOL, RunState.CANCELLING) else state
            self.assertEqual(record.state, expected)
        with sqlite3.connect(self.database) as connection:
            for state in (RunState.RUNNING, RunState.WAITING_FOR_TOOL, RunState.CANCELLING):
                rows = connection.execute("SELECT observed_at FROM run_application_events WHERE run_id = ? AND kind = 'run.interrupted'", (runs[state],)).fetchall()
                self.assertEqual(len(rows), 1)
                timestamp = datetime.fromisoformat(rows[0][0].replace("Z", "+00:00"))
                self.assertGreaterEqual(timestamp, started_after)
                self.assertEqual(timestamp.utcoffset().total_seconds(), 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM run_outstanding_tool_calls").fetchone()[0], 0)
        self.assertTrue(runtime.rendezvous_path.exists())

    def test_second_instance_cannot_recover_first_instances_work(self):
        owner = self.build()
        owner.server.start()
        active = self.seed(RunState.RUNNING)
        before = self.run_rows()
        contender = self.build()
        self.assertEqual(self.run_rows(), before)
        advertisement = owner.rendezvous_path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "another engine instance"):
            contender.server.start()
        self.assertEqual(self.run_rows(), before)
        self.assertEqual(self.repo.get(GetRunCommand(active)).state, RunState.RUNNING)
        self.assertEqual(owner.rendezvous_path.read_bytes(), advertisement)

    def test_recovery_failure_leaves_no_listener_or_advertisement_and_releases_lock(self):
        pending = self.seed(RunState.ACCEPTED)
        active = self.seed(RunState.RUNNING)
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE runs SET tools_json = ? WHERE run_id = ?", ('[{"call_id":"legacy"}]', pending))
        before = self.run_rows()
        runtime = self.build()
        with self.assertRaises(StoredToolDefinitionsCompatibilityError):
            runtime.server.start()
        self.assertEqual(self.run_rows(), before)
        self.assertEqual(self.repo.get(GetRunCommand(active)).state, RunState.RUNNING)
        self.assertFalse((self.sockets / "engine.sock").exists())
        self.assertFalse(runtime.rendezvous_path.exists())
        lock = FileInstanceLock(self.state / "engine/instance.lock")
        self.assertTrue(lock.acquire(0))
        lock.release()


if __name__ == "__main__":
    unittest.main()

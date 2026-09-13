"""Real archived standard-library child through public runtime and engine ports."""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from uuid import uuid4
import zipfile

from model_deck.adapters.routing.registered import ProviderRouteDefinition, RegisteredRouteResolver
from model_deck.adapters.storage.sqlite_connection_repository import SQLiteConnectionRepository
from model_deck.adapters.storage.sqlite_model_repository import SQLiteModelRepository
from model_deck.adapters.storage.sqlite_session_run_repository import SQLiteSessionRunRepository
from model_deck.engine.connections.ports import SaveConnectionCommand
from model_deck.engine.model_library.ports import RegisterModelCommand
from model_deck.engine.routing.ports import CapabilityFeature, CapabilityTriState, ExecutionMode
from model_deck.engine.runs.ports import GetRunCommand, RunState
from model_deck.engine.runs.use_cases import CancelRunUseCase, RunApplicationCoordinator, StartRunUseCase, SubmitToolResultUseCase
from model_deck.engine.sessions.ports import CreateSessionCommand
from model_deck.engine.sessions.use_cases import SelectSessionModelUseCase
from model_deck.plugins.archive_inspection import inspect_archive
from model_deck.plugins.artifact_store import stage_archive
from model_deck.plugins.lifecycle_session import LifecycleSession
from model_deck.plugins.process_runtime import ProcessRuntime, ProcessRuntimeConfig
from model_deck.plugins.provider_proxy import ExternalProviderExecution
from model_deck_contracts import validate_schema_ref

PLUGIN_ID = "org.example.deterministic-provider"
PRINCIPAL = "fixture-operator"


class Events:
    def __init__(self):
        self.values = []
        self.condition = threading.Condition()

    def publish_application_event(self, event):
        with self.condition:
            self.values.append(event)
            self.condition.notify_all()

    def wait(self, run_id, kind):
        with self.condition:
            matched = lambda: any(value.run_id == run_id and value.kind == kind for value in self.values)
            if not self.condition.wait_for(matched, timeout=5):
                raise AssertionError("expected fixture event: " + kind + "; got " + repr([e.kind for e in self.values]))
            return next(value for value in self.values if value.run_id == run_id and value.kind == kind)


class ExternalProviderIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="md-external-provider-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        package = Path(__file__).resolve().parents[3] / "examples/deterministic-provider"
        manifest = json.loads((package / "manifest.json").read_text())
        validate_schema_ref("contracts/plugin.v1/manifest.schema.json", manifest)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as writer:
            for name in ("manifest.json", "plugin.py", "README.md"):
                writer.writestr(name, (package / name).read_bytes())
        blob = archive.getvalue()
        self.assertEqual(inspect_archive(blob).total_entries, 3)
        artifacts = root / "artifacts"
        artifacts.mkdir()
        staged = stage_archive(blob, store_root=artifacts)
        self.assertFalse(staged.already_present)
        self.assertTrue(stage_archive(blob, store_root=artifacts).already_present)
        self.artifact = Path(staged.artifact_path)
        self.runtime = ProcessRuntime(ProcessRuntimeConfig(
            argv=(sys.executable, "-I", str(self.artifact / manifest["entrypoint"]["path"])),
            package_dir=str(self.artifact), timeout_s=2))
        self.addCleanup(self.runtime.close)
        watchdog = threading.Timer(20, self.runtime.close)
        watchdog.daemon = True
        watchdog.start()
        self.addCleanup(watchdog.cancel)
        lifecycle = LifecycleSession(expected_plugin_id=PLUGIN_ID, expected_plugin_version="1.0.0",
                                     offered_api_major=1, offered_api_minor=0,
                                     activation_token="fixture-token", allowed_broker_methods=())
        self.runtime.spawn()
        hello = self.runtime.run_hello(lifecycle, "fixture-nonce")
        self.assertIn("fixture.isolated-imports", hello["capabilities"])
        self.runtime.run_activation(lifecycle)
        self.proxy = ExternalProviderExecution(self.runtime.provider_channel(), provider_id=PLUGIN_ID,
                                               clock=lambda: datetime.now(timezone.utc), request_timeout_s=2)
        self.addCleanup(self.proxy.close)
        database = root / "state.sqlite3"
        models = SQLiteModelRepository(database)
        connections = SQLiteConnectionRepository(database)
        self.repo = SQLiteSessionRunRepository(database)
        connection = connections.save(SaveConnectionCommand(
            connection_id=str(uuid4()), provider_id=PLUGIN_ID, expected_revision=0,
            idempotency_key=str(uuid4()), endpoint_config_ref=str(uuid4())))
        self.registrations = {}
        for name in ("text", "tool", "wait", "cancel-unconfirmed", "malformed", "foreign-handle", "crash"):
            self.registrations[name] = models.register(RegisterModelCommand(
                connection.connection_id, name, "External " + name, 0, str(uuid4())))
        routes = RegisteredRouteResolver(model_repository=models, connection_repository=connections,
            provider_route_definitions={PLUGIN_ID: ProviderRouteDefinition(ExecutionMode.CUSTOM,
                (CapabilityFeature("tools", CapabilityTriState.SUPPORTED),))})
        self.selection = SelectSessionModelUseCase(self.repo, routes)
        self.events = Events()
        self.coordinator = RunApplicationCoordinator(self.repo, event_publisher=self.events)
        self.start = StartRunUseCase(self.repo, self.repo, routes, self.proxy, self.coordinator)
        self.tools = SubmitToolResultUseCase(self.repo, self.coordinator)
        self.cancel = CancelRunUseCase(self.repo, self.coordinator,
            cancel_deadline=(datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat())

    def run_scenario(self, scenario):
        initial = self.registrations["text"]
        selected = self.registrations[scenario]
        session = self.repo.create(CreateSessionCommand(initial.registration_id))
        self.selection.execute({"session_id": session.session_id, "registration_id": selected.registration_id,
                                "expected_revision": session.revision})
        params = {"session_id": session.session_id, "registration_id": selected.registration_id,
                  "client_request_id": str(uuid4()), "idempotency_key": str(uuid4()),
                  "input": {"messages": [{"role": "user", "content": "fixture"}]}}
        if scenario == "tool":
            params["tools"] = [{"name": "lookup", "input_schema": {"type": "object"}, "host_execution_required": True}]
        result = self.start.execute(params, principal_id=PRINCIPAL)
        run_id = result["run"]["run_id"]
        record = self.repo.get(GetRunCommand(run_id))
        self.assertEqual(record.route_snapshot.provider_id, PLUGIN_ID)
        self.assertEqual(record.route_snapshot.provider_model_id, scenario)
        self.assertEqual(record.route_snapshot.registration_id, selected.registration_id)
        return run_id, params

    def assert_one_terminal(self, run_id):
        terminal = {"run.completed", "run.failed", "run.cancelled", "run.interrupted"}
        self.assertEqual(len([e for e in self.events.values if e.run_id == run_id and e.kind in terminal]), 1)

    def test_archived_isolated_text_provider_and_admission_replay(self):
        run_id, params = self.run_scenario("text")
        self.events.wait(run_id, "run.completed")
        delta = self.events.wait(run_id, "content.delta")
        self.assertEqual(delta.payload["delta"], "external deterministic text")
        replay = self.start.execute(params, principal_id=PRINCIPAL)
        self.assertEqual(replay["run"]["run_id"], run_id)
        self.assertEqual(self.repo.get(GetRunCommand(run_id)).state, RunState.COMPLETED)
        self.assert_one_terminal(run_id)

    def test_real_tool_request_result_completion_and_receipt_replay(self):
        run_id, _ = self.run_scenario("tool")
        tool = self.events.wait(run_id, "tool.requested")
        self.assertEqual(tool.payload["call_id"], "fixture-call")
        self.assertEqual(tool.payload["arguments"], {"query": "fixture"})
        self.assertEqual(self.repo.get(GetRunCommand(run_id)).state, RunState.WAITING_FOR_TOOL)
        params = {"run_id": run_id, "call_id": "fixture-call", "result": {"answer": 42}, "idempotency_key": str(uuid4())}
        self.tools.execute(params, principal_id=PRINCIPAL)
        self.events.wait(run_id, "run.completed")
        self.tools.execute(params, principal_id=PRINCIPAL)
        self.assertEqual(self.events.wait(run_id, "content.delta").payload["delta"], "tool result received")
        self.assert_one_terminal(run_id)

    def test_confirmed_cancel_terminalizes_real_worker_run(self):
        run_id, _ = self.run_scenario("wait")
        self.events.wait(run_id, "run.started")
        result = self.cancel.execute({"run_id": run_id, "idempotency_key": str(uuid4())})
        self.assertTrue(result["accepted"])
        self.events.wait(run_id, "run.cancelled")
        self.assertEqual(self.repo.get(GetRunCommand(run_id)).state, RunState.CANCELLED)
        self.assert_one_terminal(run_id)

    def test_unconfirmed_cancel_remains_cancelling_until_interruption(self):
        run_id, _ = self.run_scenario("cancel-unconfirmed")
        self.events.wait(run_id, "run.started")
        result = self.cancel.execute({"run_id": run_id, "idempotency_key": str(uuid4())})
        self.assertEqual(result["state"], "cancelling")
        self.runtime.close()
        self.events.wait(run_id, "run.interrupted")
        self.assert_one_terminal(run_id)

    def test_malformed_wire_interrupts_without_retry(self):
        run_id, _ = self.run_scenario("malformed")
        self.events.wait(run_id, "run.interrupted")
        self.assertEqual(self.repo.get(GetRunCommand(run_id)).state, RunState.INTERRUPTED)
        self.assert_one_terminal(run_id)

    def test_foreign_handle_interrupts_without_publication(self):
        run_id, _ = self.run_scenario("foreign-handle")
        self.events.wait(run_id, "run.interrupted")
        self.assertFalse(any(e.kind == "run.started" for e in self.events.values))
        self.assert_one_terminal(run_id)

    def test_child_exit_interrupts_without_retry(self):
        run_id, _ = self.run_scenario("crash")
        self.events.wait(run_id, "run.interrupted")
        self.assertEqual(self.repo.get(GetRunCommand(run_id)).state, RunState.INTERRUPTED)
        self.assert_one_terminal(run_id)


if __name__ == "__main__":
    unittest.main()

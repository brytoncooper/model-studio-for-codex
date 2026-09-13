from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from model_deck.adapters.providers.deterministic import (
    DeterministicProviderExecutionPort, EmitStarted, EmitTerminalCompleted, EmitUsage,
)
from model_deck.adapters.storage.sqlite_usage import SqliteUsageRepository
from model_deck.adapters.transport.rendezvous import load_rendezvous_file
from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
from model_deck.bootstrap import build_engine_server
from model_deck.engine.usage.ports import UsageConflictError, UsageEventMismatchError, UsageResourceExhaustedError
from model_deck.engine.usage.reconciliation import ReconciledUsageQueryUseCase
from model_deck_contracts.paths import repo_root
from model_deck_contracts.validator import validate_schema_ref
from tests.engine import test_engine_event_dispatch as event_fixtures

USAGE_METHOD = "engine.v1.usage.query"


class RequestUsageFixtureProvider:
    def __init__(self):
        self.started = 0
        self.expected = []

    def start(self, request, sink):
        self.started += 1
        first = {"run_id": request.run_id, "session_id": request.session_id,
                 "observed_at": "2026-09-12T10:00:00Z", "units": 12, "unit_kind": "input_tokens"}
        second = {**first, "observed_at": "2026-09-12T10:01:00Z", "units": 3,
                  "unit_kind": "output_tokens", "settled_amount": None, "estimate_amount": None, "currency": None}
        self.expected = [first, second]
        return DeterministicProviderExecutionPort(auto_advance=True,
            script=(EmitStarted(), EmitUsage(first), EmitUsage(second), EmitTerminalCompleted())).start(request, sink)


class UsageBootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.provider = RequestUsageFixtureProvider()

    def build(self, *, application=True, runs=True):
        with mock.patch("model_deck.bootstrap.DeterministicProviderExecutionPort", return_value=self.provider):
            runtime = build_engine_server(
                state_root=self.root / "state", artifact_root=self.root / "artifact", socket_root=self.root / "socket",
                source_root=repo_root(), legacy_agents_dir=event_fixtures.FIXTURES / "legacy_agent",
                default_connection_id=event_fixtures.CONNECTION_ID,
                enable_application_state=application, enable_fixture_runs=runs)
        runtime.server.start()
        self.addCleanup(runtime.server.stop)
        return runtime

    def connection(self, runtime):
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text().strip()
        return UnixSocketEngineClient(descriptor.socket_path).session(), descriptor, credential

    def _authenticate(self, session, descriptor, credential, client_name=event_fixtures.CLIENT_NAME):
        event_fixtures.EngineEventDispatchTests._authenticate(self, session, descriptor, credential, client_name)

    def run_fixture(self, session, descriptor, credential):
        return event_fixtures.EngineEventDispatchTests._fixture_run_id(self, session, descriptor, credential)

    def query(self, session, params=None):
        return session.call({"jsonrpc": "2.0", "id": 40, "method": USAGE_METHOD,
                             "params": {} if params is None else params})

    def test_authenticated_query_records_genuine_events_and_preserves_unknown_fields(self):
        runtime = self.build()
        connection, descriptor, credential = self.connection(runtime)
        with connection as session:
            run_id = self.run_fixture(session, descriptor, credential)
            response = self.query(session)
            self.assertIn("result", response, response)
            validate_schema_ref("contracts/engine.v1/methods/usage.query.result.schema.json", response["result"])
            self.assertEqual(response["result"]["records"], self.provider.expected)
            self.assertEqual(response["result"]["records"][0]["run_id"], run_id)
            self.assertNotIn("settled_amount", response["result"]["records"][0])
            self.assertIsNone(response["result"]["records"][1]["settled_amount"])
            self.assertEqual(self.query(session)["result"], response["result"])
            filtered = self.query(session, {"since": "2026-09-12T10:00:30Z"})
            self.assertEqual(filtered["result"]["records"], self.provider.expected[1:])
            operations = session.call({"jsonrpc": "2.0", "id": 41, "method": "engine.v1.operations.list", "params": {}})
            descriptor_row = next(row for row in operations["result"]["operations"] if row["operation_id"] == USAGE_METHOD)
            self.assertEqual(descriptor_row["effect"], "read")
        self.assertEqual(self.provider.started, 1)
        self.assertEqual([row.to_wire() for row in SqliteUsageRepository(runtime.application_database_path).query(None, None).records],
                         self.provider.expected)

    def test_typed_failures_and_invalid_results_are_fixed_safe_wire_errors(self):
        runtime = self.build()
        connection, descriptor, credential = self.connection(runtime)
        with connection as session:
            self._authenticate(session, descriptor, credential)
            for failure, expected in ((UsageConflictError("private payload"), "conflict"),
                                      (UsageEventMismatchError("private payload"), "internal"),
                                      (UsageResourceExhaustedError("private payload"), "resource_exhausted")):
                with self.subTest(error=type(failure)), mock.patch.object(ReconciledUsageQueryUseCase, "query", side_effect=failure):
                    response = self.query(session)
                self.assertEqual(response["error"]["data"]["code"], expected)
                self.assertNotIn("private payload", json.dumps(response))
            invalid = mock.Mock()
            invalid.to_wire.return_value = {"records": [{"private": "payload"}]}
            with mock.patch.object(ReconciledUsageQueryUseCase, "query", return_value=invalid):
                response = self.query(session)
            self.assertEqual(response["error"]["data"]["code"], "internal")
            self.assertNotIn("private", json.dumps(response))

    def test_usage_response_preflight_failure_does_not_break_connection(self):
        with mock.patch("model_deck.bootstrap.encode_frame", side_effect=ValueError("private response")):
            runtime = self.build()
        connection, descriptor, credential = self.connection(runtime)
        with connection as session:
            self._authenticate(session, descriptor, credential)
            response = self.query(session)
            self.assertEqual(response["error"]["data"]["code"], "resource_exhausted")
            self.assertNotIn("private response", json.dumps(response))
            health = session.call({"jsonrpc": "2.0", "id": 44, "method": "engine.v1.health", "params": {}})
            self.assertEqual(health["result"], {"status": "ok"})
    def test_restart_recovers_unmaterialized_committed_usage_without_provider_dispatch(self):
        first = self.build()
        connection, descriptor, credential = self.connection(first)
        with connection as session:
            self.run_fixture(session, descriptor, credential)
        first.server.stop()
        restarted = self.build()
        connection, descriptor, credential = self.connection(restarted)
        with connection as session:
            self._authenticate(session, descriptor, credential)
            self.assertEqual(self.query(session)["result"]["records"], self.provider.expected)
            self.assertEqual(self.query(session)["result"]["records"], self.provider.expected)
        self.assertEqual(self.provider.started, 1)

    def test_record_failure_after_run_commit_returns_error_then_retry_repairs(self):
        runtime = self.build()
        connection, descriptor, credential = self.connection(runtime)
        original = SqliteUsageRepository.store

        def fail_second(repository, record, sequence):
            if record.unit_kind == "output_tokens":
                raise OSError("sensitive fixture database detail")
            return original(repository, record, sequence)

        with connection as session:
            run_id = self.run_fixture(session, descriptor, credential)
            with mock.patch.object(SqliteUsageRepository, "store", fail_second):
                failed = self.query(session)
            self.assertNotIn("result", failed)
            self.assertEqual(failed["error"]["data"]["code"], "internal")
            self.assertNotIn("sensitive", json.dumps(failed))
            self.assertEqual(self.query(session)["result"]["records"], self.provider.expected)
            state = session.call({"jsonrpc": "2.0", "id": 42, "method": "engine.v1.runs.get", "params": {"run_id": run_id}})
            self.assertEqual(state["result"]["run"]["state"], "completed")
        self.assertEqual(self.provider.started, 1)

    def test_authentication_default_unavailable_and_strict_params(self):
        runtime = self.build()
        connection, descriptor, credential = self.connection(runtime)
        with connection as session:
            self.assertEqual(self.query(session)["error"]["data"]["code"], "capability_denied")
            self._authenticate(session, descriptor, credential)
            for params in ({"extra": "secret value"}, {"since": None}, {"since": "invalid timestamp"},
                           {"since": "2026-09-13T00:00:00Z", "until": "2026-09-12T00:00:00Z"}):
                response = self.query(session, params)
                self.assertNotIn("result", response)
                self.assertNotIn("secret value", json.dumps(response))
        runtime.server.stop()
        for application in (False, True):
            runtime = self.build(application=application, runs=False)
            connection, descriptor, credential = self.connection(runtime)
            with connection as session:
                self._authenticate(session, descriptor, credential)
                response = self.query(session)
                self.assertEqual(response["error"]["data"]["code"], "unsupported_capability")
                operations = session.call({"jsonrpc": "2.0", "id": 43, "method": "engine.v1.operations.list", "params": {}})
                self.assertNotIn(USAGE_METHOD, [row["operation_id"] for row in operations["result"]["operations"]])
            runtime.server.stop()

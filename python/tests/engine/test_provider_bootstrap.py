from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest

from model_deck.adapters.providers.deterministic import (
    DeterministicProviderExecutionPort, EmitStarted, EmitUsage, EmitToolRequested,
    EmitContent, EmitTerminalCompleted,
)
from model_deck.adapters.routing.registered import ProviderRouteDefinition
from model_deck.adapters.transport.rendezvous import load_rendezvous_file
from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
from model_deck.bootstrap import build_engine_server
from model_deck.engine.routing.ports import CapabilityFeature, CapabilityTriState, ExecutionMode
from model_deck.engine.runs.ports import SubmitToolResultProviderOutcome
from model_deck_contracts.paths import repo_root
from model_deck_contracts.validator import validate_schema_ref
from tests.engine import test_engine_event_dispatch as event_fixtures

PROVIDER_ID = "com.example.injected"
CONNECTION_ID = event_fixtures.CONNECTION_ID
TOOL = {"name": "lookup", "input_schema": {"type": "object"}, "host_execution_required": False}


def definition(state=CapabilityTriState.SUPPORTED):
    return ProviderRouteDefinition(ExecutionMode.CUSTOM, (CapabilityFeature("tools", state),), "ref:injected.capabilities")


class FixtureHandle:
    def __init__(self, inner):
        self.inner = inner
        self.submissions = 0

    def submit_tool_result(self, call_id, result):
        self.submissions += 1
        response = self.inner.submit_tool_result(call_id, result)
        if response.outcome == SubmitToolResultProviderOutcome.ACCEPTED:
            while not self.inner.is_terminal:
                self.inner.advance()
        return response

    def request_cancel(self, *, deadline):
        return self.inner.request_cancel(deadline=deadline)


class InjectedFixtureProvider:
    def __init__(self):
        self.requests = []
        self.handles = []
        self.closes = 0

    def start(self, request, sink):
        self.requests.append(request)
        usage = {"run_id": request.run_id, "session_id": request.session_id,
                 "observed_at": "2026-09-12T12:00:00Z", "units": 9, "unit_kind": "input_tokens"}
        execution = DeterministicProviderExecutionPort(provider_id=PROVIDER_ID, auto_advance=True,
            script=(EmitStarted(), EmitUsage(usage), EmitToolRequested("call-injected", "lookup", {"q": "fixture"}),
                    EmitContent("injected output"), EmitTerminalCompleted()))
        handle = FixtureHandle(execution.start(request, sink))
        self.handles.append(handle)
        return handle

    def close(self):
        self.closes += 1


class ProviderBootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.provider = InjectedFixtureProvider()
        self.next_request_id = 1

    def arguments(self):
        return dict(state_root=self.root / "state", artifact_root=self.root / "artifact",
            socket_root=self.root / "socket", source_root=repo_root(),
            legacy_agents_dir=event_fixtures.FIXTURES / "legacy_agent", default_connection_id=CONNECTION_ID,
            enable_application_state=True, provider_execution=self.provider,
            provider_route_definitions={PROVIDER_ID: definition()})

    def build(self, **overrides):
        arguments = {**self.arguments(), **overrides}
        runtime = build_engine_server(**arguments)
        runtime.server.start()
        self.addCleanup(runtime.server.stop)
        return runtime

    def session(self, runtime):
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        connection = UnixSocketEngineClient(descriptor.socket_path).session()
        return connection, descriptor, runtime.enrollment.credential_path.read_text().strip()

    def authenticate(self, session, descriptor, credential):
        event_fixtures.EngineEventDispatchTests._authenticate(self, session, descriptor, credential)

    def request(self, session, method, params):
        self.next_request_id += 1
        return session.call({"jsonrpc": "2.0", "id": self.next_request_id, "method": "engine.v1." + method, "params": params})

    def call(self, session, method, params):
        response = self.request(session, method, params)
        self.assertIn("result", response, response)
        return response["result"]

    def register(self, session):
        self.call(session, "connections.save", {"expected_revision": 0, "idempotency_key": "connection",
            "connection": {"connection_id": CONNECTION_ID, "provider_id": PROVIDER_ID,
                           "credential_ref": "ref:fixture.credential", "endpoint_config_ref": "ref:fixture.endpoint"}})
        return self.call(session, "models.register", {"connection_id": CONNECTION_ID,
            "provider_model_id": "injected/model", "display_name": "Injected", "expected_revision": 0,
            "idempotency_key": "model"})["model"]["registration_id"]

    def start_params(self, session, registration_id):
        session_id = self.call(session, "sessions.create", {"registration_id": registration_id})["session_id"]
        return {"session_id": session_id, "registration_id": registration_id,
                "client_request_id": "injected-request", "idempotency_key": session_id, "tools": [TOOL]}

    def test_injected_provider_receives_admitted_route_tools_and_socket_callbacks(self):
        routes = {PROVIDER_ID: definition()}
        runtime = self.build(provider_route_definitions=routes)
        routes.clear()
        connection, descriptor, credential = self.session(runtime)
        with connection as session:
            self.authenticate(session, descriptor, credential)
            registration_id = self.register(session)
            params = self.start_params(session, registration_id)
            run = self.call(session, "runs.start", params)["run"]
            self.assertEqual(run["state"], "waiting_for_tool")
            received = self.provider.requests[0]
            self.assertEqual(received.run_id, run["run_id"])
            self.assertEqual(received.route_snapshot.provider_id, PROVIDER_ID)
            self.assertEqual(received.route_snapshot.credential_ref, "ref:fixture.credential")
            self.assertEqual(received.route_snapshot.endpoint_config_ref, "ref:fixture.endpoint")
            self.assertEqual(received.tools[0].name, "lookup")
            self.call(session, "events.subscribe", {"topics": ["run:" + run["run_id"]], "initial_credit": 10})
            notifications = [session.read_notification() for _ in range(3)]
            for notification in notifications:
                validate_schema_ref("contracts/engine.v1/notifications/event.schema.json", notification)
            self.assertEqual(notifications[-1]["params"]["event"]["tool_call"]["call_id"], "call-injected")
            self.assertEqual(self.call(session, "usage.query", {})["records"][0]["run_id"], run["run_id"])
            result_params = {"run_id": run["run_id"], "call_id": "call-injected", "result": {"answer": "fixture"}, "idempotency_key": "result"}
            self.call(session, "runs.submit_tool_result", result_params)
            self.call(session, "runs.submit_tool_result", result_params)
            self.assertEqual(self.provider.handles[0].submissions, 1)
            self.assertEqual(self.call(session, "runs.get", {"run_id": run["run_id"]})["run"]["state"], "completed")
        runtime.server.stop()
        self.assertEqual(self.provider.closes, 0)

    def test_removed_model_rejects_new_admission_while_admitted_run_keeps_route(self):
        runtime = self.build()
        connection, descriptor, credential = self.session(runtime)
        with connection as session:
            self.authenticate(session, descriptor, credential)
            registration_id = self.register(session)
            first_params = self.start_params(session, registration_id)
            admitted = self.call(session, "runs.start", first_params)["run"]
            self.assertEqual(admitted["state"], "waiting_for_tool")
            captured_route = self.provider.requests[0].route_snapshot

            removed = self.call(session, "models.remove", {
                "registration_id": registration_id,
                "expected_revision": 1,
                "idempotency_key": "remove-during-run",
            })
            self.assertTrue(removed["removed"])

            rejected_params = dict(first_params)
            rejected_params["client_request_id"] = "new-after-removal"
            rejected_params["idempotency_key"] = "new-after-removal"
            rejected = self.request(session, "runs.start", rejected_params)
            self.assertIn("error", rejected)
            self.assertEqual(rejected["error"]["data"]["code"], "not_found")
            self.assertEqual(len(self.provider.requests), 1)

            self.call(session, "runs.submit_tool_result", {
                "run_id": admitted["run_id"],
                "call_id": "call-injected",
                "result": {"answer": "fixture"},
                "idempotency_key": "finish-admitted-after-removal",
            })
            completed = self.call(
                session, "runs.get", {"run_id": admitted["run_id"]}
            )["run"]
            self.assertEqual(completed["state"], "completed")
            self.assertEqual(self.provider.requests[0].route_snapshot, captured_route)
            self.assertEqual(captured_route.provider_id, PROVIDER_ID)
            self.assertEqual(captured_route.connection_id, CONNECTION_ID)

    def test_unknown_tools_capability_refuses_before_durable_admission(self):
        runtime = self.build(provider_route_definitions={PROVIDER_ID: definition(CapabilityTriState.UNKNOWN)})
        connection, descriptor, credential = self.session(runtime)
        with connection as session:
            self.authenticate(session, descriptor, credential)
            params = self.start_params(session, self.register(session))
            self.assertIn("error", self.request(session, "runs.start", params))
        self.assertEqual(self.provider.requests, [])
        with sqlite3.connect(runtime.application_database_path) as database:
            self.assertEqual(database.execute("SELECT count(*) FROM runs").fetchone()[0], 0)

    def test_cancel_and_restart_keep_application_recovery_and_caller_ownership(self):
        runtime = self.build()
        connection, descriptor, credential = self.session(runtime)
        with connection as session:
            self.authenticate(session, descriptor, credential)
            registration_id = self.register(session)
            cancelled = self.call(session, "runs.start", self.start_params(session, registration_id))["run"]["run_id"]
            self.call(session, "runs.cancel", {"run_id": cancelled, "idempotency_key": "cancel"})
            self.assertEqual(self.call(session, "runs.get", {"run_id": cancelled})["run"]["state"], "cancelled")
            active = self.call(session, "runs.start", self.start_params(session, registration_id))["run"]["run_id"]
        runtime.server.stop()
        restarted = self.build()
        connection, descriptor, credential = self.session(restarted)
        with connection as session:
            self.authenticate(session, descriptor, credential)
            self.assertEqual(self.call(session, "runs.get", {"run_id": active})["run"]["state"], "interrupted")
            self.assertEqual(len(self.call(session, "usage.query", {})["records"]), 2)
        self.assertEqual(len(self.provider.requests), 2)
        self.assertEqual(self.provider.closes, 0)

    def test_routes_snapshot_isolates_engine_from_caller_mutation(self):
        routes = {PROVIDER_ID: definition()}
        runtime = self.build(provider_route_definitions=routes)
        routes.clear()
        routes["com.example.other"] = ProviderRouteDefinition(
            ExecutionMode.CUSTOM,
            (CapabilityFeature("tools", CapabilityTriState.UNSUPPORTED),),
            "ref:other.capabilities",
        )
        connection, descriptor, credential = self.session(runtime)
        with connection as session:
            self.authenticate(session, descriptor, credential)
            params = self.start_params(session, self.register(session))
            run = self.call(session, "runs.start", params)["run"]
            self.assertEqual(run["state"], "waiting_for_tool")
            self.assertEqual(self.provider.requests[-1].route_snapshot.provider_id, PROVIDER_ID)
            self.assertEqual(self.provider.requests[-1].route_snapshot.capability_snapshot_ref, "ref:injected.capabilities")
            self.assertEqual(
                [(feature.name, feature.state.value) for feature in self.provider.requests[-1].route_snapshot.capability_features],
                [("tools", CapabilityTriState.SUPPORTED.value)],
            )

    def test_invalid_capability_snapshot_ref_rejects_before_state_creation(self):
        overlong = "ref:" + ("a" * 130)
        cases = [
            ("", {PROVIDER_ID: ProviderRouteDefinition(ExecutionMode.CUSTOM,
                (CapabilityFeature("tools", CapabilityTriState.SUPPORTED),), "")}),
            ("not-a-uuid-or-ref", {PROVIDER_ID: ProviderRouteDefinition(ExecutionMode.CUSTOM,
                (CapabilityFeature("tools", CapabilityTriState.SUPPORTED),), "not-a-uuid-or-ref")}),
            ("ref:BadCase", {PROVIDER_ID: ProviderRouteDefinition(ExecutionMode.CUSTOM,
                (CapabilityFeature("tools", CapabilityTriState.SUPPORTED),), "ref:BadCase")}),
            ("ref:1bad", {PROVIDER_ID: ProviderRouteDefinition(ExecutionMode.CUSTOM,
                (CapabilityFeature("tools", CapabilityTriState.SUPPORTED),), "ref:1bad")}),
            (overlong, {PROVIDER_ID: ProviderRouteDefinition(ExecutionMode.CUSTOM,
                (CapabilityFeature("tools", CapabilityTriState.SUPPORTED),), overlong)}),
            ("urn:uuid:not-canonical", {PROVIDER_ID: ProviderRouteDefinition(ExecutionMode.CUSTOM,
                (CapabilityFeature("tools", CapabilityTriState.SUPPORTED),), "urn:uuid:not-canonical")}),
            ("not a string", {PROVIDER_ID: ProviderRouteDefinition(ExecutionMode.CUSTOM,
                (CapabilityFeature("tools", CapabilityTriState.SUPPORTED),), 42)}),
        ]
        for label, routes in cases:
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    build_engine_server(**{**self.arguments(), "provider_route_definitions": routes})
                self.assertFalse((self.root / "state").exists(), label)
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(self.provider.closes, 0)

    def test_nested_capability_feature_list_mutation_isolates_engine(self):
        features = [CapabilityFeature("tools", CapabilityTriState.SUPPORTED)]
        routes = {PROVIDER_ID: ProviderRouteDefinition(ExecutionMode.CUSTOM, features,
            "ref:injected.capabilities")}
        runtime = self.build(provider_route_definitions=routes)
        features.append(CapabilityFeature("streaming", CapabilityTriState.SUPPORTED))
        features[0] = CapabilityFeature("tools", CapabilityTriState.UNSUPPORTED)
        connection, descriptor, credential = self.session(runtime)
        with connection as session:
            self.authenticate(session, descriptor, credential)
            params = self.start_params(session, self.register(session))
            run = self.call(session, "runs.start", params)["run"]
            self.assertEqual(run["state"], "waiting_for_tool")
            snapshot_features = self.provider.requests[-1].route_snapshot.capability_features
            self.assertEqual(
                [(feature.name, feature.state.value) for feature in snapshot_features],
                [("tools", CapabilityTriState.SUPPORTED.value)],
            )

    def test_invalid_composition_rejects_before_state_creation(self):
        cases = [dict(provider_route_definitions=None), dict(provider_execution=None),
                 dict(enable_application_state=False), dict(enable_fixture_runs=True),
                 dict(provider_execution=object()), dict(provider_route_definitions=[]),
                 dict(provider_route_definitions={PROVIDER_ID: object()})]
        for override in cases:
            with self.subTest(override=override), self.assertRaises(ValueError):
                build_engine_server(**{**self.arguments(), **override})
        self.assertFalse((self.root / "state").exists())

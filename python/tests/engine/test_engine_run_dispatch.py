import tempfile
import unittest
from pathlib import Path

from model_deck.adapters.platform.macos.isolated_roots import validate_isolated_roots
from model_deck.adapters.providers.deterministic import DETERMINISTIC_PROVIDER_ID
from model_deck.adapters.transport.rendezvous import load_rendezvous_file
from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
from model_deck.bootstrap import build_engine_server
from model_deck_contracts.paths import repo_root

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CONNECTION_ID = "550e8400-e29b-41d4-a716-446655440002"
CLIENT_NAME = "fixture-run-client"

_BASE_OPERATION_IDS = {
    "engine.v1.hello",
    "engine.v1.health",
    "engine.v1.operations.list",
    "engine.v1.capabilities.get",
    "engine.v1.models.list",
}

_B07_OPERATION_IDS = {
    "engine.v1.models.register",
    "engine.v1.models.rename",
    "engine.v1.models.remove",
    "engine.v1.connections.list",
    "engine.v1.connections.save",
}

_B12_OPERATION_IDS = {
    "engine.v1.sessions.create",
    "engine.v1.sessions.get",
    "engine.v1.sessions.select_model",
    "engine.v1.runs.start",
    "engine.v1.runs.get",
    "engine.v1.runs.cancel",
    "engine.v1.runs.submit_tool_result",
    "engine.v1.events.subscribe",
    "engine.v1.events.ack",
    "engine.v1.events.unsubscribe",
}


class EngineRunDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temps: list[tempfile.TemporaryDirectory[str]] = []

    def tearDown(self) -> None:
        for td in self._temps:
            td.cleanup()

    def _temp_dir(self) -> Path:
        td = tempfile.TemporaryDirectory()
        self._temps.append(td)
        return Path(td.name)

    def _start_runtime(self, *, enable_application_state: bool, enable_fixture_runs: bool = False):
        root = repo_root()
        state = self._temp_dir()
        artifact = self._temp_dir()
        socket_root = self._temp_dir()
        validate_isolated_roots(state, artifact, socket_root, source_root=root)
        runtime = build_engine_server(
            state_root=state,
            artifact_root=artifact,
            socket_root=socket_root,
            legacy_agents_dir=FIXTURES / "legacy_agent",
            default_connection_id=CONNECTION_ID,
            source_root=root,
            enable_application_state=enable_application_state,
            enable_fixture_runs=enable_fixture_runs,
        )
        runtime.server.start()
        self.addCleanup(runtime.server.stop)
        return runtime

    def _authenticate(self, session, descriptor, credential: str, client_name: str = CLIENT_NAME) -> None:
        response = session.call(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "engine.v1.hello",
                "params": {
                    "client_name": client_name,
                    "offered_api": {"major": 1, "minor": 0},
                    "authentication": {
                        "engine_instance_id": descriptor.engine_instance_id,
                        "instance_nonce": descriptor.instance_nonce,
                        "credential": credential,
                    },
                },
            }
        )
        self.assertTrue(response["result"]["authenticated"])

    def test_default_mode_operations_unchanged(self) -> None:
        runtime = self._start_runtime(enable_application_state=False)
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        with client.session() as session:
            self._authenticate(session, descriptor, credential)
            response = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "engine.v1.operations.list",
                    "params": {},
                }
            )
        operation_ids = {entry["operation_id"] for entry in response["result"]["operations"]}
        self.assertEqual(operation_ids, _BASE_OPERATION_IDS)

    def test_application_state_without_fixture_runs_keeps_b07_only(self) -> None:
        runtime = self._start_runtime(enable_application_state=True, enable_fixture_runs=False)
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        with client.session() as session:
            self._authenticate(session, descriptor, credential)
            response = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "engine.v1.operations.list",
                    "params": {},
                }
            )
            operation_ids = {entry["operation_id"] for entry in response["result"]["operations"]}
            self.assertEqual(operation_ids, _BASE_OPERATION_IDS | _B07_OPERATION_IDS)
            unsupported = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "engine.v1.runs.start",
                    "params": {
                        "session_id": "550e8400-e29b-41d4-a716-446655440003",
                        "client_request_id": "client-1",
                        "idempotency_key": "idem-1",
                        "registration_id": "550e8400-e29b-41d4-a716-446655440001",
                    },
                }
            )
            self.assertEqual(unsupported["error"]["data"]["code"], "unsupported_capability")

    def test_fixture_mode_advertises_b12_operations(self) -> None:
        runtime = self._start_runtime(enable_application_state=True, enable_fixture_runs=True)
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        with client.session() as session:
            self._authenticate(session, descriptor, credential)
            response = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "engine.v1.operations.list",
                    "params": {},
                }
            )
        operation_ids = {entry["operation_id"] for entry in response["result"]["operations"]}
        self.assertEqual(operation_ids, _BASE_OPERATION_IDS | _B07_OPERATION_IDS | _B12_OPERATION_IDS
                         | {"engine.v1.usage.query"})

    def test_fixture_run_lifecycle_completes_with_fixture_text(self) -> None:
        runtime = self._start_runtime(enable_application_state=True, enable_fixture_runs=True)
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        with client.session() as session:
            self._authenticate(session, descriptor, credential)
            saved = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "engine.v1.connections.save",
                    "params": {
                        "expected_revision": 0,
                        "idempotency_key": "conn-create",
                        "connection": {
                            "connection_id": CONNECTION_ID,
                            "provider_id": DETERMINISTIC_PROVIDER_ID,
                        },
                    },
                }
            )
            self.assertIn("result", saved)
            registered = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "engine.v1.models.register",
                    "params": {
                        "connection_id": CONNECTION_ID,
                        "provider_model_id": "fixture/model",
                        "display_name": "Fixture Model",
                        "expected_revision": 0,
                        "idempotency_key": "model-reg",
                    },
                }
            )
            registration_id = registered["result"]["model"]["registration_id"]
            created = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "engine.v1.sessions.create",
                    "params": {"registration_id": registration_id},
                }
            )
            session_id = created["result"]["session_id"]
            started = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "engine.v1.runs.start",
                    "params": {
                        "session_id": session_id,
                        "client_request_id": "client-1",
                        "idempotency_key": "run-1",
                        "registration_id": registration_id,
                    },
                }
            )
            self.assertIn("result", started)
            run_id = started["result"]["run"]["run_id"]
            self.assertEqual(started["result"]["run"]["state"], "completed")
            self.assertEqual(
                started["result"]["run"]["terminal_result"]["outcome"],
                "completed",
            )
            fetched = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "engine.v1.runs.get",
                    "params": {"run_id": run_id},
                }
            )
            self.assertEqual(fetched["result"]["run"]["state"], "completed")
            self.assertEqual(
                fetched["result"]["run"]["terminal_result"]["outcome"],
                "completed",
            )

    def test_runs_start_invalid_schema_maps_to_invalid_params(self) -> None:
        runtime = self._start_runtime(enable_application_state=True, enable_fixture_runs=True)
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        with client.session() as session:
            self._authenticate(session, descriptor, credential)
            response = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "engine.v1.runs.start",
                    "params": {"session_id": "not-a-uuid"},
                }
            )
        self.assertEqual(response["error"]["code"], -32602)

    def test_missing_provider_route_maps_to_not_found_on_session_create(self) -> None:
        runtime = self._start_runtime(enable_application_state=True, enable_fixture_runs=True)
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        with client.session() as session:
            self._authenticate(session, descriptor, credential)
            session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "engine.v1.connections.save",
                    "params": {
                        "expected_revision": 0,
                        "idempotency_key": "conn-create",
                        "connection": {
                            "connection_id": CONNECTION_ID,
                            "provider_id": "com.example.unconfigured",
                        },
                    },
                }
            )
            registration_id = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "engine.v1.models.register",
                    "params": {
                        "connection_id": CONNECTION_ID,
                        "provider_model_id": "fixture/model",
                        "display_name": "Fixture Model",
                        "expected_revision": 0,
                        "idempotency_key": "model-reg",
                    },
                }
            )["result"]["model"]["registration_id"]
            created = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "engine.v1.sessions.create",
                    "params": {"registration_id": registration_id},
                }
            )
        self.assertEqual(created["error"]["data"]["code"], "not_found")

    def test_host_context_session_is_rejected_for_runs_start(self) -> None:
        runtime = self._start_runtime(enable_application_state=True, enable_fixture_runs=True)
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        with client.session() as session:
            self._authenticate(session, descriptor, credential)
            session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "engine.v1.connections.save",
                    "params": {
                        "expected_revision": 0,
                        "idempotency_key": "conn-create",
                        "connection": {
                            "connection_id": CONNECTION_ID,
                            "provider_id": DETERMINISTIC_PROVIDER_ID,
                        },
                    },
                }
            )
            registration_id = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "engine.v1.models.register",
                    "params": {
                        "connection_id": CONNECTION_ID,
                        "provider_model_id": "fixture/model",
                        "display_name": "Fixture Model",
                        "expected_revision": 0,
                        "idempotency_key": "model-reg",
                    },
                }
            )["result"]["model"]["registration_id"]
            created = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "engine.v1.sessions.create",
                    "params": {
                        "registration_id": registration_id,
                        "host_context_ref": "ref:host.context",
                    },
                }
            )
            response = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "engine.v1.runs.start",
                    "params": {
                        "session_id": created["result"]["session_id"],
                        "client_request_id": "client-1",
                        "idempotency_key": "run-1",
                        "registration_id": registration_id,
                    },
                }
            )
        self.assertEqual(response["error"]["data"]["code"], "capability_denied")

    def test_principal_replays_run_admission_after_reconnect(self) -> None:
        runtime = self._start_runtime(enable_application_state=True, enable_fixture_runs=True)
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        session_id = None
        registration_id = None
        run_params = None
        first_run_id = None
        with client.session() as session:
            self._authenticate(session, descriptor, credential)
            session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "engine.v1.connections.save",
                    "params": {
                        "expected_revision": 0,
                        "idempotency_key": "conn-create-principal",
                        "connection": {
                            "connection_id": CONNECTION_ID,
                            "provider_id": DETERMINISTIC_PROVIDER_ID,
                        },
                    },
                }
            )
            registration_id = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "engine.v1.models.register",
                    "params": {
                        "connection_id": CONNECTION_ID,
                        "provider_model_id": "fixture/model",
                        "display_name": "Fixture Model",
                        "expected_revision": 0,
                        "idempotency_key": "model-reg-principal",
                    },
                }
            )["result"]["model"]["registration_id"]
            session_id = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "engine.v1.sessions.create",
                    "params": {"registration_id": registration_id},
                }
            )["result"]["session_id"]
            run_params = {
                "session_id": session_id,
                "client_request_id": "client-principal-replay",
                "idempotency_key": "principal-replay",
                "registration_id": registration_id,
            }
            started = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "engine.v1.runs.start",
                    "params": run_params,
                }
            )
            self.assertIn("result", started)
            first_run_id = started["result"]["run"]["run_id"]

        with client.session() as session:
            self._authenticate(session, descriptor, credential, client_name=CLIENT_NAME)
            replayed = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "engine.v1.runs.start",
                    "params": run_params,
                }
            )
        self.assertIn("result", replayed)
        self.assertEqual(replayed["result"]["run"]["run_id"], first_run_id)

    def test_enable_fixture_runs_requires_application_state(self) -> None:
        root = repo_root()
        state = self._temp_dir()
        artifact = self._temp_dir()
        socket_root = self._temp_dir()
        validate_isolated_roots(state, artifact, socket_root, source_root=root)
        with self.assertRaises(ValueError):
            build_engine_server(
                state_root=state,
                artifact_root=artifact,
                socket_root=socket_root,
                legacy_agents_dir=FIXTURES / "legacy_agent",
                default_connection_id=CONNECTION_ID,
                source_root=root,
                enable_application_state=False,
                enable_fixture_runs=True,
            )


from model_deck_contracts.validator import SchemaValidationError, validate_schema_ref


class ToolAdmissionIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temps: list[tempfile.TemporaryDirectory[str]] = []

    def tearDown(self) -> None:
        for td in self._temps:
            td.cleanup()

    def _temp_dir(self) -> Path:
        td = tempfile.TemporaryDirectory()
        self._temps.append(td)
        return Path(td.name)

    def _start_fixture_runtime(self):
        root = repo_root()
        state = self._temp_dir()
        artifact = self._temp_dir()
        socket_root = self._temp_dir()
        validate_isolated_roots(state, artifact, socket_root, source_root=root)
        runtime = build_engine_server(
            state_root=state,
            artifact_root=artifact,
            socket_root=socket_root,
            legacy_agents_dir=FIXTURES / "legacy_agent",
            default_connection_id=CONNECTION_ID,
            source_root=root,
            enable_application_state=True,
            enable_fixture_runs=True,
        )
        runtime.server.start()
        self.addCleanup(runtime.server.stop)
        return runtime

    def _authenticated_session(self, runtime, client, credential, client_name=CLIENT_NAME):
        from model_deck.adapters.transport.rendezvous import load_rendezvous_file as _load
        descriptor = _load(runtime.rendezvous_path)
        session = client.session()
        session.__enter__()
        self.addCleanup(session.__exit__, None, None, None)
        response = session.call(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "engine.v1.hello",
                "params": {
                    "client_name": client_name,
                    "offered_api": {"major": 1, "minor": 0},
                    "authentication": {
                        "engine_instance_id": descriptor.engine_instance_id,
                        "instance_nonce": descriptor.instance_nonce,
                        "credential": credential,
                    },
                },
            }
        )
        self.assertTrue(response["result"]["authenticated"])
        return session

    def _provision_session(self, session):
        session.call(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "engine.v1.connections.save",
                "params": {
                    "expected_revision": 0,
                    "idempotency_key": "conn-tools",
                    "connection": {
                        "connection_id": CONNECTION_ID,
                        "provider_id": DETERMINISTIC_PROVIDER_ID,
                    },
                },
            }
        )
        registration_id = session.call(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "engine.v1.models.register",
                "params": {
                    "connection_id": CONNECTION_ID,
                    "provider_model_id": "fixture/model",
                    "display_name": "Fixture Model",
                    "expected_revision": 0,
                    "idempotency_key": "model-tools",
                },
            }
        )["result"]["model"]["registration_id"]
        session_id = session.call(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "engine.v1.sessions.create",
                "params": {"registration_id": registration_id},
            }
        )["result"]["session_id"]
        return registration_id, session_id

    def test_old_call_advertisement_shape_rejected_before_dispatch(self) -> None:
        runtime = self._start_fixture_runtime()
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(load_rendezvous_file(runtime.rendezvous_path).socket_path)
        session = self._authenticated_session(runtime, client, credential)
        registration_id, session_id = self._provision_session(session)
        rejected = session.call(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "engine.v1.runs.start",
                "params": {
                    "session_id": session_id,
                    "client_request_id": "client-1",
                    "idempotency_key": "run-old-shape",
                    "registration_id": registration_id,
                    "tools": [
                        {
                            "call_id": "c1",
                            "tool_name": "search",
                            "arguments": {},
                        }
                    ],
                },
            }
        )
        self.assertIn("error", rejected)
        self.assertEqual(rejected["error"]["code"], -32602)
        retried = session.call(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "engine.v1.runs.start",
                "params": {
                    "session_id": session_id,
                    "client_request_id": "client-1",
                    "idempotency_key": "run-old-shape",
                    "registration_id": registration_id,
                },
            }
        )
        self.assertIn("result", retried)
        self.assertEqual(retried["result"]["run"]["state"], "completed")

    def test_valid_tool_advertisement_passes_schema_gate(self) -> None:
        runtime = self._start_fixture_runtime()
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(load_rendezvous_file(runtime.rendezvous_path).socket_path)
        session = self._authenticated_session(runtime, client, credential)
        registration_id, session_id = self._provision_session(session)
        response = session.call(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "engine.v1.runs.start",
                "params": {
                    "session_id": session_id,
                    "client_request_id": "client-1",
                    "idempotency_key": "run-valid-tools",
                    "registration_id": registration_id,
                    "tools": [
                        {
                            "name": "search",
                            "description": "Search the docs",
                            "input_schema": {"type": "object"},
                            "host_execution_required": True,
                        }
                    ],
                },
            }
        )
        self.assertIn("error", response)
        self.assertNotEqual(response["error"]["code"], -32602)
        self.assertEqual(response["error"]["data"]["code"], "unsupported_capability")
        without_description = session.call(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "engine.v1.runs.start",
                "params": {
                    "session_id": session_id,
                    "client_request_id": "client-2",
                    "idempotency_key": "run-valid-tools-nodesc",
                    "registration_id": registration_id,
                    "tools": [
                        {
                            "name": "search",
                            "input_schema": {"type": "object"},
                            "host_execution_required": False,
                        }
                    ],
                },
            }
        )
        self.assertIn("error", without_description)
        self.assertNotEqual(without_description["error"]["code"], -32602)
        self.assertEqual(without_description["error"]["data"]["code"], "unsupported_capability")

    def test_emitted_tool_call_shape_stays_valid_and_distinct(self) -> None:
        validate_schema_ref(
            "contracts/engine.v1/vocabulary.schema.json#/definitions/tool_call",
            {"call_id": "call-1", "tool_name": "search", "arguments": {"q": "contracts"}},
        )
        with self.assertRaises(SchemaValidationError):
            validate_schema_ref(
                "contracts/engine.v1/vocabulary.schema.json#/definitions/tool_call",
                {
                    "name": "search",
                    "description": "Search the docs",
                    "input_schema": {"type": "object"},
                    "host_execution_required": True,
                },
            )
        with self.assertRaises(SchemaValidationError):
            validate_schema_ref(
                "contracts/engine.v1/methods/runs.start.params.schema.json",
                {
                    "session_id": "550e8400-e29b-41d4-a716-446655440003",
                    "client_request_id": "client-1",
                    "idempotency_key": "idem-1",
                    "registration_id": "550e8400-e29b-41d4-a716-446655440001",
                    "tools": [{"call_id": "c1", "tool_name": "search", "arguments": {}}],
                },
            )

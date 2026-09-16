import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from model_deck.adapters.platform.macos.isolated_roots import validate_isolated_roots
from model_deck.adapters.transport.rendezvous import load_rendezvous_file
from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
from model_deck.bootstrap import build_engine_server
from collections.abc import Mapping
from types import SimpleNamespace

from model_deck.engine.dispatch import EngineDispatch
from model_deck_contracts.paths import repo_root

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CONNECTION_ID = "550e8400-e29b-41d4-a716-446655440002"
MISSING_REGISTRATION_ID = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
PROVIDER_ID = "com.example.provider"

_B07_OPERATION_EFFECTS = {
    "engine.v1.models.register": "write",
    "engine.v1.models.rename": "write",
    "engine.v1.models.remove": "write",
    "engine.v1.connections.list": "read",
    "engine.v1.connections.save": "write",
}

# Application state alone composes the evidence cache and the first-party job
# directory, so these operations are advertised with or without run wiring.
_APPLICATION_STATE_OPERATION_IDS = {
    "engine.v1.prices.query",
    "engine.v1.prices.refresh",
    "engine.v1.benchmarks.query",
    "engine.v1.benchmarks.refresh",
    "engine.v1.jobs.get",
    "engine.v1.jobs.cancel",
    "engine.v1.jobs.resume",
}

_BASE_OPERATION_IDS = {
    "engine.v1.hello",
    "engine.v1.health",
    "engine.v1.operations.list",
    "engine.v1.capabilities.get",
    "engine.v1.models.list",
}




class _DispatchListModelsStub:
    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result

    def execute(self, params: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._result


class _DispatchEnrollmentStub:
    def verify(self, engine_instance_id: str, instance_nonce: str, credential: str) -> bool:
        return True

class EngineStateDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temps: list[tempfile.TemporaryDirectory[str]] = []

    def tearDown(self) -> None:
        for td in self._temps:
            td.cleanup()

    def _temp_dir(self) -> Path:
        td = tempfile.TemporaryDirectory()
        self._temps.append(td)
        return Path(td.name)

    def _start_runtime(self, *, enable_application_state: bool):
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
        )
        runtime.server.start()
        self.addCleanup(runtime.server.stop)
        return runtime

    def _authenticate(self, session, descriptor, credential: str) -> None:
        response = session.call(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "engine.v1.hello",
                "params": {
                    "client_name": "test",
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

    def _fetch_outbox_rows(self, db_path: Path) -> list[tuple[Any, ...]]:
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute(
                "SELECT outbox_id, aggregate_type, aggregate_id, aggregate_revision, event_kind, payload_json, state "
                "FROM projection_outbox ORDER BY outbox_id"
            ).fetchall()
        finally:
            conn.close()

    def test_operations_list_advertises_b07_methods_with_effects(self) -> None:
        runtime = self._start_runtime(enable_application_state=True)
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
        self.assertIn("result", response)
        by_id = {entry["operation_id"]: entry for entry in response["result"]["operations"]}
        self.assertEqual(
            set(by_id.keys()),
            _BASE_OPERATION_IDS
            | set(_B07_OPERATION_EFFECTS.keys())
            | _APPLICATION_STATE_OPERATION_IDS,
        )
        for operation_id, effect in _B07_OPERATION_EFFECTS.items():
            self.assertEqual(by_id[operation_id]["effect"], effect)

    def test_connection_and_model_lifecycle(self) -> None:
        runtime = self._start_runtime(enable_application_state=True)
        assert runtime.application_database_path is not None
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
                            "provider_id": PROVIDER_ID,
                        },
                    },
                }
            )
            self.assertIn("result", saved)
            self.assertEqual(saved["result"]["connection"]["connection_id"], CONNECTION_ID)
            listed_connections = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "engine.v1.connections.list",
                    "params": {},
                }
            )
            self.assertEqual(len(listed_connections["result"]["connections"]), 1)
            registered = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "engine.v1.models.register",
                    "params": {
                        "connection_id": CONNECTION_ID,
                        "provider_model_id": "openrouter/test-model",
                        "display_name": "Test Model",
                        "expected_revision": 0,
                        "idempotency_key": "model-reg",
                    },
                }
            )
            self.assertIn("result", registered)
            registration_id = registered["result"]["model"]["registration_id"]
            revision = registered["result"]["model"]["revision"]
            models = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "engine.v1.models.list",
                    "params": {"collection": "registered"},
                }
            )
            self.assertEqual(len(models["result"]["items"]), 1)
            renamed = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "engine.v1.models.rename",
                    "params": {
                        "registration_id": registration_id,
                        "display_name": "Renamed Model",
                        "expected_revision": revision,
                        "idempotency_key": "model-ren",
                    },
                }
            )
            self.assertEqual(renamed["result"]["model"]["display_name"], "Renamed Model")
            new_revision = renamed["result"]["model"]["revision"]
            listed_after_rename = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "engine.v1.models.list",
                    "params": {"collection": "registered"},
                }
            )
            self.assertEqual(listed_after_rename["result"]["items"][0]["display_name"], "Renamed Model")
            self.assertEqual(listed_after_rename["result"]["items"][0]["revision"], new_revision)
            removed = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 8,
                    "method": "engine.v1.models.remove",
                    "params": {
                        "registration_id": registration_id,
                        "expected_revision": new_revision,
                        "idempotency_key": "model-rm",
                    },
                }
            )
            self.assertTrue(removed["result"]["removed"])
            empty = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "engine.v1.models.list",
                    "params": {"collection": "registered"},
                }
            )
            self.assertEqual(empty["result"]["items"], [])

    def test_idempotent_replay_returns_original_result(self) -> None:
        runtime = self._start_runtime(enable_application_state=True)
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        params = {
            "expected_revision": 0,
            "idempotency_key": "conn-replay",
            "connection": {
                "connection_id": CONNECTION_ID,
                "provider_id": PROVIDER_ID,
            },
        }
        with client.session() as session:
            self._authenticate(session, descriptor, credential)
            first = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "engine.v1.connections.save",
                    "params": params,
                }
            )
            second = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "engine.v1.connections.save",
                    "params": params,
                }
            )
        self.assertEqual(first["result"], second["result"])

    def test_stale_revision_and_idempotency_payload_map_conflict(self) -> None:
        runtime = self._start_runtime(enable_application_state=True)
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
                        "idempotency_key": "conn-base",
                        "connection": {
                            "connection_id": CONNECTION_ID,
                            "provider_id": PROVIDER_ID,
                        },
                    },
                }
            )
            stale = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "engine.v1.connections.save",
                    "params": {
                        "expected_revision": 0,
                        "idempotency_key": "conn-stale",
                        "connection": {
                            "connection_id": CONNECTION_ID,
                            "provider_id": PROVIDER_ID,
                        },
                    },
                }
            )
            self.assertEqual(stale["error"]["data"]["code"], "conflict")
            changed_key = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "engine.v1.connections.save",
                    "params": {
                        "expected_revision": 0,
                        "idempotency_key": "conn-base",
                        "connection": {
                            "connection_id": CONNECTION_ID,
                            "provider_id": "com.other.provider",
                        },
                    },
                }
            )
            self.assertEqual(changed_key["error"]["data"]["code"], "conflict")
            missing_rename = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "engine.v1.models.rename",
                    "params": {
                        "registration_id": MISSING_REGISTRATION_ID,
                        "display_name": "Missing",
                        "expected_revision": 1,
                        "idempotency_key": "missing",
                    },
                }
            )
            self.assertEqual(missing_rename["error"]["data"]["code"], "not_found")

    def test_malformed_params_map_to_invalid_params(self) -> None:
        runtime = self._start_runtime(enable_application_state=True)
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        with client.session() as session:
            self._authenticate(session, descriptor, credential)
            response = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "engine.v1.models.register",
                    "params": {"connection_id": "not-a-uuid"},
                }
            )
        self.assertEqual(response["error"]["code"], -32602)

    def test_disabled_application_state_excludes_b07_without_sqlite(self) -> None:
        runtime = self._start_runtime(enable_application_state=False)
        self.assertIsNone(runtime.application_database_path)
        state_db = runtime.rendezvous_path.parent / "state.sqlite3"
        self.assertFalse(state_db.exists())
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        with client.session() as session:
            self._authenticate(session, descriptor, credential)
            listed = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "engine.v1.operations.list",
                    "params": {},
                }
            )
            operation_ids = {entry["operation_id"] for entry in listed["result"]["operations"]}
            self.assertEqual(operation_ids, _BASE_OPERATION_IDS)
            denied = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "engine.v1.models.register",
                    "params": {
                        "connection_id": CONNECTION_ID,
                        "provider_model_id": "openrouter/test",
                        "display_name": "Test",
                        "expected_revision": 0,
                        "idempotency_key": "x",
                    },
                }
            )
        self.assertEqual(denied["error"]["data"]["code"], "unsupported_capability")

    def test_outbox_rows_exist_for_writes_without_duplicate_on_replay(self) -> None:
        runtime = self._start_runtime(enable_application_state=True)
        assert runtime.application_database_path is not None
        db_path = runtime.application_database_path
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        save_params = {
            "expected_revision": 0,
            "idempotency_key": "outbox-conn",
            "connection": {
                "connection_id": CONNECTION_ID,
                "provider_id": PROVIDER_ID,
            },
        }
        with client.session() as session:
            self._authenticate(session, descriptor, credential)
            session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "engine.v1.connections.save",
                    "params": save_params,
                }
            )
            after_first = len(self._fetch_outbox_rows(db_path))
            self.assertGreater(after_first, 0)
            session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "engine.v1.connections.save",
                    "params": save_params,
                }
            )
            after_replay = len(self._fetch_outbox_rows(db_path))
            self.assertEqual(after_first, after_replay)
            session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "engine.v1.models.register",
                    "params": {
                        "connection_id": CONNECTION_ID,
                        "provider_model_id": "openrouter/outbox-model",
                        "display_name": "Outbox",
                        "expected_revision": 0,
                        "idempotency_key": "outbox-model",
                    },
                }
            )
            after_register = len(self._fetch_outbox_rows(db_path))
            self.assertGreater(after_register, after_replay)

    def test_supplied_empty_array_params_rejected_for_no_param_b07_method(self) -> None:
        runtime = self._start_runtime(enable_application_state=True)
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        client = UnixSocketEngineClient(descriptor.socket_path)
        with client.session() as session:
            response = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "engine.v1.connections.list",
                    "params": [],
                }
            )
        self.assertEqual(response["error"]["code"], -32602)

    def test_supplied_null_params_rejected_for_no_param_b07_method(self) -> None:
        runtime = self._start_runtime(enable_application_state=True)
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        client = UnixSocketEngineClient(descriptor.socket_path)
        with client.session() as session:
            response = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "engine.v1.connections.list",
                    "params": None,
                }
            )
        self.assertEqual(response["error"]["code"], -32602)

    def test_models_list_invalid_use_case_result_maps_to_internal_error(self) -> None:
        engine_instance_id = "550e8400-e29b-41d4-a716-446655440001"
        credential = "a" * 32
        identity = SimpleNamespace(engine_instance_id=engine_instance_id, instance_nonce="nonce-test")
        dispatch = EngineDispatch(
            _DispatchListModelsStub({"items": []}),
            identity,
            _DispatchEnrollmentStub(),
        )
        connection_id = 7
        hello = dispatch.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "engine.v1.hello",
                "params": {
                    "client_name": "test",
                    "offered_api": {"major": 1, "minor": 0},
                    "authentication": {
                        "engine_instance_id": engine_instance_id,
                        "instance_nonce": identity.instance_nonce,
                        "credential": credential,
                    },
                },
            },
            connection_id,
        )
        self.assertTrue(hello["result"]["authenticated"])
        response = dispatch.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "engine.v1.models.list",
                "params": {},
            },
            connection_id,
        )
        self.assertEqual(response["error"]["code"], -32603)

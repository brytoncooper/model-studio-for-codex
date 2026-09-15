import tempfile
import unittest
from pathlib import Path
from unittest import mock
from dataclasses import replace

from model_deck.adapters.platform.macos.isolated_roots import validate_isolated_roots
from model_deck.adapters.providers.deterministic import (
    DETERMINISTIC_PROVIDER_ID, DeterministicProviderExecutionPort, EmitStarted, EmitToolRequested,
)
from model_deck.adapters.transport.rendezvous import load_rendezvous_file
from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
from model_deck.bootstrap import build_engine_server
from model_deck.engine.dispatch import _flatten_application_run_event
from model_deck.engine.runs.ports import ApplicationRunEvent
from model_deck.engine.routing.ports import CapabilityFeature, CapabilityTriState
from model_deck_contracts.paths import repo_root
from model_deck_contracts.validator import validate_schema_ref

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CONNECTION_ID = "550e8400-e29b-41d4-a716-446655440002"
CLIENT_NAME = "fixture-event-client"
OTHER_CLIENT_NAME = "fixture-event-other"

_EVENT_OPERATION_IDS = {
    "engine.v1.events.subscribe",
    "engine.v1.events.ack",
    "engine.v1.events.unsubscribe",
}

# Bootstrap imports the deterministic provider inside its fixture-run branch so a
# minimal engine loads no provider adapter at all, so a patch has to land on the
# defining module rather than on bootstrap.
_FIXTURE_PROVIDER_CLASS = (
    "model_deck.adapters.providers.deterministic.DeterministicProviderExecutionPort"
)

_CANONICAL_UNDERSCORE_OPERATION_IDS = (
    "engine.v1.sessions.select_model",
    "engine.v1.runs.submit_tool_result",
)


class EngineEventDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temps: list[tempfile.TemporaryDirectory[str]] = []

    def tearDown(self) -> None:
        for td in self._temps:
            td.cleanup()

    def _temp_dir(self) -> Path:
        td = tempfile.TemporaryDirectory()
        self._temps.append(td)
        return Path(td.name)

    def _start_runtime(self):
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

    def _read_fixture_run_notifications(
        self,
        session,
        *,
        expected_count: int = 3,
        max_reads: int = 8,
    ) -> list[dict]:
        notifications: list[dict] = []
        for _ in range(max_reads):
            notifications.append(session.read_notification())
            if len(notifications) >= expected_count:
                break
        self.assertEqual(len(notifications), expected_count)
        return notifications

    def _fixture_run_id(self, session, descriptor, credential: str, client_name: str = CLIENT_NAME, tools=None) -> str:
        self._authenticate(session, descriptor, credential, client_name=client_name)
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
                    "client_request_id": "client_request_with_underscores",
                    "idempotency_key": "idempotency_key_with_underscores",
                    "registration_id": registration_id,
                    **({"tools": tools} if tools is not None else {}),
                },
            }
        )
        return started["result"]["run"]["run_id"]

    def test_operations_list_advertises_event_methods_with_dotted_ids(self) -> None:
        runtime = self._start_runtime()
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
        validate_schema_ref(
            "contracts/engine.v1/methods/operations.list.result.schema.json",
            response["result"],
        )
        operation_ids = {entry["operation_id"] for entry in response["result"]["operations"]}
        for operation_id in _CANONICAL_UNDERSCORE_OPERATION_IDS:
            self.assertIn(operation_id, operation_ids)
        self.assertTrue(_EVENT_OPERATION_IDS.issubset(operation_ids))
        for operation_id in _EVENT_OPERATION_IDS:
            self.assertIn(".", operation_id)
            self.assertNotIn("_", operation_id.replace("engine.v1.", "").split(".")[0])

    def test_subscribe_denies_foreign_run_owner(self) -> None:
        runtime = self._start_runtime()
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        with client.session() as session:
            run_id = self._fixture_run_id(session, descriptor, credential, client_name=CLIENT_NAME)
        with client.session() as other_session:
            self._authenticate(other_session, descriptor, credential, client_name=OTHER_CLIENT_NAME)
            denied = other_session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "engine.v1.events.subscribe",
                    "params": {
                        "topics": [f"run:{run_id}"],
                        "initial_credit": 8,
                    },
                }
            )
        self.assertEqual(denied["error"]["data"]["code"], "capability_denied")

    def test_subscribe_response_precedes_drained_notifications(self) -> None:
        runtime = self._start_runtime()
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        with client.session() as session:
            run_id = self._fixture_run_id(session, descriptor, credential)
            response = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 20,
                    "method": "engine.v1.events.subscribe",
                    "params": {
                        "topics": [f"run:{run_id}"],
                        "initial_credit": 32,
                    },
                }
            )
            self.assertIn("subscription_id", response["result"])
            notification = session.read_notification()
        validate_schema_ref(
            "contracts/engine.v1/notifications/event.schema.json",
            notification,
        )
        self.assertEqual(notification["method"], "engine.v1.event")

    def test_content_delta_notification_contains_fixture_text(self) -> None:
        runtime = self._start_runtime()
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        with client.session() as session:
            run_id = self._fixture_run_id(session, descriptor, credential)
            session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 21,
                    "method": "engine.v1.events.subscribe",
                    "params": {
                        "topics": [f"run:{run_id}"],
                        "initial_credit": 32,
                    },
                }
            )
            seen_delta = False
            for _ in range(16):
                notification = session.read_notification()
                event = notification["params"]["event"]
                if event.get("kind") == "content.delta":
                    seen_delta = True
                    self.assertEqual(event.get("delta"), "fixture text")
                    break
            self.assertTrue(seen_delta)

    def test_authenticated_tool_notification_uses_frozen_nested_wire_shape(self) -> None:
        provider = DeterministicProviderExecutionPort(auto_advance=True, script=(
            EmitStarted(), EmitToolRequested("call_fixture", "lookup", {"q": "fixture"})))
        with mock.patch(_FIXTURE_PROVIDER_CLASS, return_value=provider), \
                mock.patch("model_deck.bootstrap.CapabilityFeature",
                           return_value=CapabilityFeature("tools", CapabilityTriState.SUPPORTED)):
            runtime = self._start_runtime()
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        with UnixSocketEngineClient(descriptor.socket_path).session() as session:
            run_id = self._fixture_run_id(session, descriptor, credential, tools=[{
                "name": "lookup", "input_schema": {"type": "object"}, "host_execution_required": False}])
            state = session.call({"jsonrpc": "2.0", "id": 30, "method": "engine.v1.runs.get",
                                  "params": {"run_id": run_id}})
            self.assertEqual(state["result"]["run"]["state"], "waiting_for_tool")
            subscribed = session.call({"jsonrpc": "2.0", "id": 31, "method": "engine.v1.events.subscribe",
                                       "params": {"topics": [f"run:{run_id}"], "initial_credit": 8}})
            self.assertIn("result", subscribed, subscribed)
            notifications = self._read_fixture_run_notifications(session, expected_count=2)
        for notification in notifications:
            validate_schema_ref("contracts/engine.v1/notifications/event.schema.json", notification)
        event = notifications[-1]["params"]["event"]
        self.assertEqual(event["kind"], "tool.requested")
        self.assertEqual(event["tool_call"], {"call_id": "call_fixture", "tool_name": "lookup", "arguments": {"q": "fixture"}})
        self.assertNotIn("call_id", event)

    def test_tool_wire_conversion_detaches_and_rejects_invalid_payloads(self) -> None:
        payload = {"call_id": "call_fixture", "tool_name": "lookup", "arguments": {"q": ["fixture"]}}
        event = ApplicationRunEvent(kind="tool.requested", run_id=CONNECTION_ID, session_id=CONNECTION_ID,
            sequence=2, event_schema_version=1, observed_at="2026-09-12T00:00:01Z", payload=payload)
        converted = _flatten_application_run_event(event)
        self.assertEqual(converted["tool_call"], payload)
        converted["tool_call"]["arguments"]["q"].append("later")
        self.assertEqual(payload["arguments"]["q"], ["fixture"])
        for invalid in (None, [], {}, {"tool_call": payload}, {**payload, "extra": True},
                        {**payload, "tool_call": payload}, {**payload, "call_id": 3},
                        {**payload, "tool_name": "x" * 129},
                        {"call_id": "c", "tool_name": "lookup"},
                        {**payload, "arguments": float("nan")}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                _flatten_application_run_event(replace(event, payload=invalid))

    def test_duplicate_cumulative_ack_does_not_mint_extra_credit(self) -> None:
        runtime = self._start_runtime()
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        with client.session() as session:
            run_id = self._fixture_run_id(session, descriptor, credential)
            subscribed = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 30,
                    "method": "engine.v1.events.subscribe",
                    "params": {
                        "topics": [f"run:{run_id}"],
                        "initial_credit": 1,
                    },
                }
            )
            subscription_id = subscribed["result"]["subscription_id"]
            notifications = self._read_fixture_run_notifications(
                session,
                expected_count=1,
            )
            max_sequence = max(
                notification["params"]["event"]["sequence"] for notification in notifications
            )
            first_ack = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 31,
                    "method": "engine.v1.events.ack",
                    "params": {
                        "subscription_id": subscription_id,
                        "sequence": max_sequence,
                    },
                }
            )
            credit_after_first = first_ack["result"]["credit"]
            self.assertEqual(credit_after_first, 0)
            next_notification = session.read_notification()
            self.assertEqual(
                next_notification["params"]["event"]["sequence"],
                max_sequence + 1,
            )
            duplicate_ack = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 32,
                    "method": "engine.v1.events.ack",
                    "params": {
                        "subscription_id": subscription_id,
                        "sequence": max_sequence,
                    },
                }
            )
            self.assertEqual(duplicate_ack["result"]["credit"], credit_after_first)

    def test_ack_after_owner_disconnect_returns_not_found(self) -> None:
        runtime = self._start_runtime()
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        with client.session() as session:
            run_id = self._fixture_run_id(session, descriptor, credential, client_name=CLIENT_NAME)
            subscribed = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 40,
                    "method": "engine.v1.events.subscribe",
                    "params": {
                        "topics": [f"run:{run_id}"],
                        "initial_credit": 8,
                    },
                }
            )
            subscription_id = subscribed["result"]["subscription_id"]
        with client.session() as other_session:
            self._authenticate(other_session, descriptor, credential, client_name=OTHER_CLIENT_NAME)
            denied = other_session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 41,
                    "method": "engine.v1.events.ack",
                    "params": {
                        "subscription_id": subscription_id,
                        "sequence": 1,
                    },
                }
            )
        self.assertEqual(denied["error"]["data"]["code"], "not_found")

    def test_unsubscribe_owned_subscription(self) -> None:
        runtime = self._start_runtime()
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        with client.session() as session:
            run_id = self._fixture_run_id(session, descriptor, credential)
            subscribed = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 50,
                    "method": "engine.v1.events.subscribe",
                    "params": {
                        "topics": [f"run:{run_id}"],
                        "initial_credit": 4,
                    },
                }
            )
            subscription_id = subscribed["result"]["subscription_id"]
            unsubscribed = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 51,
                    "method": "engine.v1.events.unsubscribe",
                    "params": {"subscription_id": subscription_id},
                }
            )
            self.assertTrue(unsubscribed["result"]["unsubscribed"])
            unsubscribed_again = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 52,
                    "method": "engine.v1.events.unsubscribe",
                    "params": {"subscription_id": subscription_id},
                }
            )
            self.assertEqual(unsubscribed_again["error"]["data"]["code"], "not_found")

    def test_subscribe_does_not_redispatch_provider(self) -> None:
        runtime = self._start_runtime()
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        with mock.patch(
            f"{_FIXTURE_PROVIDER_CLASS}.start",
            autospec=True,
        ) as start_mock:
            with client.session() as session:
                run_id = self._fixture_run_id(session, descriptor, credential)
                start_count_after_run = start_mock.call_count
                session.call(
                    {
                        "jsonrpc": "2.0",
                        "id": 60,
                        "method": "engine.v1.events.subscribe",
                        "params": {
                            "topics": [f"run:{run_id}"],
                            "initial_credit": 8,
                        },
                    }
                )
        self.assertEqual(start_mock.call_count, start_count_after_run)

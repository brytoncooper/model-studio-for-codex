from __future__ import annotations

import socket
import tempfile
import threading
import unittest
from pathlib import Path

from model_deck.adapters.platform.macos.isolated_roots import validate_isolated_roots
from model_deck.adapters.providers.deterministic import (
    DETERMINISTIC_PROVIDER_ID,
    DeterministicProviderExecutionPort,
    EmitContent,
    EmitStarted,
)
from model_deck.adapters.routing.registered import ProviderRouteDefinition
from model_deck.adapters.transport.framing import decode_frame, encode_frame
from model_deck.adapters.transport.rendezvous import load_rendezvous_file
from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
from model_deck.adapters.transport.unix_server import UnixSocketEngineServer
from model_deck.bootstrap import build_engine_server
from model_deck.engine.routing.ports import (
    CapabilityFeature,
    CapabilityTriState,
    ExecutionMode,
)
from model_deck_contracts.paths import repo_root


CONNECTION_ID = "550e8400-e29b-41d4-a716-446655440012"
CLIENT_NAME = "async-event-client"
READ_TIMEOUT_SECONDS = 0.5


class _CapturingDeterministicProvider(DeterministicProviderExecutionPort):
    def __init__(self, script) -> None:
        super().__init__(script=script, auto_advance=False)
        self.handles = {}

    def start(self, request, sink):
        handle = super().start(request, sink)
        self.handles[request.run_id] = handle
        return handle


class AsyncEventDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temps: list[tempfile.TemporaryDirectory[str]] = []

    def tearDown(self) -> None:
        for temp_dir in self._temps:
            temp_dir.cleanup()

    def _temp_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self._temps.append(temp_dir)
        return Path(temp_dir.name)

    def _start_runtime(self, provider: _CapturingDeterministicProvider):
        root = repo_root()
        state_root = self._temp_dir()
        artifact_root = self._temp_dir()
        socket_root = self._temp_dir()
        validate_isolated_roots(
            state_root,
            artifact_root,
            socket_root,
            source_root=root,
        )
        runtime = build_engine_server(
            state_root=state_root,
            artifact_root=artifact_root,
            socket_root=socket_root,
            legacy_agents_dir=(
                Path(__file__).resolve().parent / "fixtures" / "legacy_agent"
            ),
            default_connection_id=CONNECTION_ID,
            source_root=root,
            enable_application_state=True,
            provider_execution=provider,
            provider_route_definitions={
                DETERMINISTIC_PROVIDER_ID: ProviderRouteDefinition(
                    execution_mode=ExecutionMode.CUSTOM,
                    capability_features=(
                        CapabilityFeature("tools", CapabilityTriState.UNSUPPORTED),
                    ),
                    capability_snapshot_ref="ref:capability.async-event-test",
                )
            },
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
                    "client_name": CLIENT_NAME,
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

    def _create_run(self, session, descriptor, credential: str) -> str:
        self._authenticate(session, descriptor, credential)
        session.call(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "engine.v1.connections.save",
                "params": {
                    "expected_revision": 0,
                    "idempotency_key": "async-connection-create",
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
                    "provider_model_id": "async/model",
                    "display_name": "Async Model",
                    "expected_revision": 0,
                    "idempotency_key": "async-model-register",
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
        started = session.call(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "engine.v1.runs.start",
                "params": {
                    "session_id": created["result"]["session_id"],
                    "client_request_id": "async-client-request",
                    "idempotency_key": "async-run-start",
                    "registration_id": registration_id,
                },
            }
        )
        return started["result"]["run"]["run_id"]

    def _subscribe(self, session, run_id: str, *, credit: int) -> str:
        response = session.call(
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "engine.v1.events.subscribe",
                "params": {
                    "topics": [f"run:{run_id}"],
                    "initial_credit": credit,
                },
            }
        )
        return response["result"]["subscription_id"]

    def test_notification_checks_preserve_a_partial_request_frame(self) -> None:
        socket_path = self._temp_dir() / "partial-frame.sock"
        checked_while_idle = threading.Event()
        check_count = 0

        def handler(frame, _connection_id, _stop):
            return {
                "jsonrpc": "2.0",
                "id": frame["id"],
                "result": {"handled": True},
            }

        def notification_provider(_connection_id):
            nonlocal check_count
            check_count += 1
            if check_count >= 2:
                checked_while_idle.set()
            return ()

        server = UnixSocketEngineServer(
            socket_path,
            handler,
            notification_provider=notification_provider,
        )
        server.start()
        self.addCleanup(server.stop)

        request = encode_frame(
            {
                "jsonrpc": "2.0",
                "id": 99,
                "method": "engine.v1.ping",
                "params": {},
            }
        )
        split_at = len(request) // 2
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.settimeout(1.0)
        client.connect(str(socket_path))
        client.sendall(request[:split_at])
        self.assertTrue(checked_while_idle.wait(timeout=1.0))
        client.sendall(request[split_at:])

        response_buffer = bytearray(client.recv(65536))
        response = decode_frame(response_buffer)
        self.assertEqual(response["id"], 99)
        self.assertEqual(response["result"], {"handled": True})

    def test_idle_client_receives_delayed_event_and_ack_releases_credit(self) -> None:
        provider = _CapturingDeterministicProvider(
            (EmitStarted(), EmitContent("after ack"))
        )
        runtime = self._start_runtime(provider)
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(
            encoding="utf-8"
        ).strip()

        client = UnixSocketEngineClient(
            descriptor.socket_path,
            timeout_seconds=READ_TIMEOUT_SECONDS,
        )
        with client.session() as session:
            run_id = self._create_run(session, descriptor, credential)
            subscription_id = self._subscribe(session, run_id, credit=1)

            self.assertTrue(provider.handles[run_id].advance())
            first = session.read_notification()
            self.assertEqual(first["params"]["subscription_id"], subscription_id)
            self.assertEqual(first["params"]["event"]["kind"], "run.started")

            self.assertTrue(provider.handles[run_id].advance())
            with self.assertRaises(TimeoutError):
                session.read_notification()

            sequence = first["params"]["event"]["sequence"]
            acknowledged = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "engine.v1.events.ack",
                    "params": {
                        "subscription_id": subscription_id,
                        "sequence": sequence,
                    },
                }
            )
            self.assertIn("result", acknowledged)
            self.assertEqual(acknowledged["result"]["credit"], 0)
            second = session.read_notification()

        self.assertEqual(second["params"]["subscription_id"], subscription_id)
        self.assertEqual(second["params"]["event"]["kind"], "content.delta")
        self.assertEqual(second["params"]["event"]["delta"], "after ack")

    def test_unsubscribed_and_disconnected_subscriptions_receive_no_events(self) -> None:
        provider = _CapturingDeterministicProvider((EmitStarted(),))
        runtime = self._start_runtime(provider)
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(
            encoding="utf-8"
        ).strip()
        client = UnixSocketEngineClient(
            descriptor.socket_path,
            timeout_seconds=READ_TIMEOUT_SECONDS,
        )

        with client.session() as session:
            run_id = self._create_run(session, descriptor, credential)
            subscription_id = self._subscribe(session, run_id, credit=1)
            unsubscribed = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 12,
                    "method": "engine.v1.events.unsubscribe",
                    "params": {"subscription_id": subscription_id},
                }
            )
            self.assertTrue(unsubscribed["result"]["unsubscribed"])
            self.assertTrue(provider.handles[run_id].advance())
            with self.assertRaises(TimeoutError):
                session.read_notification()

        closed_provider = _CapturingDeterministicProvider((EmitStarted(),))
        closed_runtime = self._start_runtime(closed_provider)
        closed_descriptor = load_rendezvous_file(closed_runtime.rendezvous_path)
        closed_credential = closed_runtime.enrollment.credential_path.read_text(
            encoding="utf-8"
        ).strip()
        closed_client = UnixSocketEngineClient(
            closed_descriptor.socket_path,
            timeout_seconds=READ_TIMEOUT_SECONDS,
        )
        socket_server = closed_runtime.server._server
        dispatch = socket_server._notification_provider.__self__
        event_replay = dispatch._event_replay
        disconnect_complete = threading.Event()
        original_disconnect = socket_server._on_disconnect

        def observe_disconnect(connection_id):
            original_disconnect(connection_id)
            disconnect_complete.set()

        socket_server._on_disconnect = observe_disconnect
        with closed_client.session() as first_session:
            closed_run_id = self._create_run(
                first_session,
                closed_descriptor,
                closed_credential,
            )
            closed_subscription_id = self._subscribe(
                first_session,
                closed_run_id,
                credit=1,
            )
            self.assertIn(closed_subscription_id, dispatch._subscriptions)
            self.assertIn(closed_subscription_id, event_replay._subscriptions)

        self.assertTrue(disconnect_complete.wait(timeout=1.0))
        self.assertNotIn(closed_subscription_id, dispatch._subscriptions)
        self.assertNotIn(closed_subscription_id, event_replay._subscriptions)
        self.assertTrue(closed_provider.handles[closed_run_id].advance())
        with closed_client.session() as later_session:
            self._authenticate(later_session, closed_descriptor, closed_credential)
            missing = later_session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 13,
                    "method": "engine.v1.events.ack",
                    "params": {
                        "subscription_id": closed_subscription_id,
                        "sequence": 1,
                    },
                }
            )
            self.assertEqual(
                missing["error"]["data"]["code"],
                "not_found",
            )
            with self.assertRaises(TimeoutError):
                later_session.read_notification()

    def test_active_subscriptions_are_isolated_by_connection(self) -> None:
        provider = _CapturingDeterministicProvider((EmitStarted(),))
        runtime = self._start_runtime(provider)
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(
            encoding="utf-8"
        ).strip()
        client = UnixSocketEngineClient(
            descriptor.socket_path,
            timeout_seconds=READ_TIMEOUT_SECONDS,
        )

        with client.session() as owning_session, client.session() as other_session:
            run_id = self._create_run(owning_session, descriptor, credential)
            subscription_id = self._subscribe(owning_session, run_id, credit=1)
            self._authenticate(other_session, descriptor, credential)

            self.assertTrue(provider.handles[run_id].advance())
            notification = owning_session.read_notification()
            self.assertEqual(
                notification["params"]["subscription_id"],
                subscription_id,
            )
            with self.assertRaises(TimeoutError):
                other_session.read_notification()

            denied = other_session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 14,
                    "method": "engine.v1.events.ack",
                    "params": {
                        "subscription_id": subscription_id,
                        "sequence": notification["params"]["event"]["sequence"],
                    },
                }
            )
            self.assertEqual(denied["error"]["data"]["code"], "capability_denied")


if __name__ == "__main__":
    unittest.main()

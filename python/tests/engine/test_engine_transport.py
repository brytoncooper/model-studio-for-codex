import json
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

from model_deck.adapters.platform.macos.isolated_roots import validate_isolated_roots
from model_deck.adapters.credentials.file_enrollment import FileEnrollmentCredentialStore
from model_deck.adapters.transport.rendezvous import load_rendezvous_file
from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
from model_deck.bootstrap import build_engine_server
from model_deck.engine.server import EngineServer
from model_deck.integrations.hosts.codex.legacy_models import LegacyCodexModelRepository
from model_deck_contracts.paths import repo_root

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CONNECTION = "550e8400-e29b-41d4-a716-446655440002"


class EngineTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temps = []

    def _temp_dir(self) -> Path:
        td = tempfile.TemporaryDirectory()
        self._temps.append(td)
        return Path(td.name)

    def tearDown(self) -> None:
        for td in self._temps:
            td.cleanup()

    def _start_server(self, *, catalog_cache_path: Path | None = None):
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
            default_connection_id=CONNECTION,
            source_root=root,
            catalog_cache_path=catalog_cache_path,
            # Without an application database the caller owns the registered
            # models; these tests serve the legacy Codex fixtures.
            model_repository=LegacyCodexModelRepository(
                FIXTURES / "legacy_agent",
                default_connection_id=CONNECTION,
            ),
        )
        runtime.server.start()
        self.addCleanup(runtime.server.stop)
        return runtime

    def test_session_auth_allows_models_list_on_same_connection(self) -> None:
        runtime = self._start_server()
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        with client.session() as session:
            first = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "engine.v1.hello",
                    "params": {
                        "client_name": "test",
                        "offered_api": {"major": 1, "minor": 0},
                    },
                }
            )
            self.assertIn("result", first)
            second = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
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
            self.assertTrue(second["result"]["authenticated"])
            listed = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "engine.v1.models.list",
                    "params": {"collection": "registered"},
                }
            )
        self.assertIn("result", listed)
        self.assertEqual(len(listed["result"]["items"]), 1)

    def test_per_call_client_fails_models_list_after_authenticated_hello(self) -> None:
        runtime = self._start_server()
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        client.call(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "engine.v1.hello",
                "params": {
                    "client_name": "test",
                    "offered_api": {"major": 1, "minor": 0},
                },
            }
        )
        client.call(
            {
                "jsonrpc": "2.0",
                "id": 2,
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
        denied = client.call(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "engine.v1.models.list",
                "params": {"collection": "registered"},
            }
        )
        self.assertIn("error", denied)

    def test_disconnect_does_not_affect_other_connection_sessions(self) -> None:
        runtime = self._start_server()
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        with client.session() as session_a:
            session_a.call(
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
            with client.session() as session_b:
                session_b.call(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "engine.v1.hello",
                        "params": {
                            "client_name": "test",
                            "offered_api": {"major": 1, "minor": 0},
                        },
                    }
                )
            listed = session_a.call(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "engine.v1.models.list",
                    "params": {"collection": "registered"},
                }
            )
        self.assertIn("result", listed)

    def test_operations_list_enumerates_implemented_methods(self) -> None:
        runtime = self._start_server()
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        with client.session() as session:
            session.call(
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
            response = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "engine.v1.operations.list",
                    "params": {},
                }
            )
        self.assertIn("result", response)
        operation_ids = {entry["operation_id"] for entry in response["result"]["operations"]}
        self.assertEqual(
            operation_ids,
            {
                "engine.v1.hello",
                "engine.v1.health",
                "engine.v1.operations.list",
                "engine.v1.capabilities.get",
                "engine.v1.models.list",
            },
        )

    def test_capabilities_advertise_tools_unsupported(self) -> None:
        runtime = self._start_server()
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        with client.session() as session:
            session.call(
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
            response = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "engine.v1.capabilities.get",
                    "params": {},
                }
            )
        self.assertEqual(response["result"]["features"]["tools"], "unsupported")

    def test_invalid_credential_rejected(self) -> None:
        runtime = self._start_server()
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        client = UnixSocketEngineClient(descriptor.socket_path)
        with client.session() as session:
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
                            "credential": "wrong-credential-value",
                        },
                    },
                }
            )
        self.assertIn("error", response)
        data = response["error"].get("data", {})
        self.assertEqual(data.get("code"), "capability_denied")


    def test_models_list_catalog_collection_uses_fixture_cache(self) -> None:
        runtime = self._start_server(
            catalog_cache_path=FIXTURES / "catalog" / "openrouter_sample.json",
        )
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        with client.session() as session:
            first = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "engine.v1.hello",
                    "params": {
                        "client_name": "test",
                        "offered_api": {"major": 1, "minor": 0},
                    },
                }
            )
            self.assertIn("result", first)
            self.assertFalse(first["result"]["authenticated"])
            self.assertEqual(
                first["result"]["engine_instance_id"],
                descriptor.engine_instance_id,
            )
            self.assertEqual(
                first["result"]["instance_nonce"],
                descriptor.instance_nonce,
            )
            second = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
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
            self.assertIn("result", second)
            self.assertTrue(second["result"]["authenticated"])
            self.assertEqual(
                second["result"]["engine_instance_id"],
                descriptor.engine_instance_id,
            )
            self.assertEqual(
                second["result"]["instance_nonce"],
                descriptor.instance_nonce,
            )
            listed = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "engine.v1.models.list",
                    "params": {"collection": "catalog", "connection_id": CONNECTION},
                }
            )
        self.assertIn("result", listed)
        self.assertTrue(listed["result"]["cache_only"])
        self.assertEqual(listed["result"]["collection"], "catalog")
        self.assertEqual(len(listed["result"]["items"]), 4)

    def test_models_list_catalog_unavailable_without_cache(self) -> None:
        runtime = self._start_server()
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        with client.session() as session:
            session.call(
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
            denied = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "engine.v1.models.list",
                    "params": {"collection": "catalog", "connection_id": CONNECTION},
                }
            )
        self.assertIn("error", denied)
        data = denied["error"].get("data", {})
        self.assertEqual(data.get("code"), "unsupported_capability")


    def test_validate_isolated_roots_uses_root_guard_authority(self) -> None:
        from model_deck_root_guard.roots import validate_isolated_roots as authority_validate

        from model_deck.adapters.platform.macos.isolated_roots import (
            validate_isolated_roots as runtime_validate,
        )

        self.assertIs(runtime_validate, authority_validate)

    def test_validate_isolated_roots_accepts_disjoint_temp_dirs(self) -> None:
        root = repo_root()
        state = self._temp_dir()
        artifact = self._temp_dir()
        socket_root = self._temp_dir()
        resolved = validate_isolated_roots(state, artifact, socket_root, source_root=root)
        self.assertEqual(resolved[0], state.resolve())

    def test_rendezvous_fixture_shape(self) -> None:
        descriptor = load_rendezvous_file(FIXTURES / "rendezvous_minimal.json")
        self.assertEqual(descriptor.transport, "unix")
        self.assertTrue(descriptor.socket_path.is_absolute())

    def test_listener_starts_before_rendezvous_publish(self) -> None:
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
            default_connection_id=CONNECTION,
            source_root=root,
        )
        socket_path = socket_root / "engine.sock"
        self.assertFalse(socket_path.exists())
        self.assertFalse(runtime.rendezvous_path.exists())
        runtime.server.start()
        try:
            self.assertTrue(socket_path.exists())
            self.assertTrue(runtime.rendezvous_path.exists())
        finally:
            runtime.server.stop()

    def test_identity_load_deferred_until_server_start(self) -> None:
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
            default_connection_id=CONNECTION,
            source_root=root,
        )
        store = runtime.enrollment
        with unittest.mock.patch.object(store, "load_or_create", wraps=store.load_or_create) as tracked:
            self.assertEqual(tracked.call_count, 0)
            runtime.server.start()
            try:
                self.assertGreater(tracked.call_count, 0)
            finally:
                runtime.server.stop()

    def test_enrollment_refuses_symlink_engine_directory(self) -> None:
        root = self._temp_dir()
        real_engine = root / "real_engine"
        real_engine.mkdir(parents=True)
        engine_link = root / "engine"
        engine_link.symlink_to(real_engine)
        store = FileEnrollmentCredentialStore(root)
        with self.assertRaises(OSError):
            store.load_or_create()


class EngineServerRollbackTests(unittest.TestCase):
    class _Lock:
        def __init__(self) -> None:
            self.held = False

        def acquire(self, _timeout: float) -> bool:
            if self.held:
                return False
            self.held = True
            return True

        def release(self) -> None:
            self.held = False

    class _Socket:
        def __init__(self) -> None:
            self.started = False
            self.stopped = False

        def start(self) -> None:
            self.started = True

        def stop(self) -> None:
            self.stopped = True
            self.started = False

    def test_publish_failure_stops_listener_and_releases_lock(self) -> None:
        lock = self._Lock()
        socket = self._Socket()

        def publish(_payload: dict) -> None:
            raise OSError("publish failed")

        server = EngineServer(
            instance_lock=lock,
            socket_server=socket,
            rendezvous_payload_builder=lambda: {"transport": "unix"},
            rendezvous_publish=publish,
        )
        with self.assertRaises(OSError):
            server.start()
        self.assertFalse(lock.held)
        self.assertTrue(socket.stopped)
        self.assertFalse(socket.started)


    def test_stop_releases_lock_when_socket_stop_raises(self) -> None:
        lock = self._Lock()

        class _RaisingSocket:
            def start(self) -> None:
                return None

            def stop(self) -> None:
                raise RuntimeError("stop failed")

        server = EngineServer(
            instance_lock=lock,
            socket_server=_RaisingSocket(),
            rendezvous_payload_builder=lambda: {"ok": True},
            rendezvous_publish=lambda _payload: None,
        )
        server.start()
        with self.assertRaises(RuntimeError):
            server.stop()
        self.assertFalse(lock.held)

    def test_stop_is_idempotent(self) -> None:
        lock = self._Lock()
        socket = self._Socket()
        published: list[dict] = []

        def publish(payload: dict) -> None:
            published.append(payload)

        server = EngineServer(
            instance_lock=lock,
            socket_server=socket,
            rendezvous_payload_builder=lambda: {"ok": True},
            rendezvous_publish=publish,
        )
        server.start()
        server.stop()
        server.stop()
        self.assertFalse(lock.held)

import socket
import stat

from model_deck.adapters.transport.framing import (
    MAX_FRAME_BYTES,
    FrameError,
    decode_frame,
    encode_frame,
)
from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
from model_deck.adapters.transport.unix_server import UnixSocketEngineServer



class UnixSocketEngineEventTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temps: list[tempfile.TemporaryDirectory[str]] = []

    def tearDown(self) -> None:
        for td in self._temps:
            td.cleanup()

    def _temp_dir(self) -> Path:
        td = tempfile.TemporaryDirectory()
        self._temps.append(td)
        return Path(td.name)

    def test_call_queues_notification_received_before_matching_response(self) -> None:
        root = self._temp_dir()
        socket_path = root / "engine.sock"
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        ready = threading.Event()

        def serve() -> None:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(socket_path))
            server.listen(1)
            ready.set()
            conn, _addr = server.accept()
            conn.recv(65536)
            notification = {
                "jsonrpc": "2.0",
                "method": "engine.v1.event",
                "params": {
                    "subscription_id": "sub-1",
                    "event": {"type": "run.started", "run_id": "run-1"},
                },
            }
            response = {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}
            conn.sendall(encode_frame(notification))
            conn.sendall(encode_frame(response))
            conn.close()
            server.close()

        threading.Thread(target=serve, daemon=True).start()
        self.assertTrue(ready.wait(timeout=2.0))
        client = UnixSocketEngineClient(socket_path, timeout_seconds=2.0)
        with client.session() as session:
            result = session.call(
                {"jsonrpc": "2.0", "id": 7, "method": "engine.v1.ping", "params": {}}
            )
            self.assertEqual(result.get("result"), {"ok": True})
            queued = session.read_notification()
        self.assertEqual(queued.get("method"), "engine.v1.event")
        self.assertEqual(
            queued.get("params", {}).get("subscription_id"),
            "sub-1",
        )

    def test_server_sends_response_before_notifications(self) -> None:
        root = self._temp_dir()
        socket_path = root / "engine.sock"

        def handler(
            frame: dict,
            _connection_id: int,
            _stop: threading.Event,
        ) -> dict:
            return {
                "jsonrpc": "2.0",
                "id": frame["id"],
                "result": {"handled": True},
            }

        def notification_provider(_connection_id: int) -> tuple[dict, ...]:
            return (
                {
                    "jsonrpc": "2.0",
                    "method": "engine.v1.event",
                    "params": {
                        "subscription_id": "sub-2",
                        "event": {"type": "content.delta"},
                    },
                },
            )

        server = UnixSocketEngineServer(
            socket_path,
            handler,
            notification_provider=notification_provider,
        )
        server.start()
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(socket_path))
            client.sendall(
                encode_frame(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "engine.v1.ping",
                        "params": {},
                    }
                )
            )
            client.settimeout(2.0)
            buffer = bytearray()
            frames: list[dict] = []
            while len(frames) < 2:
                chunk = client.recv(4096)
                if not chunk:
                    break
                buffer.extend(chunk)
                while True:
                    frame = decode_frame(buffer)
                    if frame is None:
                        break
                    frames.append(frame)
            client.close()
        finally:
            server.stop()
        self.assertEqual(len(frames), 2)
        first = frames[0]
        second = frames[1]
        self.assertEqual(first.get("id"), 3)
        self.assertIn("result", first)
        self.assertNotIn("id", second)
        self.assertEqual(second.get("method"), "engine.v1.event")

    def test_read_notification_receives_post_response_notification(self) -> None:
        root = self._temp_dir()
        socket_path = root / "engine.sock"

        def handler(
            frame: dict,
            _connection_id: int,
            _stop: threading.Event,
        ) -> dict:
            return {
                "jsonrpc": "2.0",
                "id": frame["id"],
                "result": {"subscribed": True},
            }

        event_payload = {
            "type": "run.completed",
            "run_id": "run-9",
        }

        def notification_provider(_connection_id: int) -> tuple[dict, ...]:
            return (
                {
                    "jsonrpc": "2.0",
                    "method": "engine.v1.event",
                    "params": {
                        "subscription_id": "sub-9",
                        "event": event_payload,
                    },
                },
            )

        server = UnixSocketEngineServer(
            socket_path,
            handler,
            notification_provider=notification_provider,
        )
        server.start()
        try:
            client = UnixSocketEngineClient(socket_path, timeout_seconds=2.0)
            with client.session() as session:
                response = session.call(
                    {
                        "jsonrpc": "2.0",
                        "id": 11,
                        "method": "engine.v1.events.subscribe",
                        "params": {"topic": "run:run-9"},
                    }
                )
                self.assertEqual(response.get("result"), {"subscribed": True})
                notification = session.read_notification()
        finally:
            server.stop()
        self.assertEqual(notification.get("method"), "engine.v1.event")
        self.assertEqual(
            notification.get("params", {}).get("event"),
            event_payload,
        )


class UnixTransportSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temps: list[tempfile.TemporaryDirectory[str]] = []

    def tearDown(self) -> None:
        for td in self._temps:
            td.cleanup()

    def _temp_dir(self) -> Path:
        td = tempfile.TemporaryDirectory()
        self._temps.append(td)
        return Path(td.name)

    def test_decode_frame_rejects_overlong_unterminated_buffer(self) -> None:
        buffer = bytearray(b"x" * (MAX_FRAME_BYTES + 1))
        with self.assertRaises(FrameError):
            decode_frame(buffer)

    def test_start_sets_private_directory_and_socket_modes(self) -> None:
        root = self._temp_dir()
        socket_path = root / "sockets" / "engine.sock"
        server = UnixSocketEngineServer(socket_path, lambda *_args: None)
        server.start()
        try:
            dir_mode = stat.S_IMODE(socket_path.parent.stat().st_mode)
            sock_mode = stat.S_IMODE(socket_path.stat().st_mode)
            self.assertEqual(dir_mode, 0o700)
            self.assertEqual(sock_mode, 0o600)
        finally:
            server.stop()

    def test_start_refuses_existing_non_socket_path(self) -> None:
        root = self._temp_dir()
        socket_path = root / "engine.sock"
        socket_path.write_text("not a socket", encoding="utf-8")
        server = UnixSocketEngineServer(socket_path, lambda *_args: None)
        with self.assertRaises(OSError):
            server.start()

    def test_peer_credential_checker_denies_mismatched_peer(self) -> None:
        root = self._temp_dir()
        socket_path = root / "engine.sock"
        handled: list[int] = []

        def handler(_frame: dict, connection_id: int, _stop: threading.Event) -> dict | None:
            handled.append(connection_id)
            return {"jsonrpc": "2.0", "id": 1, "result": {}}

        server = UnixSocketEngineServer(
            socket_path,
            handler,
            peer_credential_checker=lambda _conn: False,
        )
        server.start()
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(socket_path))
            client.settimeout(1.0)
            data = client.recv(4096)
            self.assertEqual(data, b"")
            client.close()
        finally:
            server.stop()
        self.assertEqual(handled, [])

    def test_overlong_unterminated_frame_rejects_with_parse_error(self) -> None:
        root = self._temp_dir()
        socket_path = root / "engine.sock"
        server = UnixSocketEngineServer(socket_path, lambda *_args: None)
        server.start()
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(socket_path))
            client.sendall(b"a" * (MAX_FRAME_BYTES + 1))
            client.settimeout(2.0)
            data = client.recv(65536)
            client.close()
        finally:
            server.stop()
        self.assertTrue(data)
        payload = json.loads(data.decode("utf-8").strip())
        self.assertEqual(payload.get("error", {}).get("code"), -32700)

    def test_default_peer_checker_allows_same_uid_connection(self) -> None:
        root = self._temp_dir()
        socket_path = root / "engine.sock"
        handled: list[int] = []

        def handler(_frame: dict, connection_id: int, _stop: threading.Event) -> dict | None:
            handled.append(connection_id)
            return {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}

        server = UnixSocketEngineServer(socket_path, handler)
        server.start()
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(socket_path))
            client.sendall(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
            client.settimeout(2.0)
            data = client.recv(65536)
            client.close()
        finally:
            server.stop()
        self.assertEqual(len(handled), 1)
        response = json.loads(data.decode("utf-8").strip())
        self.assertEqual(response.get("result"), {"ok": True})

class UnixSocketEngineClientTimeoutTests(unittest.TestCase):
    def test_connect_failure_closes_socket_without_open_session(self) -> None:
        socket_path = Path("/tmp/model-deck-missing.sock")
        client = UnixSocketEngineClient(socket_path, timeout_seconds=2.0)
        mock_sock = unittest.mock.MagicMock()
        mock_sock.connect.side_effect = ConnectionRefusedError("refused")
        with unittest.mock.patch(
            "model_deck.adapters.transport.unix_client.socket.socket",
            return_value=mock_sock,
        ):
            with self.assertRaises(ConnectionRefusedError):
                with client.session():
                    pass
        mock_sock.settimeout.assert_called_once_with(2.0)
        mock_sock.close.assert_called_once()

    def test_injected_timeout_is_applied_before_connect(self) -> None:
        socket_path = Path("/tmp/model-deck-timeout.sock")
        client = UnixSocketEngineClient(socket_path, timeout_seconds=3.5)
        mock_sock = unittest.mock.MagicMock()
        order: list[str] = []

        def settimeout(value: float) -> None:
            order.append(f"settimeout:{value}")

        def connect(_path: str) -> None:
            order.append("connect")

        mock_sock.settimeout.side_effect = settimeout
        mock_sock.connect.side_effect = connect
        with unittest.mock.patch(
            "model_deck.adapters.transport.unix_client.socket.socket",
            return_value=mock_sock,
        ):
            with client.session():
                pass
        self.assertEqual(order, ["settimeout:3.5", "connect"])

    def test_call_recv_timeout_raises_stable_error_and_closes_session(self) -> None:
        socket_path = Path("/tmp/model-deck-recv-timeout.sock")
        client = UnixSocketEngineClient(socket_path, timeout_seconds=1.0)
        mock_sock = unittest.mock.MagicMock()
        mock_sock.recv.side_effect = socket.timeout("timed out")
        with unittest.mock.patch(
            "model_deck.adapters.transport.unix_client.socket.socket",
            return_value=mock_sock,
        ):
            with client.session() as session:
                with self.assertRaises(TimeoutError) as ctx:
                    session.call({"jsonrpc": "2.0", "id": 1, "method": "hello"})
                self.assertEqual(str(ctx.exception), "engine call timed out")
        mock_sock.close.assert_called()


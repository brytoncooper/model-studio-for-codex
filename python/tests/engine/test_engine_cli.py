import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from model_deck.adapters.transport.rendezvous import RendezvousError, load_rendezvous_file
from model_deck.cli import main as cli_main

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class EngineCliRendezvousTests(unittest.TestCase):
    def test_minimal_fixture_parses(self) -> None:
        descriptor = load_rendezvous_file(FIXTURES / "rendezvous_minimal.json")
        self.assertEqual(descriptor.api_profile.major, 1)

    def test_rejects_extra_fields(self) -> None:
        payload = json.loads((FIXTURES / "rendezvous_minimal.json").read_text(encoding="utf-8"))
        payload["credential_path"] = "/secret"
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(payload, handle)
            path = Path(handle.name)
        try:
            with self.assertRaises(RendezvousError):
                load_rendezvous_file(path)
        finally:
            path.unlink()

    def test_rejects_non_uuid_engine_instance_id(self) -> None:
        payload = json.loads((FIXTURES / "rendezvous_minimal.json").read_text(encoding="utf-8"))
        payload["engine_instance_id"] = "not-a-uuid"
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(payload, handle)
            path = Path(handle.name)
        try:
            with self.assertRaises(RendezvousError):
                load_rendezvous_file(path)
        finally:
            path.unlink()

    def test_models_list_absent_engine_returns_nonzero(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(
                {
                    "transport": "unix",
                    "socket_path": "/tmp/model-deck-missing-engine.sock",
                    "engine_instance_id": "550e8400-e29b-41d4-a716-446655440099",
                    "instance_nonce": "nonce",
                    "api_profile": {"major": 1, "minor": 0},
                },
                handle,
            )
            rendezvous = Path(handle.name)
        try:
            with tempfile.NamedTemporaryFile("w", delete=False) as cred:
                cred.write("secret")
                cred_path = Path(cred.name)
            try:
                exit_code = cli_main.main(
                    [
                        "models",
                        "list",
                        "--rendezvous",
                        str(rendezvous),
                        "--credential",
                        str(cred_path),
                    ]
                )
            finally:
                cred_path.unlink()
        finally:
            rendezvous.unlink()
        self.assertEqual(exit_code, 1)

    def test_models_list_reads_credential_only_after_challenge(self) -> None:
        payload = json.loads((FIXTURES / "rendezvous_minimal.json").read_text(encoding="utf-8"))
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(payload, handle)
            rendezvous = Path(handle.name)
        with tempfile.NamedTemporaryFile("w", delete=False) as cred:
            cred.write("secret")
            cred_path = Path(cred.name)
        hello_completed = False
        credential_reads = 0
        original_read_text = Path.read_text
        test_case = self

        def tracking_read_text(path, *args, **kwargs):
            nonlocal credential_reads
            if path.resolve() == cred_path.resolve():
                test_case.assertTrue(
                    hello_completed,
                    "credential read before hello-1 challenge completed",
                )
                credential_reads += 1
            return original_read_text(path, *args, **kwargs)

        try:
            with mock.patch.object(Path, "read_text", tracking_read_text):
                with mock.patch(
                    "model_deck.cli.main.UnixSocketEngineClient",
                ) as client_cls:
                    session = mock.MagicMock()
                    session.__enter__.return_value = session
                    session.__exit__.return_value = False

                    call_count = 0

                    def on_call(_frame):
                        nonlocal hello_completed, call_count
                        call_count += 1
                        if call_count == 1:
                            hello_completed = True
                            return {
                                "result": {
                                    "authenticated": False,
                                    "api_profile": {"major": 1, "minor": 0},
                                    "engine_instance_id": payload["engine_instance_id"],
                                    "instance_nonce": payload["instance_nonce"],
                                }
                            }
                        if call_count == 2:
                            return {
                                "result": {
                                    "authenticated": True,
                                    "api_profile": {"major": 1, "minor": 0},
                                    "engine_instance_id": payload["engine_instance_id"],
                                    "instance_nonce": payload["instance_nonce"],
                                }
                            }
                        return {"result": {"items": []}}

                    session.call.side_effect = on_call
                    client_cls.return_value.session.return_value = session
                    exit_code = cli_main.main(
                        [
                            "models",
                            "list",
                            "--rendezvous",
                            str(rendezvous),
                            "--credential",
                            str(cred_path),
                        ]
                    )
            self.assertEqual(exit_code, 0)
            self.assertEqual(credential_reads, 1)
        finally:
            rendezvous.unlink()
            cred_path.unlink()

    def test_models_list_rejects_hello2_descriptor_mismatch(self) -> None:
        payload = json.loads((FIXTURES / "rendezvous_minimal.json").read_text(encoding="utf-8"))
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(payload, handle)
            rendezvous = Path(handle.name)
        with tempfile.NamedTemporaryFile("w", delete=False) as cred:
            cred.write("secret")
            cred_path = Path(cred.name)
        try:
            with mock.patch(
                "model_deck.cli.main.UnixSocketEngineClient",
            ) as client_cls:
                session = mock.MagicMock()
                session.__enter__.return_value = session
                session.__exit__.return_value = False

                call_count = 0

                def on_call(_frame):
                    nonlocal call_count
                    call_count += 1
                    if call_count == 1:
                        return {
                            "result": {
                                "authenticated": False,
                                "api_profile": {"major": 1, "minor": 0},
                                "engine_instance_id": payload["engine_instance_id"],
                                "instance_nonce": payload["instance_nonce"],
                            }
                        }
                    if call_count == 2:
                        return {
                            "result": {
                                "authenticated": True,
                                "api_profile": {"major": 1, "minor": 0},
                                "engine_instance_id": "00000000-0000-4000-8000-000000000001",
                                "instance_nonce": payload["instance_nonce"],
                            }
                        }
                    return {"result": {"items": []}}

                session.call.side_effect = on_call
                client_cls.return_value.session.return_value = session
                exit_code = cli_main.main(
                    [
                        "models",
                        "list",
                        "--rendezvous",
                        str(rendezvous),
                        "--credential",
                        str(cred_path),
                    ]
                )
            self.assertEqual(exit_code, 1)
        finally:
            rendezvous.unlink()
            cred_path.unlink()

    def test_models_list_handles_malformed_hello2_result(self) -> None:
        payload = json.loads((FIXTURES / "rendezvous_minimal.json").read_text(encoding="utf-8"))
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(payload, handle)
            rendezvous = Path(handle.name)
        with tempfile.NamedTemporaryFile("w", delete=False) as cred:
            cred.write("secret")
            cred_path = Path(cred.name)
        try:
            with mock.patch(
                "model_deck.cli.main.UnixSocketEngineClient",
            ) as client_cls:
                session = mock.MagicMock()
                session.__enter__.return_value = session
                session.__exit__.return_value = False

                call_count = 0

                def on_call(_frame):
                    nonlocal call_count
                    call_count += 1
                    if call_count == 1:
                        return {
                            "result": {
                                "authenticated": False,
                                "api_profile": {"major": 1, "minor": 0},
                                "engine_instance_id": payload["engine_instance_id"],
                                "instance_nonce": payload["instance_nonce"],
                            }
                        }
                    if call_count == 2:
                        return {"result": "not-a-map"}
                    return {"result": {"items": []}}

                session.call.side_effect = on_call
                client_cls.return_value.session.return_value = session
                exit_code = cli_main.main(
                    [
                        "models",
                        "list",
                        "--rendezvous",
                        str(rendezvous),
                        "--credential",
                        str(cred_path),
                    ]
                )
            self.assertEqual(exit_code, 1)
        finally:
            rendezvous.unlink()
            cred_path.unlink()

class EngineCliServeForwardingTests(unittest.TestCase):
    def test_engine_serve_forwards_fixture_flags(self) -> None:
        runtime = mock.MagicMock()
        runtime.server.serve_forever.side_effect = lambda: None
        with mock.patch(
            "model_deck.bootstrap.build_engine_server",
            return_value=runtime,
        ) as build_mock:
            with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as artifact, tempfile.TemporaryDirectory() as socket_root:
                exit_code = cli_main.main(
                    [
                        "engine",
                        "serve",
                        "--state-root",
                        state,
                        "--artifact-root",
                        artifact,
                        "--socket-root",
                        socket_root,
                        "--legacy-agents-dir",
                        str(FIXTURES / "legacy_agent"),
                        "--enable-application-state",
                        "--enable-fixture-runs",
                    ]
                )
        self.assertEqual(exit_code, 0)
        kwargs = build_mock.call_args.kwargs
        self.assertTrue(kwargs["enable_application_state"])
        self.assertTrue(kwargs["enable_fixture_runs"])


class EngineCliFixtureTextTests(unittest.TestCase):
    def test_runs_fixture_text_reports_authentication_failure(self) -> None:
        payload = json.loads((FIXTURES / "rendezvous_minimal.json").read_text(encoding="utf-8"))
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(payload, handle)
            rendezvous = Path(handle.name)
        with tempfile.NamedTemporaryFile("w", delete=False) as cred:
            cred.write("secret")
            cred_path = Path(cred.name)
        try:
            with mock.patch(
                "model_deck.cli.main.UnixSocketEngineClient",
            ) as client_cls:
                session = mock.MagicMock()
                session.__enter__.return_value = session
                session.__exit__.return_value = False

                call_count = 0

                def on_call(_frame):
                    nonlocal call_count
                    call_count += 1
                    if call_count == 1:
                        return {
                            "result": {
                                "authenticated": False,
                                "api_profile": {"major": 1, "minor": 0},
                                "engine_instance_id": payload["engine_instance_id"],
                                "instance_nonce": payload["instance_nonce"],
                            }
                        }
                    return {
                        "error": {
                            "message": "authentication failed",
                            "data": {"code": "capability_denied"},
                        }
                    }

                session.call.side_effect = on_call
                client_cls.return_value.session.return_value = session
                exit_code = cli_main.main(
                    [
                        "runs",
                        "fixture-text",
                        "--rendezvous",
                        str(rendezvous),
                        "--credential",
                        str(cred_path),
                    ]
                )
            self.assertEqual(exit_code, 1)
        finally:
            rendezvous.unlink()
            cred_path.unlink()

    def test_runs_fixture_text_rejects_malformed_event_notification(self) -> None:
        payload = json.loads((FIXTURES / "rendezvous_minimal.json").read_text(encoding="utf-8"))
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(payload, handle)
            rendezvous = Path(handle.name)
        with tempfile.NamedTemporaryFile("w", delete=False) as cred:
            cred.write("secret")
            cred_path = Path(cred.name)
        try:
            with mock.patch(
                "model_deck.cli.main.UnixSocketEngineClient",
            ) as client_cls:
                session = mock.MagicMock()
                session.__enter__.return_value = session
                session.__exit__.return_value = False

                call_count = 0

                def on_call(frame):
                    nonlocal call_count
                    call_count += 1
                    method = frame.get("method")
                    if method == "engine.v1.hello" and call_count == 1:
                        return {
                            "result": {
                                "authenticated": False,
                                "api_profile": {"major": 1, "minor": 0},
                                "engine_instance_id": payload["engine_instance_id"],
                                "instance_nonce": payload["instance_nonce"],
                            }
                        }
                    if method == "engine.v1.hello" and call_count == 2:
                        return {
                            "result": {
                                "authenticated": True,
                                "api_profile": {"major": 1, "minor": 0},
                                "engine_instance_id": payload["engine_instance_id"],
                                "instance_nonce": payload["instance_nonce"],
                            }
                        }
                    if method == "engine.v1.connections.save":
                        return {"result": {"connection": {"connection_id": "550e8400-e29b-41d4-a716-446655440002"}}}
                    if method == "engine.v1.models.register":
                        return {
                            "result": {
                                "model": {
                                    "registration_id": "550e8400-e29b-41d4-a716-446655440010",
                                }
                            }
                        }
                    if method == "engine.v1.sessions.create":
                        return {"result": {"session_id": "550e8400-e29b-41d4-a716-446655440011"}}
                    if method == "engine.v1.runs.start":
                        return {
                            "result": {
                                "run": {"run_id": "550e8400-e29b-41d4-a716-446655440012"}
                            }
                        }
                    if method == "engine.v1.events.subscribe":
                        return {
                            "result": {
                                "subscription_id": "550e8400-e29b-41d4-a716-446655440013"
                            }
                        }
                    return {"result": {}}

                session.call.side_effect = on_call
                session.read_notification.return_value = {
                    "jsonrpc": "2.0",
                    "method": "engine.v1.event",
                    "params": {"event": {"kind": "content.delta"}},
                }
                client_cls.return_value.session.return_value = session
                exit_code = cli_main.main(
                    [
                        "runs",
                        "fixture-text",
                        "--rendezvous",
                        str(rendezvous),
                        "--credential",
                        str(cred_path),
                    ]
                )
            self.assertEqual(exit_code, 1)
        finally:
            rendezvous.unlink()
            cred_path.unlink()
    def test_runs_fixture_text_rejects_mismatched_subscription_id(self) -> None:
        payload = json.loads((FIXTURES / "rendezvous_minimal.json").read_text(encoding="utf-8"))
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(payload, handle)
            rendezvous = Path(handle.name)
        with tempfile.NamedTemporaryFile("w", delete=False) as cred:
            cred.write("secret")
            cred_path = Path(cred.name)
        try:
            with mock.patch(
                "model_deck.cli.main.UnixSocketEngineClient",
            ) as client_cls:
                session = mock.MagicMock()
                session.__enter__.return_value = session
                session.__exit__.return_value = False

                call_count = 0
                run_id = "550e8400-e29b-41d4-a716-446655440012"

                def on_call(frame):
                    nonlocal call_count
                    call_count += 1
                    method = frame.get("method")
                    if method == "engine.v1.hello" and call_count == 1:
                        return {
                            "result": {
                                "authenticated": False,
                                "api_profile": {"major": 1, "minor": 0},
                                "engine_instance_id": payload["engine_instance_id"],
                                "instance_nonce": payload["instance_nonce"],
                            }
                        }
                    if method == "engine.v1.hello" and call_count == 2:
                        return {
                            "result": {
                                "authenticated": True,
                                "api_profile": {"major": 1, "minor": 0},
                                "engine_instance_id": payload["engine_instance_id"],
                                "instance_nonce": payload["instance_nonce"],
                            }
                        }
                    if method == "engine.v1.connections.save":
                        return {"result": {"connection": {"connection_id": "550e8400-e29b-41d4-a716-446655440002"}}}
                    if method == "engine.v1.models.register":
                        return {
                            "result": {
                                "model": {
                                    "registration_id": "550e8400-e29b-41d4-a716-446655440010",
                                }
                            }
                        }
                    if method == "engine.v1.sessions.create":
                        return {"result": {"session_id": "550e8400-e29b-41d4-a716-446655440011"}}
                    if method == "engine.v1.runs.start":
                        return {"result": {"run": {"run_id": run_id}}}
                    if method == "engine.v1.events.subscribe":
                        return {
                            "result": {
                                "subscription_id": "550e8400-e29b-41d4-a716-446655440013"
                            }
                        }
                    return {"result": {}}

                session.call.side_effect = on_call
                session.read_notification.return_value = {
                    "jsonrpc": "2.0",
                    "method": "engine.v1.event",
                    "params": {
                        "subscription_id": "550e8400-e29b-41d4-a716-4466554400ff",
                        "event": {
                            "kind": "content.delta",
                            "run_id": run_id,
                            "session_id": "550e8400-e29b-41d4-a716-446655440011",
                            "sequence": 1,
                            "event_schema_version": 1,
                            "observed_at": "2026-09-12T16:00:00Z",
                            "channel": "text",
                            "delta": "x",
                        },
                    },
                }
                client_cls.return_value.session.return_value = session
                exit_code = cli_main.main(
                    [
                        "runs",
                        "fixture-text",
                        "--rendezvous",
                        str(rendezvous),
                        "--credential",
                        str(cred_path),
                    ]
                )
            self.assertEqual(exit_code, 1)
        finally:
            rendezvous.unlink()
            cred_path.unlink()

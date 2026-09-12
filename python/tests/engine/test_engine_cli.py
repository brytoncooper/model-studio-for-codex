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

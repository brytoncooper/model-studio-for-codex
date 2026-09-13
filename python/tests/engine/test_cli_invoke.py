from __future__ import annotations

import io
import json
import math
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from model_deck.adapters.transport.framing import MAX_FRAME_BYTES
from model_deck.bootstrap import build_engine_server
from model_deck.cli import main as cli_main
from model_deck.engine.kernel_composition import KernelComposition
from model_deck.kernel import FeatureDescriptor, KernelApiVersion, OperationDescriptor, compose
from model_deck_contracts.paths import repo_root


OPERATION = "com.example.fixture.invoke"
GRANT = "fixture.invoke"
PERMISSIVE_SCHEMA = (
    "contracts/engine.v1/methods/health.params.schema.json#/properties"
)


def _invoke_fixture_kernel(handler, *, operation_id=OPERATION):
    feature = FeatureDescriptor(
        "com.example.fixture", "1.0.0", KernelApiVersion(1, 0),
        operations=(OperationDescriptor(
            operation_id, PERMISSIVE_SCHEMA, PERMISSIVE_SCHEMA, "read", (GRANT,),
        ),),
    )
    return compose((feature,), {}, {operation_id: handler})


class _SocketTestBase(unittest.TestCase):
    """Real-temp-socket harness mirroring ``KernelSocketCompositionTests``."""

    def directory(self) -> Path:
        directory = tempfile.TemporaryDirectory(prefix="cli-invoke-")
        self.addCleanup(directory.cleanup)
        return Path(directory.name).resolve()

    def build(self, composition):
        return build_engine_server(
            state_root=self.directory(),
            artifact_root=self.directory(),
            socket_root=self.directory(),
            legacy_agents_dir=self.directory(),
            default_connection_id="550e8400-e29b-41d4-a716-446655440099",
            source_root=repo_root(),
            kernel_composition=composition,
        )

    @contextmanager
    def runtime(self, composition):
        runtime = self.build(composition)
        runtime.server.start()
        try:
            yield runtime
        finally:
            runtime.server.stop()

    def _invoke_cli(
        self, *, operation_id, rendezvous, credential, input_path=None,
    ) -> tuple[int, str, str]:
        argv = [
            "invoke", operation_id,
            "--rendezvous", str(rendezvous),
            "--credential", str(credential),
        ]
        if input_path is not None:
            argv.extend(["--input-file", str(input_path)])
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(sys, "stdout", stdout), mock.patch.object(sys, "stderr", stderr):
            exit_code = cli_main.main(argv)
        return exit_code, stdout.getvalue(), stderr.getvalue()


class GenericCliInvokeTests(_SocketTestBase):
    def test_generic_contributed_operation_returns_result_and_preserves_falsy_values(self):
        observed: list[tuple[object, object]] = []

        def handler(params, grants):
            observed.append((params, grants))
            return {
                "status": "ok",
                "echo": params,
                "literal_zero": 0,
                "literal_false": False,
                "literal_empty_string": "",
                "literal_null": None,
                "literal_empty_list": [],
                "literal_empty_object": {},
            }

        composition = KernelComposition(
            _invoke_fixture_kernel(handler), operator_grants=(GRANT,),
        )
        with self.runtime(composition) as runtime:
            input_path = self.directory() / "input.json"
            input_path.write_text(json.dumps({
                "flag": False,
                "count": 0,
                "label": "",
                "value": None,
                "items": [],
                "meta": {},
            }))
            exit_code, stdout, stderr = self._invoke_cli(
                operation_id=OPERATION,
                rendezvous=runtime.rendezvous_path,
                credential=runtime.enrollment.credential_path,
                input_path=input_path,
            )
        self.assertEqual(exit_code, 0, msg=stderr)
        parsed = json.loads(stdout)
        self.assertEqual(parsed["status"], "ok")
        self.assertEqual(parsed["echo"], {
            "flag": False,
            "count": 0,
            "label": "",
            "value": None,
            "items": [],
            "meta": {},
        })
        self.assertEqual(parsed["literal_zero"], 0)
        self.assertEqual(parsed["literal_false"], False)
        self.assertEqual(parsed["literal_empty_string"], "")
        self.assertEqual(parsed["literal_null"], None)
        self.assertEqual(parsed["literal_empty_list"], [])
        self.assertEqual(parsed["literal_empty_object"], {})
        self.assertEqual(len(observed), 1)
        params, grants = observed[0]
        self.assertEqual(params, {
            "flag": False,
            "count": 0,
            "label": "",
            "value": None,
            "items": [],
            "meta": {},
        })
        self.assertEqual(grants, frozenset({GRANT}))

    def test_authentication_failure_blocks_invocation_and_handler_is_never_called(self):
        observed: list[object] = []
        handler = lambda params, grants: observed.append(params)
        composition = KernelComposition(
            _invoke_fixture_kernel(handler), operator_grants=(GRANT,),
        )
        with self.runtime(composition) as runtime:
            bogus_cred = self.directory() / "bogus_credential"
            bogus_cred.write_text("not-the-engine-credential")
            exit_code, stdout, stderr = self._invoke_cli(
                operation_id=OPERATION,
                rendezvous=runtime.rendezvous_path,
                credential=bogus_cred,
            )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(observed, [])

    def test_unknown_operation_id_fails_discovery_with_zero_invocations(self):
        observed: list[object] = []
        handler = lambda params, grants: observed.append(params)
        composition = KernelComposition(
            _invoke_fixture_kernel(handler), operator_grants=(GRANT,),
        )
        with self.runtime(composition) as runtime:
            exit_code, stdout, stderr = self._invoke_cli(
                operation_id="com.example.nonexistent.operation",
                rendezvous=runtime.rendezvous_path,
                credential=runtime.enrollment.credential_path,
            )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("not advertised", stderr)
        self.assertEqual(observed, [])

    def test_duplicate_keys_in_input_file_reject_before_connecting(self):
        observed: list[object] = []
        handler = lambda params, grants: observed.append(params)
        composition = KernelComposition(
            _invoke_fixture_kernel(handler), operator_grants=(GRANT,),
        )
        with self.runtime(composition) as runtime:
            rendezvous_path = runtime.rendezvous_path
            credential_path = runtime.enrollment.credential_path
            input_path = self.directory() / "input.json"
            input_path.write_text('{"flag": true, "flag": false}')
            with mock.patch("model_deck.cli.main.UnixSocketEngineClient") as client_cls:
                exit_code, stdout, stderr = self._invoke_cli(
                    operation_id=OPERATION,
                    rendezvous=rendezvous_path,
                    credential=credential_path,
                    input_path=input_path,
                )
            client_cls.assert_not_called()
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("duplicate key", stderr)
        self.assertEqual(observed, [])

    def test_trailing_data_in_input_file_rejects_before_connecting(self):
        observed: list[object] = []
        handler = lambda params, grants: observed.append(params)
        composition = KernelComposition(
            _invoke_fixture_kernel(handler), operator_grants=(GRANT,),
        )
        with self.runtime(composition) as runtime:
            input_path = self.directory() / "input.json"
            input_path.write_text('{"flag": true} trailing')
            with mock.patch("model_deck.cli.main.UnixSocketEngineClient") as client_cls:
                exit_code, stdout, stderr = self._invoke_cli(
                    operation_id=OPERATION,
                    rendezvous=runtime.rendezvous_path,
                    credential=runtime.enrollment.credential_path,
                    input_path=input_path,
                )
            client_cls.assert_not_called()
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("trailing", stderr)
        self.assertEqual(observed, [])

    def test_oversize_input_file_rejects_before_connecting(self):
        observed: list[object] = []
        handler = lambda params, grants: observed.append(params)
        composition = KernelComposition(
            _invoke_fixture_kernel(handler), operator_grants=(GRANT,),
        )
        with self.runtime(composition) as runtime:
            input_path = self.directory() / "input.json"
            # 2 MiB of whitespace inside an object -> > MAX_FRAME_BYTES (1 MiB)
            input_path.write_bytes(b'{"x": "' + (b' ' * (2 * 1024 * 1024)) + b'"}')
            with mock.patch("model_deck.cli.main.UnixSocketEngineClient") as client_cls:
                exit_code, stdout, stderr = self._invoke_cli(
                    operation_id=OPERATION,
                    rendezvous=runtime.rendezvous_path,
                    credential=runtime.enrollment.credential_path,
                    input_path=input_path,
                )
            client_cls.assert_not_called()
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("frame budget", stderr)
        self.assertEqual(observed, [])

    def test_non_finite_number_in_input_file_rejects_before_connecting(self):
        observed: list[object] = []
        handler = lambda params, grants: observed.append(params)
        composition = KernelComposition(
            _invoke_fixture_kernel(handler), operator_grants=(GRANT,),
        )
        with self.runtime(composition) as runtime:
            input_path = self.directory() / "input.json"
            input_path.write_text(json.dumps({"value": math.inf}))
            with mock.patch("model_deck.cli.main.UnixSocketEngineClient") as client_cls:
                exit_code, stdout, stderr = self._invoke_cli(
                    operation_id=OPERATION,
                    rendezvous=runtime.rendezvous_path,
                    credential=runtime.enrollment.credential_path,
                    input_path=input_path,
                )
            client_cls.assert_not_called()
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("non-finite", stderr)
        self.assertEqual(observed, [])

    def test_non_object_input_file_rejects_before_connecting(self):
        observed: list[object] = []
        handler = lambda params, grants: observed.append(params)
        composition = KernelComposition(
            _invoke_fixture_kernel(handler), operator_grants=(GRANT,),
        )
        with self.runtime(composition) as runtime:
            input_path = self.directory() / "input.json"
            input_path.write_text("[1, 2, 3]")
            with mock.patch("model_deck.cli.main.UnixSocketEngineClient") as client_cls:
                exit_code, stdout, stderr = self._invoke_cli(
                    operation_id=OPERATION,
                    rendezvous=runtime.rendezvous_path,
                    credential=runtime.enrollment.credential_path,
                    input_path=input_path,
                )
            client_cls.assert_not_called()
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("JSON object", stderr)
        self.assertEqual(observed, [])

    def test_denied_grant_returns_capability_denied_and_handler_never_called(self):
        observed: list[object] = []
        handler = lambda params, grants: observed.append(params)
        composition = KernelComposition(
            _invoke_fixture_kernel(handler), operator_grants=(),
        )
        with self.runtime(composition) as runtime:
            exit_code, stdout, stderr = self._invoke_cli(
                operation_id=OPERATION,
                rendezvous=runtime.rendezvous_path,
                credential=runtime.enrollment.credential_path,
            )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("operation grant denied", stderr.lower())
        self.assertEqual(observed, [])

class InvokeFramePreflightTests(_SocketTestBase):
    """Verify the preflight catches full-frame overflow before any connection."""

    def test_envelope_overflow_preflight_rejects_before_connecting(self):
        observed: list[object] = []
        handler = lambda params, grants: observed.append(params)
        composition = KernelComposition(
            _invoke_fixture_kernel(handler), operator_grants=(GRANT,),
        )
        with self.runtime(composition) as runtime:
            # Input file is under MAX_FRAME_BYTES raw, but the wrapped frame
            # (input + envelope) exceeds MAX_FRAME_BYTES. The preflight
            # encode_frame() catches this before any socket connection.
            payload_size = MAX_FRAME_BYTES - 50
            input_text = '{"a":"' + (" " * payload_size) + '"}'
            assert len(input_text) == payload_size + 8
            input_path = self.directory() / "input.json"
            input_path.write_text(input_text)
            with mock.patch("model_deck.cli.main.UnixSocketEngineClient") as client_cls:
                exit_code, stdout, stderr = self._invoke_cli(
                    operation_id=OPERATION,
                    rendezvous=runtime.rendezvous_path,
                    credential=runtime.enrollment.credential_path,
                    input_path=input_path,
                )
                client_cls.assert_not_called()
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("frame", stderr.lower())
        self.assertEqual(observed, [])

    def test_input_file_max_plus_one_bytes_rejects_before_connecting(self):
        observed: list[object] = []
        handler = lambda params, grants: observed.append(params)
        composition = KernelComposition(
            _invoke_fixture_kernel(handler), operator_grants=(GRANT,),
        )
        with self.runtime(composition) as runtime:
            # len('{"a":"' + spaces + '"} ') == MAX_FRAME_BYTES + 1
            payload_size = MAX_FRAME_BYTES - 8
            input_text = '{"a":"' + (" " * payload_size) + '"} '
            assert len(input_text) == MAX_FRAME_BYTES + 1
            input_path = self.directory() / "input.json"
            input_path.write_text(input_text)
            with mock.patch("model_deck.cli.main.UnixSocketEngineClient") as client_cls:
                exit_code, stdout, stderr = self._invoke_cli(
                    operation_id=OPERATION,
                    rendezvous=runtime.rendezvous_path,
                    credential=runtime.enrollment.credential_path,
                    input_path=input_path,
                )
                client_cls.assert_not_called()
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("frame budget", stderr)
        self.assertEqual(observed, [])

    def test_input_file_just_under_max_bytes_passes(self):
        observed: list[object] = []
        handler = lambda params, grants: observed.append(params)
        composition = KernelComposition(
            _invoke_fixture_kernel(handler), operator_grants=(GRANT,),
        )
        with self.runtime(composition) as runtime:
            # Just under MAX so input-file check + envelope-overhead preflight
            # both pass.
            payload_size = MAX_FRAME_BYTES - 200
            input_text = '{"a":"' + (" " * payload_size) + '"}'
            input_path = self.directory() / "input.json"
            input_path.write_text(input_text)
            exit_code, stdout, stderr = self._invoke_cli(
                operation_id=OPERATION,
                rendezvous=runtime.rendezvous_path,
                credential=runtime.enrollment.credential_path,
                input_path=input_path,
            )
        self.assertEqual(exit_code, 0, msg=stderr)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0], {"a": " " * payload_size})


class InputFileBoundaryTests(_SocketTestBase):
    """Walk the strict bounded-JSON contract on the input file."""

    def _build_runtime_with_handler(self, observed):
        handler = lambda params, grants: observed.append(params)
        composition = KernelComposition(
            _invoke_fixture_kernel(handler), operator_grants=(GRANT,),
        )
        return self.build(composition)

    def test_input_file_at_max_depth_passes(self):
        observed: list[object] = []
        runtime = self._build_runtime_with_handler(observed)
        runtime.server.start()
        try:
            depth = 64  # exactly the walker limit
            input_text = '{"a":' * depth + "null" + "}" * depth
            input_path = self.directory() / "input.json"
            input_path.write_text(input_text)
            exit_code, stdout, stderr = self._invoke_cli(
                operation_id=OPERATION,
                rendezvous=runtime.rendezvous_path,
                credential=runtime.enrollment.credential_path,
                input_path=input_path,
            )
        finally:
            runtime.server.stop()
        self.assertEqual(exit_code, 0, msg=stderr)

    def test_input_file_above_max_depth_rejects_before_connecting(self):
        observed: list[object] = []
        runtime = self._build_runtime_with_handler(observed)
        runtime.server.start()
        try:
            depth = 65  # one over the walker limit
            input_text = '{"a":' * depth + "null" + "}" * depth
            input_path = self.directory() / "input.json"
            input_path.write_text(input_text)
            with mock.patch("model_deck.cli.main.UnixSocketEngineClient") as client_cls:
                exit_code, stdout, stderr = self._invoke_cli(
                    operation_id=OPERATION,
                    rendezvous=runtime.rendezvous_path,
                    credential=runtime.enrollment.credential_path,
                    input_path=input_path,
                )
                client_cls.assert_not_called()
        finally:
            runtime.server.stop()
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("depth limit", stderr)
        self.assertEqual(observed, [])

    def test_input_file_with_lone_surrogate_in_value_rejects_before_connecting(self):
        observed: list[object] = []
        runtime = self._build_runtime_with_handler(observed)
        runtime.server.start()
        try:
            input_path = self.directory() / "input.json"
            # ``write_bytes`` is required because Python's UTF-8 encoder
            # rejects lone surrogates; the on-disk JSON literal "\uD800"
            # parses into a lone-surrogate string.
            input_path.write_bytes(b'{"label": "\\uD800"}')
            with mock.patch("model_deck.cli.main.UnixSocketEngineClient") as client_cls:
                exit_code, stdout, stderr = self._invoke_cli(
                    operation_id=OPERATION,
                    rendezvous=runtime.rendezvous_path,
                    credential=runtime.enrollment.credential_path,
                    input_path=input_path,
                )
                client_cls.assert_not_called()
        finally:
            runtime.server.stop()
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("surrogate", stderr.lower())
        self.assertEqual(observed, [])

    def test_input_file_with_lone_surrogate_in_key_rejects_before_connecting(self):
        observed: list[object] = []
        runtime = self._build_runtime_with_handler(observed)
        runtime.server.start()
        try:
            input_path = self.directory() / "input.json"
            input_path.write_bytes(b'{"\\uD800": "value"}')
            with mock.patch("model_deck.cli.main.UnixSocketEngineClient") as client_cls:
                exit_code, stdout, stderr = self._invoke_cli(
                    operation_id=OPERATION,
                    rendezvous=runtime.rendezvous_path,
                    credential=runtime.enrollment.credential_path,
                    input_path=input_path,
                )
                client_cls.assert_not_called()
        finally:
            runtime.server.stop()
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("surrogate", stderr.lower())
        self.assertEqual(observed, [])


def _write_mock_rendezvous_and_credential(directory):
    rendezvous = directory / "rendezvous.json"
    rendezvous.write_text(json.dumps({
        "transport": "unix",
        "socket_path": "/tmp/whatever.sock",
        "engine_instance_id": "550e8400-e29b-41d4-a716-446655440099",
        "instance_nonce": "nonce",
        "api_profile": {"major": 1, "minor": 0},
    }))
    credential = directory / "cred"
    credential.write_text("secret")
    return rendezvous, credential


def _mock_invoke_client(replies):
    """Return a stand-in for ``UnixSocketEngineClient`` whose instance serves queued replies.

    ``mock.patch("module.UnixSocketEngineClient", standin)`` makes
    ``UnixSocketEngineClient(path)`` call ``standin(path)``, which returns
    ``standin.return_value`` (a fresh MagicMock unless chained). Chaining
    ``standin.return_value.session.return_value = session`` makes every
    constructed instance reuse this prepared session.
    """
    session = mock.MagicMock()
    session.__enter__.return_value = session
    session.__exit__.return_value = False
    queue = list(replies)

    def on_call(_frame):
        if not queue:
            raise AssertionError("session.call invoked more times than expected")
        return queue.pop(0)

    session.call.side_effect = on_call
    standin = mock.MagicMock()
    standin.return_value.session.return_value = session
    return standin


def _valid_auth_then_listing_replies():
    return [
        {"result": {"authenticated": False,
                    "api_profile": {"major": 1, "minor": 0},
                    "engine_instance_id": "550e8400-e29b-41d4-a716-446655440099",
                    "instance_nonce": "nonce"}},
        {"result": {"authenticated": True,
                    "api_profile": {"major": 1, "minor": 0},
                    "engine_instance_id": "550e8400-e29b-41d4-a716-446655440099",
                    "instance_nonce": "nonce"}},
        {"result": {"operations": [{
            "operation_id": OPERATION,
            "input_schema_id": PERMISSIVE_SCHEMA,
            "output_schema_id": PERMISSIVE_SCHEMA,
            "effect": "read",
            "required_grants": [GRANT],
        }]}},
    ]


class InvokeResponseEnvelopeTests(_SocketTestBase):
    """The invoke reply must contain exactly one of ``error`` / ``result``."""

    def test_invoke_response_with_neither_error_nor_result_rejects(self):
        rendezvous, credential = _write_mock_rendezvous_and_credential(self.directory())
        replies = _valid_auth_then_listing_replies() + [{}]
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient",
                        _mock_invoke_client(replies)):
            exit_code, stdout, stderr = self._invoke_cli(
                operation_id=OPERATION, rendezvous=rendezvous, credential=credential,
            )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("invalid engine response", stderr.lower())

    def test_invoke_response_with_both_error_and_result_rejects(self):
        rendezvous, credential = _write_mock_rendezvous_and_credential(self.directory())
        replies = _valid_auth_then_listing_replies() + [
            {"error": {"code": -1, "message": "x"}, "result": {"status": "ok"}},
        ]
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient",
                        _mock_invoke_client(replies)):
            exit_code, stdout, stderr = self._invoke_cli(
                operation_id=OPERATION, rendezvous=rendezvous, credential=credential,
            )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("invalid engine response", stderr.lower())

    def test_invoke_response_that_is_not_an_object_rejects(self):
        rendezvous, credential = _write_mock_rendezvous_and_credential(self.directory())
        replies = _valid_auth_then_listing_replies() + ["not-a-dict"]
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient",
                        _mock_invoke_client(replies)):
            exit_code, stdout, stderr = self._invoke_cli(
                operation_id=OPERATION, rendezvous=rendezvous, credential=credential,
            )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("invalid engine response", stderr.lower())

    def test_invoke_result_with_non_finite_number_rejects(self):
        rendezvous, credential = _write_mock_rendezvous_and_credential(self.directory())
        # The engine itself rejects this; the CLI must catch it as
        # defense-in-depth if a future caller bypasses the kernel contract.
        replies = _valid_auth_then_listing_replies() + [
            {"result": {"value": math.inf}},
        ]
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient",
                        _mock_invoke_client(replies)):
            exit_code, stdout, stderr = self._invoke_cli(
                operation_id=OPERATION, rendezvous=rendezvous, credential=credential,
            )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("non-finite", stderr.lower())

    def test_invoke_result_with_lone_surrogate_rejects(self):
        rendezvous, credential = _write_mock_rendezvous_and_credential(self.directory())
        replies = _valid_auth_then_listing_replies() + [
            {"result": {"label": "\uD800"}},
        ]
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient",
                        _mock_invoke_client(replies)):
            exit_code, stdout, stderr = self._invoke_cli(
                operation_id=OPERATION, rendezvous=rendezvous, credential=credential,
            )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("surrogate", stderr.lower())



if __name__ == "__main__":
    unittest.main()

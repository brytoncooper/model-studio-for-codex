from __future__ import annotations

import io
import json
import math
import re
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from model_deck.adapters.transport.framing import MAX_FRAME_BYTES
from model_deck.adapters.transport.rendezvous import RendezvousDescriptor
from model_deck.bootstrap import build_engine_server
from model_deck.cli import main as cli_main
from model_deck.engine.kernel_composition import KernelComposition
from model_deck.kernel import FeatureDescriptor, KernelApiVersion, OperationDescriptor, compose
from model_deck_contracts.paths import repo_root


OPERATION = "com.example.fixture.invoke"
WRAPPER_METHOD = "engine.v1.operations.invoke"
GRANT = "fixture.invoke"
PERMISSIVE_SCHEMA = (
    "contracts/engine.v1/methods/health.params.schema.json#/properties"
)
UUID4_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
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
        idempotency_key=None,
    ) -> tuple[int, str, str]:
        argv = [
            "invoke", operation_id,
            "--rendezvous", str(rendezvous),
            "--credential", str(credential),
        ]
        if input_path is not None:
            argv.extend(["--input-file", str(input_path)])
        if idempotency_key is not None:
            argv.extend(["--idempotency-key", idempotency_key])
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(sys, "stdout", stdout), mock.patch.object(sys, "stderr", stderr):
            exit_code = cli_main.main(argv)
        return exit_code, stdout.getvalue(), stderr.getvalue()


# ---------------------------------------------------------------------------
# Real-socket tests: exercise the CLI preflight + framing without depending
# on the engine's kernel dispatch (which does not route the wrapper method
# without an external extension host — out of scope here).
# ---------------------------------------------------------------------------


class GenericCliInvokePreflightTests(_SocketTestBase):
    """Verify preflight failures reject before any socket is opened."""

    def test_duplicate_keys_in_input_file_reject_before_connecting(self):
        handler = lambda params, grants: None
        composition = KernelComposition(
            _invoke_fixture_kernel(handler), operator_grants=(GRANT,),
        )
        with self.runtime(composition) as runtime:
            input_path = self.directory() / "input.json"
            input_path.write_text('{"flag": true, "flag": false}')
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
        self.assertIn("duplicate key", stderr)

    def test_trailing_data_in_input_file_rejects_before_connecting(self):
        handler = lambda params, grants: None
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

    def test_oversize_input_file_rejects_before_connecting(self):
        handler = lambda params, grants: None
        composition = KernelComposition(
            _invoke_fixture_kernel(handler), operator_grants=(GRANT,),
        )
        with self.runtime(composition) as runtime:
            input_path = self.directory() / "input.json"
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

    def test_non_finite_number_in_input_file_rejects_before_connecting(self):
        handler = lambda params, grants: None
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

    def test_non_object_input_file_rejects_before_connecting(self):
        handler = lambda params, grants: None
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

    def test_lone_surrogate_in_input_file_rejects_before_connecting(self):
        handler = lambda params, grants: None
        composition = KernelComposition(
            _invoke_fixture_kernel(handler), operator_grants=(GRANT,),
        )
        with self.runtime(composition) as runtime:
            input_path = self.directory() / "input.json"
            input_path.write_bytes(b'{"label": "\\uD800"}')
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
        self.assertIn("surrogate", stderr.lower())

    def test_authentication_failure_blocks_invocation(self):
        """Auth failure returns 1 before any operations.list / wrapper call.

        The dispatch has not yet been entered, so the wire method on the
        engine is irrelevant — the only contract here is exit code + stderr.
        """
        handler = lambda params, grants: None
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
        self.assertIn("invalid enrollment credential", stderr)

    def test_unknown_operation_id_fails_discovery_with_zero_invocation_calls(self):
        """Discovery rejects before any wrapper frame is sent.

        ``engine.v1.operations.list`` runs against a server whose catalog
        does not contain the requested id. The CLI exits 1 with the same
        content-free error string regardless of whether the wrapper path
        could have routed the call.
        """
        composition = KernelComposition(
            _invoke_fixture_kernel(lambda params, grants: None),
            operator_grants=(GRANT,),
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


class InvokeFramePreflightTests(_SocketTestBase):
    """Wrapper-frame overflow is caught before any socket connection."""

    def test_envelope_overflow_preflight_rejects_before_connecting(self):
        handler = lambda params, grants: None
        composition = KernelComposition(
            _invoke_fixture_kernel(handler), operator_grants=(GRANT,),
        )
        with self.runtime(composition) as runtime:
            # Input file is under MAX_FRAME_BYTES raw, but the wrapped frame
            # (input + envelope + wrapper keys) exceeds MAX_FRAME_BYTES.
            payload_size = MAX_FRAME_BYTES - 50
            input_text = '{"a":"' + (" " * payload_size) + '"}'
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

    def test_input_file_max_plus_one_bytes_rejects_before_connecting(self):
        handler = lambda params, grants: None
        composition = KernelComposition(
            _invoke_fixture_kernel(handler), operator_grants=(GRANT,),
        )
        with self.runtime(composition) as runtime:
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


# ---------------------------------------------------------------------------
# Mock-client tests: drive the CLI against a scripted Unix socket stand-in
# to assert the wire shape, idempotency defaults, and result unwrapping.
# ---------------------------------------------------------------------------


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


def _hello1_reply():
    return {"result": {
        "authenticated": False,
        "api_profile": {"major": 1, "minor": 0},
        "engine_instance_id": "550e8400-e29b-41d4-a716-446655440099",
        "instance_nonce": "nonce",
    }}


def _hello2_reply():
    return {"result": {
        "authenticated": True,
        "api_profile": {"major": 1, "minor": 0},
        "engine_instance_id": "550e8400-e29b-41d4-a716-446655440099",
        "instance_nonce": "nonce",
    }}


def _operations_listing_reply():
    return {"result": {"operations": [{
        "operation_id": OPERATION,
        "input_schema_id": PERMISSIVE_SCHEMA,
        "output_schema_id": PERMISSIVE_SCHEMA,
        "effect": "read",
        "required_grants": [GRANT],
    }]}}


class _RecordingSession:
    """Stand-in for ``UnixSocketEngineClient.session``.

    Replays a queue of JSON-RPC responses for each ``call(frame)``, records
    every frame the CLI sent, and lets the test assert on those frames.
    ``reply_factory`` is called when the queue is exhausted so a single
    stand-in can serve multiple CLI invocations within the same test.
    """

    def __init__(self, replies, *, reply_factory=None):
        self._replies = list(replies)
        self._reply_factory = reply_factory
        self.frames: list[dict[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def call(self, frame):
        self.frames.append(frame)
        if self._replies:
            return self._replies.pop(0)
        if self._reply_factory is None:
            raise AssertionError(
                f"session.call invoked more times than expected ({len(self.frames)} frames sent)",
            )
        next_reply = self._reply_factory()
        if next_reply is _STOP:
            raise AssertionError(
                f"session.call invoked more times than expected ({len(self.frames)} frames sent)",
            )
        return next_reply


_STOP = object()


def _mock_client_with_replies(replies, *, reply_factory=None):
    """Return a stand-in for ``UnixSocketEngineClient`` that returns a fresh session."""
    standin = mock.MagicMock()
    session = _RecordingSession(replies, reply_factory=reply_factory)

    def factory(_path):
        instance = mock.MagicMock()
        instance.session.return_value = session
        return instance

    standin.side_effect = factory
    return standin, session


class InvokeWrapperWireShapeTests(unittest.TestCase):
    """The first new tests assert the wire method is the wrapper, not direct dispatch."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="cli-invoke-wire-")
        self.addCleanup(self.directory.cleanup)
        self.workdir = Path(self.directory.name).resolve()
        self.rendezvous, self.credential = _write_mock_rendezvous_and_credential(self.workdir)

    def _run(self, argv_extra):
        argv = [
            "invoke", OPERATION,
            "--rendezvous", str(self.rendezvous),
            "--credential", str(self.credential),
        ]
        argv.extend(argv_extra)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(sys, "stdout", stdout), \
                mock.patch.object(sys, "stderr", stderr):
            code = cli_main.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_wire_method_is_wrapper_and_operation_is_embedded_in_params(self):
        """Distinguishes wrapper contract from the old direct-dispatch wire method."""
        wrapper_result = {"result": {"output": {"status": "ok", "echo": {"flag": False}}}}
        replies = [
            _hello1_reply(),
            _hello2_reply(),
            _operations_listing_reply(),
            wrapper_result,
        ]
        standin, session = _mock_client_with_replies(replies)
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, stdout, stderr = self._run([])
        self.assertEqual(exit_code, 0, msg=stderr)
        self.assertEqual(len(session.frames), 4, msg=f"frames={session.frames!r}")
        # Frame 0: hello-1
        self.assertEqual(session.frames[0]["method"], "engine.v1.hello")
        # Frame 1: hello-2 (authenticated)
        self.assertEqual(session.frames[1]["method"], "engine.v1.hello")
        # Frame 2: operations.list (discovery)
        self.assertEqual(session.frames[2]["method"], "engine.v1.operations.list")
        # Frame 3: the wrapper, NOT the operation_id directly.
        wrapper_frame = session.frames[3]
        self.assertEqual(wrapper_frame["method"], WRAPPER_METHOD)
        self.assertNotEqual(wrapper_frame["method"], OPERATION)
        wrapper_params = wrapper_frame["params"]
        self.assertIsInstance(wrapper_params, dict)
        self.assertEqual(wrapper_params["operation"], OPERATION)
        self.assertEqual(wrapper_params["input"], {})
        self.assertRegex(str(wrapper_params["idempotency_key"]), UUID4_PATTERN)
        # Stdout prints the wrapper's output, NOT the wrapper envelope.
        parsed = json.loads(stdout)
        self.assertEqual(parsed, {"status": "ok", "echo": {"flag": False}})
        self.assertNotIn("output", parsed)

    def test_explicit_idempotency_key_is_passed_through(self):
        wrapper_result = {"result": {"output": {"ok": True}}}
        replies = [
            _hello1_reply(),
            _hello2_reply(),
            _operations_listing_reply(),
            wrapper_result,
        ]
        standin, session = _mock_client_with_replies(replies)
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, _, stderr = self._run(["--idempotency-key", "deterministic-key-12345"])
        self.assertEqual(exit_code, 0, msg=stderr)
        wrapper_params = session.frames[3]["params"]
        self.assertEqual(wrapper_params["idempotency_key"], "deterministic-key-12345")

    def test_default_idempotency_key_is_a_fresh_uuid4(self):
        wrapper_result = {"result": {"output": {"ok": True}}}
        single_invocation = [
            _hello1_reply(),
            _hello2_reply(),
            _operations_listing_reply(),
            wrapper_result,
        ]
        # ``reply_factory`` yields one reply per call; the queue is
        # exhausted after the first CLI invocation, so the factory must
        # hand out a single dict (not the whole list) for each subsequent
        # ``session.call`` invocation.
        factory_queue = list(single_invocation)

        def factory():
            return factory_queue.pop(0)

        standin, session = _mock_client_with_replies(
            list(single_invocation), reply_factory=factory,
        )
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, _, stderr = self._run([])
            self.assertEqual(exit_code, 0, msg=stderr)
        key_a = session.frames[3]["params"]["idempotency_key"]
        self.assertRegex(str(key_a), UUID4_PATTERN)
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, _, stderr = self._run([])
            self.assertEqual(exit_code, 0, msg=stderr)
        key_b = session.frames[7]["params"]["idempotency_key"]
        self.assertRegex(str(key_b), UUID4_PATTERN)
        self.assertNotEqual(key_a, key_b)

    def test_input_params_round_trip_into_wrapper_input_field(self):
        input_path = self.workdir / "input.json"
        input_path.write_text(json.dumps({"flag": False, "label": "fixture"}))
        wrapper_result = {"result": {"output": {"echo": {"flag": False, "label": "fixture"}}}}
        replies = [
            _hello1_reply(),
            _hello2_reply(),
            _operations_listing_reply(),
            wrapper_result,
        ]
        standin, session = _mock_client_with_replies(replies)
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, stdout, stderr = self._run(["--input-file", str(input_path)])
        self.assertEqual(exit_code, 0, msg=stderr)
        wrapper_params = session.frames[3]["params"]
        self.assertEqual(wrapper_params["input"], {"flag": False, "label": "fixture"})
        self.assertEqual(
            json.loads(stdout), {"echo": {"flag": False, "label": "fixture"}},
        )

    def test_preserves_falsy_output_values_and_prints_output_field(self):
        wrapper_result = {"result": {"output": {
            "status": "ok",
            "literal_zero": 0,
            "literal_false": False,
            "literal_empty_string": "",
            "literal_null": None,
            "literal_empty_list": [],
            "literal_empty_object": {},
        }}}
        replies = [
            _hello1_reply(),
            _hello2_reply(),
            _operations_listing_reply(),
            wrapper_result,
        ]
        standin, _ = _mock_client_with_replies(replies)
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, stdout, stderr = self._run([])
        self.assertEqual(exit_code, 0, msg=stderr)
        parsed = json.loads(stdout)
        self.assertEqual(parsed["literal_zero"], 0)
        self.assertEqual(parsed["literal_false"], False)
        self.assertEqual(parsed["literal_empty_string"], "")
        self.assertEqual(parsed["literal_null"], None)
        self.assertEqual(parsed["literal_empty_list"], [])
        self.assertEqual(parsed["literal_empty_object"], {})


class InvokeWrapperResultValidationTests(unittest.TestCase):
    """The CLI must reject wrapper responses that violate envelope/contract rules."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="cli-invoke-result-")
        self.addCleanup(self.directory.cleanup)
        self.workdir = Path(self.directory.name).resolve()
        self.rendezvous, self.credential = _write_mock_rendezvous_and_credential(self.workdir)

    def _run(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(sys, "stdout", stdout), \
                mock.patch.object(sys, "stderr", stderr):
            code = cli_main.main([
                "invoke", OPERATION,
                "--rendezvous", str(self.rendezvous),
                "--credential", str(self.credential),
            ])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_response_with_neither_error_nor_result_rejects(self):
        replies = [_hello1_reply(), _hello2_reply(), _operations_listing_reply(), {}]
        standin, _ = _mock_client_with_replies(replies)
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, stdout, stderr = self._run()
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("invalid engine response", stderr.lower())

    def test_response_with_both_error_and_result_rejects(self):
        replies = [
            _hello1_reply(),
            _hello2_reply(),
            _operations_listing_reply(),
            {"error": {"code": -1, "message": "x"}, "result": {"status": "ok"}},
        ]
        standin, _ = _mock_client_with_replies(replies)
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, stdout, stderr = self._run()
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("invalid engine response", stderr.lower())

    def test_response_that_is_not_an_object_rejects(self):
        replies = [
            _hello1_reply(),
            _hello2_reply(),
            _operations_listing_reply(),
            "not-a-dict",
        ]
        standin, _ = _mock_client_with_replies(replies)
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, stdout, stderr = self._run()
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("invalid engine response", stderr.lower())

    def test_wrapper_result_missing_required_output_field_rejects(self):
        """The wrapper contract requires ``output``; missing it is a CLI failure."""
        replies = [
            _hello1_reply(),
            _hello2_reply(),
            _operations_listing_reply(),
            {"result": {"job_id": "00000000-0000-4000-8000-000000000001"}},
        ]
        standin, _ = _mock_client_with_replies(replies)
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, stdout, stderr = self._run()
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("output", stderr.lower())

    def test_wrapper_result_with_unknown_field_rejects(self):
        """Bundled schema is strict; an extra field is rejected by the CLI."""
        replies = [
            _hello1_reply(),
            _hello2_reply(),
            _operations_listing_reply(),
            {"result": {"output": {"ok": True}, "unexpected": "field"}},
        ]
        standin, _ = _mock_client_with_replies(replies)
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, stdout, stderr = self._run()
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("contract validation", stderr.lower())

    def test_engine_error_envelope_is_reported(self):
        replies = [
            _hello1_reply(),
            _hello2_reply(),
            _operations_listing_reply(),
            {"error": {"code": -32005, "message": "method not configured"}},
        ]
        standin, _ = _mock_client_with_replies(replies)
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, stdout, stderr = self._run()
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("method not configured", stderr)

    def test_output_with_non_finite_number_rejects(self):
        replies = [
            _hello1_reply(),
            _hello2_reply(),
            _operations_listing_reply(),
            {"result": {"output": {"value": math.inf}}},
        ]
        standin, _ = _mock_client_with_replies(replies)
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, stdout, stderr = self._run()
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("non-finite", stderr.lower())

    def test_output_with_lone_surrogate_rejects(self):
        replies = [
            _hello1_reply(),
            _hello2_reply(),
            _operations_listing_reply(),
            {"result": {"output": {"label": "\uD800"}}},
        ]
        standin, _ = _mock_client_with_replies(replies)
        with mock.patch("model_deck.cli.main.UnixSocketEngineClient", standin):
            exit_code, stdout, stderr = self._run()
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("surrogate", stderr.lower())


if __name__ == "__main__":
    unittest.main()

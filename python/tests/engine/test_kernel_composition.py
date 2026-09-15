from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest

from model_deck.adapters.transport.rendezvous import load_rendezvous_file
from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
from model_deck.bootstrap import build_engine_server
from model_deck.engine.builtins import FEATURE_CORE, builtin_descriptor
from model_deck.engine.kernel_composition import KernelComposition, KernelInvocationError
from model_deck.kernel import CompositionError, FeatureDescriptor, KernelApiVersion, OperationDescriptor, compose
from model_deck_contracts.paths import repo_root


OPERATION = "com.example.fixture.inspect"
INPUT_SCHEMA = "contracts/engine.v1/methods/health.params.schema.json"
OUTPUT_SCHEMA = "contracts/engine.v1/methods/health.result.schema.json"
GRANT = "fixture.read"
PERMISSIVE_SCHEMA = INPUT_SCHEMA + "#/properties"


def fixture_kernel(handler, *, operation_id=OPERATION, input_schema=INPUT_SCHEMA,
                   output_schema=OUTPUT_SCHEMA, required_capabilities=(), optional_capabilities=()):
    feature = FeatureDescriptor(
        "com.example.fixture", "1.0.0", KernelApiVersion(1, 0),
        required_capabilities=required_capabilities, optional_capabilities=optional_capabilities,
        operations=(OperationDescriptor(operation_id, input_schema, output_schema, "read", (GRANT,)),),
    )
    return compose((feature,), {"optional.fixture": "unsupported"}, {operation_id: handler})


class KernelCompositionTests(unittest.TestCase):
    def test_permissive_result_schema_still_requires_bounded_strict_json(self):
        cycle = []
        cycle.append(cycle)
        too_deep = None
        for _ in range(66):
            too_deep = [too_deep]
        class StringSubclass(str):
            pass
        invalid = (
            object(), {"nested": object()}, {1: "not a JSON key"}, (1, 2), StringSubclass("text"),
            float("nan"), float("inf"), cycle, too_deep, "\ud800", {"\ud800": "invalid key"},
            [None] * 4097, {str(index): None for index in range(1025)}, "x" * 1048577,
        )
        for index, value in enumerate(invalid):
            with self.subTest(index=index):
                composition = KernelComposition(
                    fixture_kernel(lambda params, grants: value, output_schema=PERMISSIVE_SCHEMA),
                    operator_grants=(GRANT,),
                )
                with self.assertRaises(KernelInvocationError):
                    composition.invoke(OPERATION, {})

    def test_result_detaches_valid_json_without_coercion(self):
        original = {"values": [None, True, 2, 1.5, "text", {"nested": False}]}
        composition = KernelComposition(fixture_kernel(lambda params, grants: original, output_schema=PERMISSIVE_SCHEMA),
                                        operator_grants=(GRANT,))
        result = composition.invoke(OPERATION, {})
        self.assertEqual(result, original)
        original["values"].append("later mutation")
        self.assertNotEqual(result, original)

    def test_missing_or_invalid_schema_references_fail_composition(self):
        for reference in ("", "contracts/missing.schema.json", INPUT_SCHEMA + "#/missing", "https://example.test/schema"):
            for field in ("input_schema", "output_schema"):
                with self.subTest(reference=reference, field=field), self.assertRaises(CompositionError):
                    KernelComposition(fixture_kernel(lambda params, grants: {}, **{field: reference}))

    def test_existing_schema_fragment_is_supported(self):
        schema = "contracts/engine.v1/vocabulary.schema.json#/definitions/operation_descriptor"
        composition = KernelComposition(fixture_kernel(lambda params, grants: {"status": "ok"}, input_schema=schema),
                                        operator_grants=(GRANT,))
        self.assertEqual(composition.invoke(OPERATION, {
            "operation_id": "com.example.another.operation", "input_schema_id": INPUT_SCHEMA,
            "output_schema_id": OUTPUT_SCHEMA, "effect": "read",
        }), {"status": "ok"})

    def test_optional_capability_failure_keeps_feature_available(self):
        kernel = fixture_kernel(lambda params, grants: {"status": "ok"}, optional_capabilities=("optional.fixture",))
        composition = KernelComposition(kernel, operator_grants=(GRANT,))
        self.assertEqual(composition.invoke(OPERATION, {}), {"status": "ok"})
        self.assertEqual(kernel.unavailable_optional_capabilities(), (("com.example.fixture", "optional.fixture"),))
        with self.assertRaises(CompositionError):
            fixture_kernel(lambda params, grants: {}, required_capabilities=("required.fixture",))


class KernelSocketCompositionTests(unittest.TestCase):
    def test_bad_json_result_returns_fixed_error_and_connection_remains_usable(self):
        results = iter((object(), {"nested": object()}, {1: "wrong key"}, {"status": "ok"}))
        composition = KernelComposition(
            fixture_kernel(lambda params, grants: next(results), output_schema=PERMISSIVE_SCHEMA),
            operator_grants=(GRANT,),
        )
        with self.connection(composition) as session:
            for _ in range(3):
                response = self.call(session)
                self.assertEqual(response["error"]["data"]["code"], "internal")
                self.assertEqual(response["error"]["message"], "operation failed")
            self.assertEqual(self.call(session)["result"], {"status": "ok"})

    def test_result_plus_response_envelope_is_preflighted_without_closing_connection(self):
        # This string meets json_value's own length bound. The JSON-RPC
        # envelope makes the actual frame exceed the existing transport cap.
        results = iter(("x" * 1048576, {"status": "ok"}))
        composition = KernelComposition(
            fixture_kernel(lambda params, grants: next(results), output_schema=PERMISSIVE_SCHEMA),
            operator_grants=(GRANT,),
        )
        with self.connection(composition) as session:
            response = self.call(session)
            self.assertEqual(response["error"]["data"]["code"], "internal")
            self.assertEqual(response["error"]["message"], "operation failed")
            self.assertEqual(self.call(session)["result"], {"status": "ok"})

    def directory(self):
        directory = tempfile.TemporaryDirectory(prefix="kernel-composition-")
        self.addCleanup(directory.cleanup)
        return Path(directory.name).resolve()

    def build(self, composition):
        return build_engine_server(
            state_root=self.directory(), artifact_root=self.directory(), socket_root=self.directory(),
            legacy_agents_dir=self.directory(),
            default_connection_id="550e8400-e29b-41d4-a716-446655440002",
            source_root=repo_root(), kernel_composition=composition,
        )

    @contextmanager
    def connection(self, composition, *, authenticate=True):
        runtime = self.build(composition)
        runtime.server.start()
        try:
            descriptor = load_rendezvous_file(runtime.rendezvous_path)
            client = UnixSocketEngineClient(descriptor.socket_path)
            with client.session() as session:
                if authenticate:
                    response = session.call({
                        "jsonrpc": "2.0", "id": 1, "method": "engine.v1.hello", "params": {
                            "client_name": "kernel-fixture", "offered_api": {"major": 1, "minor": 0},
                            "authentication": {
                                "engine_instance_id": descriptor.engine_instance_id,
                                "instance_nonce": descriptor.instance_nonce,
                                "credential": runtime.enrollment.credential_path.read_text().strip(),
                            },
                        },
                    })
                    self.assertTrue(response["result"]["authenticated"])
                yield session
        finally:
            runtime.server.stop()

    def call(self, session, method=OPERATION, params=None, **extra):
        return session.call({"jsonrpc": "2.0", "id": 2, "method": method, "params": params or {}, **extra})

    def test_unknown_namespaced_operation_is_authenticated_discoverable_and_invocable(self):
        observed = []
        def handler(params, grants):
            observed.append((params, grants))
            return {"status": "ok"}
        grants = [GRANT]
        composition = KernelComposition(fixture_kernel(handler), operator_grants=grants)
        grants.clear()
        with self.connection(composition) as session:
            listing = self.call(session, "engine.v1.operations.list")["result"]["operations"]
            described = next(row for row in listing if row["operation_id"] == OPERATION)
            self.assertEqual(described, {
                "operation_id": OPERATION, "input_schema_id": INPUT_SCHEMA,
                "output_schema_id": OUTPUT_SCHEMA, "effect": "read", "required_grants": [GRANT],
            })
            self.assertEqual(self.call(session)["result"], {"status": "ok"})
            self.assertEqual(self.call(session, "engine.v1.health")["result"], {"status": "ok"})
            self.assertEqual(self.call(session, "engine.v1.models.list")["result"]["items"], [])
        self.assertEqual(observed, [({}, frozenset({GRANT}))])

    def test_unauthenticated_invocation_never_calls_handler(self):
        observed = []
        composition = KernelComposition(fixture_kernel(lambda params, grants: observed.append(params)), operator_grants=(GRANT,))
        with self.connection(composition, authenticate=False) as session:
            self.assertEqual(self.call(session)["error"]["data"]["code"], "capability_denied")
        self.assertEqual(observed, [])

    def test_frame_grants_cannot_override_trusted_composition(self):
        observed = []
        composition = KernelComposition(fixture_kernel(lambda params, grants: observed.append(params)))
        with self.connection(composition) as session:
            response = self.call(session, grants=[GRANT], operator_grants=[GRANT])
            self.assertEqual(response["error"]["data"]["code"], "capability_denied")
        self.assertEqual(observed, [])

    def test_invalid_input_does_not_dispatch_and_redacts_values(self):
        observed = []
        composition = KernelComposition(fixture_kernel(lambda params, grants: observed.append(params)), operator_grants=(GRANT,))
        with self.connection(composition) as session:
            response = self.call(session, params={"secret-sentinel": "fixture-secret"})
            self.assertEqual(response["error"]["code"], -32602)
            self.assertNotIn("secret-sentinel", str(response))
            self.assertNotIn("fixture-secret", str(response))
        self.assertEqual(observed, [])

    def test_invalid_output_and_handler_failures_are_redacted(self):
        def fail(params, grants):
            raise RuntimeError("fixture-secret")
        for handler in (fail, lambda params, grants: {"status": "fixture-secret"}):
            with self.subTest(handler=handler):
                composition = KernelComposition(fixture_kernel(handler), operator_grants=(GRANT,))
                with self.connection(composition) as session:
                    response = self.call(session)
                    self.assertEqual(response["error"]["data"]["code"], "internal")
                    self.assertNotIn("fixture-secret", str(response))

    def test_collision_with_enabled_or_disabled_static_operation_fails_before_serving(self):
        for operation in ("engine.v1.health", "engine.v1.models.register"):
            with self.subTest(operation=operation), self.assertRaises(CompositionError):
                self.build(KernelComposition(fixture_kernel(lambda params, grants: {}, operation_id=operation)))

    def test_absent_composition_preserves_static_discovery(self):
        with self.connection(None) as session:
            listing = self.call(session, "engine.v1.operations.list")["result"]["operations"]
            self.assertNotIn(OPERATION, [row["operation_id"] for row in listing])
            self.assertEqual(self.call(session)["error"]["data"]["code"], "unsupported_capability")

    def test_builtin_discovery_is_served_from_the_composed_descriptors(self):
        """The built-ins the engine composed describe themselves; nothing is listed twice."""
        core = builtin_descriptor(FEATURE_CORE)
        with self.connection(None) as session:
            listing = self.call(session, "engine.v1.operations.list")["result"]["operations"]
        rows = {row["operation_id"]: row for row in listing}
        self.assertEqual(len(rows), len(listing))
        self.assertEqual(set(rows), {item.operation_id for item in core.operations})
        for item in core.operations:
            # required_grants only reaches the wire from a composed descriptor:
            # the static dispatch table has no such column.
            self.assertEqual(rows[item.operation_id], {
                "operation_id": item.operation_id,
                "input_schema_id": item.input_schema_id,
                "output_schema_id": item.output_schema_id,
                "effect": item.effect,
                "required_grants": [],
            })

    def test_caller_composition_is_listed_beside_the_static_residue(self):
        composition = KernelComposition(fixture_kernel(lambda params, grants: {"status": "ok"}),
                                        operator_grants=(GRANT,))
        with self.connection(composition) as session:
            listing = self.call(session, "engine.v1.operations.list")["result"]["operations"]
        operation_ids = [row["operation_id"] for row in listing]
        self.assertEqual(len(operation_ids), len(set(operation_ids)))
        self.assertIn(OPERATION, operation_ids)
        self.assertIn("engine.v1.models.list", operation_ids)

    def test_caller_composition_lists_exactly_the_preexisting_operations(self):
        """The escape hatch composes no built-ins, so discovery is the static residue plus the caller's."""
        core = builtin_descriptor(FEATURE_CORE)
        composition = KernelComposition(fixture_kernel(lambda params, grants: {"status": "ok"}),
                                        operator_grants=(GRANT,))
        with self.connection(composition) as session:
            listing = self.call(session, "engine.v1.operations.list")["result"]["operations"]
        operation_ids = [row["operation_id"] for row in listing]
        self.assertEqual(len(operation_ids), len(set(operation_ids)))
        self.assertEqual(
            set(operation_ids),
            {item.operation_id for item in core.operations} | {OPERATION},
        )

    def test_combined_discovery_limit_fails_before_serving(self):
        operations = tuple(OperationDescriptor(f"com.example.fixture.op{index}", INPUT_SCHEMA, OUTPUT_SCHEMA, "read")
                           for index in range(508))
        feature = FeatureDescriptor("com.example.fixture", "1.0.0", KernelApiVersion(1, 0), operations=operations)
        kernel = compose((feature,), {}, {operation.operation_id: lambda params, grants: {"status": "ok"}
                                          for operation in operations})
        composition = KernelComposition(kernel)
        with self.assertRaisesRegex(CompositionError, "combined operation discovery"):
            self.build(composition)


if __name__ == "__main__":
    unittest.main()

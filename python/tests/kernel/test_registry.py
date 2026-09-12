"""Acceptance tests for the B17 kernel registry slice."""
import unittest

from model_deck.kernel.registry import (
    ComposedKernel,
    CompositionError,
    EventDescriptor,
    FeatureDescriptor,
    GrantDeniedError,
    KernelApiVersion,
    OperationDescriptor,
    UnknownOperationError,
    compose,
)

API = KernelApiVersion(major=1, minimum_minor=0)


def _op(op_id, grants=(), effect="read"):
    return OperationDescriptor(
        operation_id=op_id,
        input_schema_id="contracts/input.json",
        output_schema_id="contracts/output.json",
        effect=effect,
        required_grants=tuple(grants),
    )


def _pass(value):
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return value


def _feature(fid, ops=(), events=(), deps=(), req=(), opt=(), api=API, version="1.0.0"):
    return FeatureDescriptor(
        feature_id=fid,
        version=version,
        api=api,
        dependencies=_pass(deps),
        required_capabilities=_pass(req),
        optional_capabilities=_pass(opt),
        operations=_pass(ops),
        events=_pass(events),
    )


def _handlers_for(*ops):
    return {op.operation_id: (lambda p, g, oid=op.operation_id: (oid, p)) for op in ops}


class RegistryTest(unittest.TestCase):
    def test_generic_namespaced_operation_dispatch(self):
        op = _op("com.example.kernel.echo")
        feature = _feature("com.example.kernel.base", ops=(op,))
        kernel = compose((feature,), {}, _handlers_for(op))
        self.assertEqual(kernel.invoke("com.example.kernel.echo", {"v": 1}, ()), ("com.example.kernel.echo", {"v": 1}))

    def test_missing_grants_block_handler(self):
        called = []
        op = _op("com.example.kernel.write", grants=("kernel.write",), effect="write")
        feature = _feature("com.example.kernel.base", ops=(op,))
        kernel = compose((feature,), {}, {op.operation_id: lambda p, g: called.append(p)})
        with self.assertRaises(GrantDeniedError):
            kernel.invoke("com.example.kernel.write", {}, ())
        self.assertEqual(called, [])
        kernel.invoke("com.example.kernel.write", {"a": 1}, ("kernel.write",))
        self.assertEqual(called, [{"a": 1}])

    def test_unknown_operation(self):
        kernel = compose((), {}, {})
        with self.assertRaises(UnknownOperationError):
            kernel.invoke("com.example.kernel.missing", {}, ())

    def test_duplicate_feature_rejected(self):
        with self.assertRaises(CompositionError):
            compose((_feature("com.example.kernel.a"), _feature("com.example.kernel.a")), {}, {})

    def test_duplicate_operation_rejected(self):
        op = _op("com.example.kernel.echo")
        with self.assertRaises(CompositionError):
            compose((_feature("com.example.kernel.a", ops=(op,)), _feature("com.example.kernel.b", ops=(_op("com.example.kernel.echo"),))), {}, {})

    def test_duplicate_event_rejected(self):
        evt = EventDescriptor(event_id="com.example.kernel.done", schema_id="contracts/e.json")
        with self.assertRaises(CompositionError):
            compose((_feature("com.example.kernel.a", events=(evt,)), _feature("com.example.kernel.b", events=(EventDescriptor(event_id="com.example.kernel.done", schema_id="contracts/e.json"),))), {}, {})

    def test_api_major_incompatibility(self):
        with self.assertRaises(CompositionError):
            compose((_feature("com.example.kernel.a", api=KernelApiVersion(major=2, minimum_minor=0)),), {}, {})

    def test_api_minor_incompatibility(self):
        with self.assertRaises(CompositionError):
            compose((_feature("com.example.kernel.a", api=KernelApiVersion(major=1, minimum_minor=99)),), {}, {})

    def test_required_capability_states(self):
        op = _op("com.example.kernel.echo")
        feature = _feature("com.example.kernel.a", ops=(op,), req=("kernel.exec",))
        kernel = compose((feature,), {"kernel.exec": "supported"}, _handlers_for(op))
        self.assertEqual(kernel.feature_order(), ("com.example.kernel.a",))
        for state in ("unknown", "unsupported"):
            with self.assertRaises(CompositionError):
                compose((feature,), {"kernel.exec": state}, _handlers_for(op))
        with self.assertRaises(CompositionError):
            compose((feature,), {}, _handlers_for(op))

    def test_optional_degradation_keeps_feature(self):
        op = _op("com.example.kernel.echo")
        feature = _feature("com.example.kernel.a", ops=(op,), opt=("kernel.fast",))
        kernel = compose((feature,), {"kernel.fast": "unknown"}, _handlers_for(op))
        self.assertEqual(kernel.feature_order(), ("com.example.kernel.a",))
        self.assertEqual(kernel.unavailable_optional_capabilities(), (("com.example.kernel.a", "kernel.fast"),))
        self.assertEqual(kernel.invoke("com.example.kernel.echo", 1, ()), ("com.example.kernel.echo", 1))

    def test_missing_dependency(self):
        with self.assertRaises(CompositionError):
            compose((_feature("com.example.kernel.a", deps=("com.example.kernel.missing",)),), {}, {})

    def test_cycle_diagnostic(self):
        a = _feature("com.example.kernel.a", deps=("com.example.kernel.b",))
        b = _feature("com.example.kernel.b", deps=("com.example.kernel.a",))
        with self.assertRaises(CompositionError) as ctx:
            compose((a, b), {}, {})
        self.assertIn("cycle", str(ctx.exception))
        self.assertIn("com.example.kernel.a", str(ctx.exception))

    def test_deterministic_dependency_order(self):
        c = _feature("com.example.kernel.c", deps=("com.example.kernel.a", "com.example.kernel.b"))
        a = _feature("com.example.kernel.a")
        b = _feature("com.example.kernel.b")
        first = compose((c, b, a), {}, {}).feature_order()
        second = compose((a, c, b), {}, {}).feature_order()
        self.assertEqual(first, second)
        self.assertEqual(first[-1], "com.example.kernel.c")

    def test_failed_composition_exposes_no_partial_registry(self):
        op = _op("com.example.kernel.echo")
        good = _feature("com.example.kernel.good", ops=(op,))
        bad = _feature("com.example.kernel.bad", deps=("com.example.kernel.missing",))
        with self.assertRaises(CompositionError):
            compose((good, bad), {}, _handlers_for(op))

    def test_immutable_detached_listings(self):
        op = _op("com.example.kernel.echo")
        evt = EventDescriptor(event_id="com.example.kernel.done", schema_id="contracts/e.json")
        feature = _feature("com.example.kernel.a", ops=(op,), events=(evt,))
        kernel = compose((feature,), {}, _handlers_for(op))
        ops = kernel.operations()
        with self.assertRaises(AttributeError):
            ops.append(op)  # type: ignore[attr-defined]
        self.assertEqual(len(kernel.operations()), 1)
        with self.assertRaises(AttributeError):
            kernel.invoke = lambda *a: None  # type: ignore[method-assign]
        with self.assertRaises(CompositionError):
            OperationDescriptor(operation_id="bad id", input_schema_id="x", output_schema_id="y", effect="read")

    def test_malformed_descriptors_rejected(self):
        with self.assertRaises(CompositionError):
            _feature("not-a-reverse-domain", version="1.0.0")
        with self.assertRaises(CompositionError):
            _feature("com.example.kernel.a", version="1.0")
        with self.assertRaises(CompositionError):
            _op("com.example.kernel.echo", effect="delete")

    def test_kernel_imports_stay_free(self):
        import ast
        import model_deck.kernel.registry as registry
        with open(registry.__file__, encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module)
        for name in imported:
            self.assertFalse(name == "model_deck.engine" or name.startswith("model_deck.engine."), name)
            self.assertFalse(name == "model_deck.adapters" or name.startswith("model_deck.adapters."), name)
            self.assertFalse(name == "model_deck.integrations" or name.startswith("model_deck.integrations."), name)
        for banned in ("model_deck.engine", "model_deck.adapters", "model_deck.integrations"):
            self.assertNotIn(f"import {banned}", source)
            self.assertNotIn(f"from {banned}", source)

    def test_prerelease_version_accepted(self):
        feature = _feature("com.example.kernel.a", version="1.2.3-rc.1")
        kernel = compose((feature,), {}, {})
        self.assertEqual(kernel.feature_order(), ("com.example.kernel.a",))

    def test_schema_fragment_accepted(self):
        op = OperationDescriptor(
            operation_id="com.example.kernel.echo",
            input_schema_id="contracts/engine.v1/vocabulary.schema.json#/definitions/operation_descriptor",
            output_schema_id="contracts/output.json",
            effect="read",
        )
        feature = _feature("com.example.kernel.a", ops=(op,))
        kernel = compose((feature,), {}, _handlers_for(op))
        self.assertEqual(len(kernel.operations()), 1)

    def test_schema_id_frozen_parity(self):
        for schema in ("", "a\x00b", "x" * 256):
            op = OperationDescriptor(
                operation_id="com.example.kernel.echo", input_schema_id=schema,
                output_schema_id="contracts/output.json", effect="read")
            self.assertEqual(op.input_schema_id, schema)
        evt = EventDescriptor(event_id="com.example.kernel.done", schema_id="")
        self.assertEqual(evt.schema_id, "")
        with self.assertRaises(CompositionError):
            OperationDescriptor(
                operation_id="com.example.kernel.echo", input_schema_id="x" * 257,
                output_schema_id="contracts/output.json", effect="read")
        with self.assertRaises(CompositionError):
            OperationDescriptor(
                operation_id="com.example.kernel.echo", input_schema_id=None,
                output_schema_id="contracts/output.json", effect="read")

    def test_grant_frozen_parity(self):
        op = _op("com.example.kernel.echo", grants=("", "a:b/c?d", "x" * 64, "dup", "dup"))
        self.assertEqual(op.required_grants[:3], ("", "a:b/c?d", "x" * 64))
        with self.assertRaises(CompositionError):
            _op("com.example.kernel.echo", grants=("x" * 65,))
        with self.assertRaises(CompositionError):
            _op("com.example.kernel.echo", grants=tuple(["g"] * 33))
        for bad in ("kernel.write", "", b"kernel.write", None):
            with self.assertRaises(CompositionError):
                OperationDescriptor(
                    operation_id="com.example.kernel.echo", input_schema_id="i",
                    output_schema_id="o", effect="read", required_grants=bad)

    def test_malformed_containers_rejected(self):
        bads = (None, 5, "com.example.kernel.dep", b"x", {"a": 1}, {"a"}, True)
        for bad in bads:
            with self.assertRaises(CompositionError, msg=f"deps {bad!r}"):
                _feature("com.example.kernel.a", deps=bad)
            with self.assertRaises(CompositionError, msg=f"req {bad!r}"):
                _feature("com.example.kernel.a", req=bad)
            with self.assertRaises(CompositionError, msg=f"opt {bad!r}"):
                _feature("com.example.kernel.a", opt=bad)
            with self.assertRaises(CompositionError, msg=f"ops {bad!r}"):
                _feature("com.example.kernel.a", ops=bad)
            with self.assertRaises(CompositionError, msg=f"events {bad!r}"):
                _feature("com.example.kernel.a", events=bad)

    def test_caller_grants_shape_rejected_before_handler(self):
        called = []
        op = _op("com.example.kernel.echo")
        feature = _feature("com.example.kernel.a", ops=(op,))
        kernel = compose((feature,), {}, _handlers_for(op))
        for bad in ("kernel.write", "", b"x", None, 5):
            with self.assertRaises(GrantDeniedError):
                kernel.invoke("com.example.kernel.echo", {}, bad)
        self.assertEqual(called, [])

    def test_capability_freeform_and_limits(self):
        op = _op("com.example.kernel.echo")
        feature = _feature("com.example.kernel.a", ops=(op,), req=("weird capability!?",))
        kernel = compose((feature,), {"weird capability!?": "supported"}, _handlers_for(op))
        self.assertEqual(kernel.feature_order(), ("com.example.kernel.a",))
        with self.assertRaises(CompositionError):
            _feature("com.example.kernel.a", req=("x" * 65,))
        with self.assertRaises(CompositionError):
            _feature("com.example.kernel.a", req=("",))

    def test_id_max_length_rejected(self):
        long_id = "com.example." + "a" * 250
        self.assertGreater(len(long_id), 256)
        with self.assertRaises(CompositionError):
            _feature(long_id)

    def test_bool_api_fields_rejected(self):
        with self.assertRaises(CompositionError):
            KernelApiVersion(major=True, minimum_minor=0)
        with self.assertRaises(CompositionError):
            KernelApiVersion(major=1, minimum_minor=False)

    def test_noncallable_handler_rejected(self):
        op = _op("com.example.kernel.echo")
        feature = _feature("com.example.kernel.a", ops=(op,))
        with self.assertRaises(CompositionError):
            compose((feature,), {}, {op.operation_id: "not-callable"})

    def test_capability_duplicates_and_overlap_rejected(self):
        with self.assertRaises(CompositionError) as ctx:
            _feature("com.example.kernel.a", req=("kernel.exec", "kernel.exec"))
        self.assertIn("com.example.kernel.a", str(ctx.exception))
        with self.assertRaises(CompositionError) as ctx:
            _feature("com.example.kernel.a", opt=("kernel.fast", "kernel.fast"))
        self.assertIn("com.example.kernel.a", str(ctx.exception))
        with self.assertRaises(CompositionError) as ctx:
            _feature("com.example.kernel.a", req=("kernel.exec",), opt=("kernel.exec",))
        self.assertIn("com.example.kernel.a", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

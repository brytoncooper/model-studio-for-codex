"""Composing the engine kernel at real startup: what must fail, and what must degrade."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest

from model_deck.adapters.transport.rendezvous import load_rendezvous_file
from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
from model_deck.bootstrap import (
    AdditionalEngineFeatures,
    EngineCompositionError,
    build_engine_server,
)
from model_deck.engine.builtins import (
    CAPABILITY_PROVIDER_EXECUTION,
    CAPABILITY_PROVIDER_ROUTES,
    CAPABILITY_SUPPORTED,
    CAPABILITY_UNSUPPORTED,
    FEATURE_PROVIDER,
    BuiltinWiringError,
    builtin_descriptor,
    builtin_handler_registry,
    select_available_builtins,
)
from model_deck.engine.evidence.feature import (
    CAPABILITY_EVIDENCE_CACHE,
    CAPABILITY_EVIDENCE_REFRESH,
    evidence_feature_descriptors,
)
from model_deck.engine.kernel_composition import KernelComposition
from model_deck.engine.usage.feature import (
    CAPABILITY_USAGE_SUMMARY,
    usage_summary_feature_descriptors,
)
from model_deck.kernel import (
    CompositionError,
    FeatureDescriptor,
    KernelApiVersion,
    OperationDescriptor,
    compose,
)
from model_deck_contracts.paths import repo_root

CONNECTION_ID = "550e8400-e29b-41d4-a716-446655440002"
INPUT_SCHEMA = "contracts/engine.v1/methods/health.params.schema.json"
OUTPUT_SCHEMA = "contracts/engine.v1/methods/health.result.schema.json"
OPTIONAL_CAPABILITY = "com.example.optional"
MISSING_CAPABILITY = "com.example.missing"


def operation(operation_id: str) -> OperationDescriptor:
    return OperationDescriptor(operation_id, INPUT_SCHEMA, OUTPUT_SCHEMA, "read")


def feature(
    feature_id: str,
    *,
    operations=(),
    dependencies=(),
    required_capabilities=(),
    optional_capabilities=(),
    api=KernelApiVersion(1, 0),
) -> FeatureDescriptor:
    return FeatureDescriptor(
        feature_id,
        "1.0.0",
        api,
        dependencies=tuple(dependencies),
        required_capabilities=tuple(required_capabilities),
        optional_capabilities=tuple(optional_capabilities),
        operations=tuple(operations),
    )


LEGACY_FEATURE = "com.example.legacy"
LEGACY_OPERATION = "com.example.legacy.inspect"


def legacy_composition() -> KernelComposition:
    """A kernel a caller composed itself, the way the escape hatch is used."""
    return KernelComposition(
        compose(
            (feature(LEGACY_FEATURE, operations=(operation(LEGACY_OPERATION),)),),
            {},
            {LEGACY_OPERATION: (lambda params, grants: {"status": "ok"})},
        )
    )


def serve_ok(descriptor, collaborators):
    """One handler adapter that answers every operation of its feature."""
    return {item.operation_id: (lambda params, grants: {"status": "ok"}) for item in descriptor.operations}


@contextmanager
def authenticated_session(test_case, runtime):
    """Start an engine, hold one authenticated socket session, then stop it."""
    runtime.server.start()
    try:
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        client = UnixSocketEngineClient(descriptor.socket_path)
        with client.session() as session:
            response = session.call({
                "jsonrpc": "2.0", "id": 1, "method": "engine.v1.hello",
                "params": {
                    "client_name": "kernel-startup", "offered_api": {"major": 1, "minor": 0},
                    "authentication": {
                        "engine_instance_id": descriptor.engine_instance_id,
                        "instance_nonce": descriptor.instance_nonce,
                        "credential": runtime.enrollment.credential_path.read_text().strip(),
                    },
                },
            })
            test_case.assertTrue(response["result"]["authenticated"])
            yield session
    finally:
        runtime.server.stop()


class EngineStartupCompositionTests(unittest.TestCase):
    """Each fault fails a real build_engine_server with the startup exception."""

    def directory(self) -> Path:
        directory = tempfile.TemporaryDirectory(prefix="kernel-startup-")
        self.addCleanup(directory.cleanup)
        return Path(directory.name).resolve()

    def build(self, **overrides):
        arguments = dict(
            state_root=self.directory(),
            artifact_root=self.directory(),
            socket_root=self.directory(),
            legacy_agents_dir=self.directory(),
            default_connection_id=CONNECTION_ID,
            source_root=repo_root(),
        )
        return build_engine_server(**{**arguments, **overrides})

    def test_operation_collision_with_a_builtin_fails_startup(self):
        additional = AdditionalEngineFeatures(
            features=(feature("com.example.collide", operations=(operation("engine.v1.health"),)),),
            handler_adapters={"com.example.collide": serve_ok},
        )
        with self.assertRaises(EngineCompositionError) as raised:
            self.build(additional_features=additional)
        self.assertIn("engine.v1.health", str(raised.exception))

    def test_incompatible_kernel_api_fails_startup(self):
        for api in (KernelApiVersion(2, 0), KernelApiVersion(1, 99)):
            additional = AdditionalEngineFeatures(
                features=(feature("com.example.future", api=api),),
                handler_adapters={"com.example.future": serve_ok},
            )
            with self.subTest(api=api), self.assertRaises(EngineCompositionError) as raised:
                self.build(additional_features=additional)
            self.assertIn("incompatible kernel API", str(raised.exception))

    def test_missing_required_capability_fails_startup(self):
        additional = AdditionalEngineFeatures(
            features=(
                feature(
                    "com.example.needy",
                    operations=(operation("com.example.needy.inspect"),),
                    required_capabilities=(MISSING_CAPABILITY,),
                ),
            ),
            handler_adapters={"com.example.needy": serve_ok},
        )
        with self.assertRaises(EngineCompositionError) as raised:
            self.build(additional_features=additional)
        self.assertIn(MISSING_CAPABILITY, str(raised.exception))

    def test_dependency_cycle_fails_startup(self):
        additional = AdditionalEngineFeatures(
            features=(
                feature("com.example.first", dependencies=("com.example.second",)),
                feature("com.example.second", dependencies=("com.example.first",)),
            ),
            handler_adapters={"com.example.first": serve_ok, "com.example.second": serve_ok},
        )
        with self.assertRaises(EngineCompositionError) as raised:
            self.build(additional_features=additional)
        self.assertIn("dependency cycle", str(raised.exception))

    def test_startup_failures_are_composition_errors(self):
        """A caller that only knows the kernel's own error type still catches these."""
        self.assertTrue(issubclass(EngineCompositionError, CompositionError))

    def test_additional_feature_cannot_restate_a_builtin_capability(self):
        additional = AdditionalEngineFeatures(
            features=(feature("com.example.restate"),),
            capabilities={CAPABILITY_PROVIDER_EXECUTION: CAPABILITY_SUPPORTED},
        )
        with self.assertRaises(EngineCompositionError) as raised:
            self.build(additional_features=additional)
        self.assertIn(CAPABILITY_PROVIDER_EXECUTION, str(raised.exception))

    def test_startup_fault_leaves_no_socket_behind(self):
        socket_root = self.directory()
        additional = AdditionalEngineFeatures(
            features=(feature("com.example.orphan", dependencies=("com.example.absent",)),),
            handler_adapters={"com.example.orphan": serve_ok},
        )
        with self.assertRaises(EngineCompositionError):
            self.build(socket_root=socket_root, additional_features=additional)
        self.assertFalse((socket_root / "engine.sock").exists())


class OptionalCapabilityDegradationTests(unittest.TestCase):
    """An absent optional capability degrades one feature and nothing else."""

    DEGRADED_FEATURE = "com.example.degraded"
    DEGRADED_OPERATION = "com.example.degraded.inspect"

    def directory(self) -> Path:
        directory = tempfile.TemporaryDirectory(prefix="kernel-degraded-")
        self.addCleanup(directory.cleanup)
        return Path(directory.name).resolve()

    def additional(self, state: str) -> AdditionalEngineFeatures:
        return AdditionalEngineFeatures(
            features=(
                feature(
                    self.DEGRADED_FEATURE,
                    operations=(operation(self.DEGRADED_OPERATION),),
                    optional_capabilities=(OPTIONAL_CAPABILITY,),
                ),
            ),
            handler_adapters={self.DEGRADED_FEATURE: serve_ok},
            capabilities={OPTIONAL_CAPABILITY: state},
        )

    def connection(self, runtime):
        return authenticated_session(self, runtime)

    def build(self, state: str):
        return build_engine_server(
            state_root=self.directory(),
            artifact_root=self.directory(),
            socket_root=self.directory(),
            legacy_agents_dir=self.directory(),
            default_connection_id=CONNECTION_ID,
            source_root=repo_root(),
            additional_features=self.additional(state),
        )

    def test_unsupported_optional_capability_keeps_unrelated_operations_serving(self):
        runtime = self.build(CAPABILITY_UNSUPPORTED)
        self.assertEqual(
            runtime.degraded_optional_capabilities,
            ((self.DEGRADED_FEATURE, OPTIONAL_CAPABILITY),),
        )
        with self.connection(runtime) as session:
            models = session.call({
                "jsonrpc": "2.0", "id": 2, "method": "engine.v1.models.list",
                "params": {"collection": "registered"},
            })
            self.assertEqual(models["result"], {"collection": "registered", "items": []})
            listing = session.call({
                "jsonrpc": "2.0", "id": 3, "method": "engine.v1.operations.list", "params": {},
            })["result"]["operations"]
            # The degraded feature still serves: the degradation is reported as
            # metadata beside the catalog, not by withdrawing the operation.
            self.assertIn(self.DEGRADED_OPERATION, [row["operation_id"] for row in listing])
            degraded = session.call({
                "jsonrpc": "2.0", "id": 4, "method": self.DEGRADED_OPERATION, "params": {},
            })
            self.assertEqual(degraded["result"], {"status": "ok"})

    def test_supported_optional_capability_reports_no_degradation(self):
        runtime = self.build(CAPABILITY_SUPPORTED)
        self.assertEqual(runtime.degraded_optional_capabilities, ())


class ProviderCapabilityTests(unittest.TestCase):
    """The injected provider ports are the provider feature's whole contribution."""

    def directory(self) -> Path:
        directory = tempfile.TemporaryDirectory(prefix="kernel-provider-")
        self.addCleanup(directory.cleanup)
        return Path(directory.name).resolve()

    def test_provider_capabilities_are_unsupported_without_an_injected_port(self):
        runtime = build_engine_server(
            state_root=self.directory(),
            artifact_root=self.directory(),
            socket_root=self.directory(),
            legacy_agents_dir=self.directory(),
            default_connection_id=CONNECTION_ID,
            source_root=repo_root(),
        )
        self.assertEqual(runtime.kernel_capabilities[CAPABILITY_PROVIDER_EXECUTION], CAPABILITY_UNSUPPORTED)
        self.assertEqual(runtime.kernel_capabilities[CAPABILITY_PROVIDER_ROUTES], CAPABILITY_UNSUPPORTED)

    def test_injected_provider_marks_execution_and_routes_supported(self):
        from model_deck.adapters.providers.deterministic import DeterministicProviderExecutionPort
        from model_deck.adapters.routing.registered import ProviderRouteDefinition
        from model_deck.engine.routing.ports import CapabilityFeature, CapabilityTriState, ExecutionMode

        provider_id = "com.example.injected"
        definition = ProviderRouteDefinition(
            ExecutionMode.CUSTOM,
            (CapabilityFeature("tools", CapabilityTriState.SUPPORTED),),
            "ref:startup.capabilities",
        )
        runtime = build_engine_server(
            state_root=self.directory(),
            artifact_root=self.directory(),
            socket_root=self.directory(),
            legacy_agents_dir=self.directory(),
            default_connection_id=CONNECTION_ID,
            source_root=repo_root(),
            enable_application_state=True,
            provider_execution=DeterministicProviderExecutionPort(provider_id=provider_id),
            provider_route_definitions={provider_id: definition},
        )
        self.assertEqual(runtime.kernel_capabilities[CAPABILITY_PROVIDER_EXECUTION], CAPABILITY_SUPPORTED)
        self.assertEqual(runtime.kernel_capabilities[CAPABILITY_PROVIDER_ROUTES], CAPABILITY_SUPPORTED)
        self.assertEqual(runtime.degraded_optional_capabilities, ())

    def test_injected_provider_without_routes_degrades_only_the_route_capability(self):
        from model_deck.adapters.providers.deterministic import DeterministicProviderExecutionPort

        runtime = build_engine_server(
            state_root=self.directory(),
            artifact_root=self.directory(),
            socket_root=self.directory(),
            legacy_agents_dir=self.directory(),
            default_connection_id=CONNECTION_ID,
            source_root=repo_root(),
            enable_application_state=True,
            provider_execution=DeterministicProviderExecutionPort(provider_id="com.example.injected"),
            provider_route_definitions={},
        )
        self.assertEqual(runtime.kernel_capabilities[CAPABILITY_PROVIDER_EXECUTION], CAPABILITY_SUPPORTED)
        self.assertEqual(runtime.kernel_capabilities[CAPABILITY_PROVIDER_ROUTES], CAPABILITY_UNSUPPORTED)
        self.assertEqual(
            runtime.degraded_optional_capabilities,
            ((FEATURE_PROVIDER, CAPABILITY_PROVIDER_ROUTES),),
        )

    def test_composed_features_are_all_discoverable(self):
        """Every operation of every composed built-in reaches operations.list."""
        runtime = build_engine_server(
            state_root=self.directory(),
            artifact_root=self.directory(),
            socket_root=self.directory(),
            legacy_agents_dir=self.directory(),
            default_connection_id=CONNECTION_ID,
            source_root=repo_root(),
            enable_application_state=True,
            enable_fixture_runs=True,
        )
        expected = {
            item.operation_id
            for composed in select_available_builtins(runtime.kernel_capabilities)
            for item in composed.operations
        }
        self.assertIn("engine.v1.runs.start", expected)
        with authenticated_session(self, runtime) as session:
            listing = session.call({
                "jsonrpc": "2.0", "id": 2, "method": "engine.v1.operations.list", "params": {},
            })["result"]["operations"]
        self.assertEqual(expected - {row["operation_id"] for row in listing}, set())

    def test_provider_feature_refuses_to_bind_without_the_injected_port(self):
        provider = builtin_descriptor(FEATURE_PROVIDER)
        registry = builtin_handler_registry()
        with self.assertRaises(BuiltinWiringError):
            registry.build_handlers((provider,), {})

        class Port:
            def start(self, request, sink):
                raise AssertionError("the provider feature must not invoke the port")

        self.assertEqual(registry.build_handlers((provider,), {"provider_execution": Port()}), {})


class LegacyKernelCompositionTests(unittest.TestCase):
    """The escape hatch serves the caller's kernel and composes nothing else."""

    def directory(self) -> Path:
        directory = tempfile.TemporaryDirectory(prefix="kernel-legacy-")
        self.addCleanup(directory.cleanup)
        return Path(directory.name).resolve()

    def build(self, **overrides):
        arguments = dict(
            state_root=self.directory(),
            artifact_root=self.directory(),
            socket_root=self.directory(),
            legacy_agents_dir=self.directory(),
            default_connection_id=CONNECTION_ID,
            source_root=repo_root(),
        )
        return build_engine_server(**{**arguments, **overrides})

    def test_additional_features_cannot_ride_the_legacy_escape_hatch(self):
        """Composing nothing cannot honour extra features, so the pair is refused."""
        additional = AdditionalEngineFeatures(
            features=(feature("com.example.extra", operations=(operation("com.example.extra.inspect"),)),),
            handler_adapters={"com.example.extra": serve_ok},
        )
        with self.assertRaises(ValueError) as raised:
            self.build(kernel_composition=legacy_composition(), additional_features=additional)
        message = str(raised.exception)
        self.assertIn("additional_features", message)
        self.assertIn("kernel_composition", message)

    def test_legacy_startup_advertises_no_capability_it_does_not_route(self):
        """Evidence and usage totals are wired but unrouted here, so neither is claimed."""
        runtime = self.build(kernel_composition=legacy_composition(), enable_application_state=True)
        for capability in (CAPABILITY_EVIDENCE_CACHE, CAPABILITY_EVIDENCE_REFRESH, CAPABILITY_USAGE_SUMMARY):
            with self.subTest(capability=capability):
                self.assertNotIn(capability, runtime.kernel_capabilities)

    def test_legacy_startup_lists_the_caller_kernel_beside_the_static_residue(self):
        """The engine's own features stay out of discovery on this path."""
        engine_owned = {
            item.operation_id
            for descriptor in (
                *evidence_feature_descriptors(include_refresh=True),
                *usage_summary_feature_descriptors(),
            )
            for item in descriptor.operations
        }
        runtime = self.build(kernel_composition=legacy_composition(), enable_application_state=True)
        with authenticated_session(self, runtime) as session:
            listing = session.call({
                "jsonrpc": "2.0", "id": 2, "method": "engine.v1.operations.list", "params": {},
            })["result"]["operations"]
        operation_ids = [row["operation_id"] for row in listing]
        self.assertEqual(len(operation_ids), len(set(operation_ids)))
        self.assertIn(LEGACY_OPERATION, operation_ids)
        self.assertIn("engine.v1.models.list", operation_ids)
        self.assertEqual(engine_owned & set(operation_ids), set())


if __name__ == "__main__":
    unittest.main()

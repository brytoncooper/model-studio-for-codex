"""Acceptance tests for the built-in engine feature descriptors (B17 seam)."""
import unittest

from model_deck.engine.builtins import (
    BUILTIN_CAPABILITY_IDS,
    BUILTIN_FEATURE_IDS,
    CAPABILITY_PROVIDER_EXECUTION,
    CAPABILITY_SUPPORTED,
    CAPABILITY_UNKNOWN,
    CAPABILITY_UNSUPPORTED,
    FEATURE_CORE,
    FEATURE_EVENTS,
    FEATURE_HOST_PROJECTION,
    FEATURE_PROVIDER,
    FEATURE_SESSIONS_RUNS,
    BuiltinAvailability,
    BuiltinHandlerRegistry,
    BuiltinWiringError,
    build_capability_map,
    builtin_descriptor,
    builtin_feature_descriptors,
    builtin_operation_ids,
    fully_supported_capability_map,
    select_available_builtins,
    unknown_capabilities,
)
from model_deck.kernel import CompositionError, FeatureDescriptor, KernelApiVersion, compose

# Imported for assertion only: the descriptors must stay in step with the
# hardwired dispatch catalog until C1b and C8d retire the if-chain.
from model_deck.engine.dispatch import _OPERATION_CATALOG

# Dispatch serves engine.v1.hosts.projection_status but the static catalog does
# not list it, so the descriptors are a superset by exactly this one operation.
CATALOG_GAP = ("engine.v1.hosts.projection_status",)


def _handlers_for(features):
    """A handler map that covers every operation the given features declare."""
    return {
        operation.operation_id: (lambda params, grants: {})
        for feature in features
        for operation in feature.operations
    }


class BuiltinCatalogCoverageTests(unittest.TestCase):
    def test_every_catalog_operation_belongs_to_exactly_one_descriptor(self):
        owners = {}
        for descriptor in builtin_feature_descriptors():
            for operation in descriptor.operations:
                self.assertNotIn(
                    operation.operation_id,
                    owners,
                    f"{operation.operation_id} is claimed by {owners.get(operation.operation_id)} "
                    f"and {descriptor.feature_id}",
                )
                owners[operation.operation_id] = descriptor.feature_id
        catalog_ids = {entry["operation_id"] for entry in _OPERATION_CATALOG}
        self.assertEqual(sorted(catalog_ids - set(owners)), [])
        self.assertEqual(sorted(set(owners) - catalog_ids), sorted(CATALOG_GAP))

    def test_descriptor_operations_match_the_catalog_schemas_and_effects(self):
        catalog = {entry["operation_id"]: entry for entry in _OPERATION_CATALOG}
        for descriptor in builtin_feature_descriptors():
            for operation in descriptor.operations:
                entry = catalog.get(operation.operation_id)
                if entry is None:
                    self.assertIn(operation.operation_id, CATALOG_GAP)
                    continue
                self.assertEqual(operation.input_schema_id, entry["input_schema_id"])
                self.assertEqual(operation.output_schema_id, entry["output_schema_id"])
                self.assertEqual(operation.effect, entry["effect"])

    def test_operation_ids_accessor_is_sorted_and_complete(self):
        declared = sorted(
            operation.operation_id
            for descriptor in builtin_feature_descriptors()
            for operation in descriptor.operations
        )
        self.assertEqual(list(builtin_operation_ids()), declared)

    def test_projection_status_is_owned_by_the_host_projection_feature(self):
        descriptor = builtin_descriptor(FEATURE_HOST_PROJECTION)
        self.assertEqual(
            [operation.operation_id for operation in descriptor.operations],
            list(CATALOG_GAP),
        )


class BuiltinCapabilityVocabularyTests(unittest.TestCase):
    def test_declared_capabilities_resolve_against_the_vocabulary(self):
        self.assertEqual(unknown_capabilities(), ())
        for descriptor in builtin_feature_descriptors():
            for capability in (
                *descriptor.required_capabilities,
                *descriptor.optional_capabilities,
            ):
                self.assertIn(capability, BUILTIN_CAPABILITY_IDS)

    def test_availability_record_builds_a_tri_state_capability_map(self):
        availability = BuiltinAvailability(
            models_library=True,
            connections=False,
            sessions_runs=True,
        )
        capabilities = build_capability_map(availability)
        self.assertEqual(sorted(capabilities), sorted(BUILTIN_CAPABILITY_IDS))
        self.assertEqual(capabilities["models.library"], CAPABILITY_SUPPORTED)
        self.assertEqual(capabilities["connections"], CAPABILITY_UNSUPPORTED)
        self.assertEqual(capabilities["usage"], CAPABILITY_UNKNOWN)

    def test_availability_rejects_non_boolean_injection_facts(self):
        with self.assertRaises(BuiltinWiringError):
            BuiltinAvailability(models_library="yes")

    def test_fully_supported_map_covers_the_whole_vocabulary(self):
        capabilities = fully_supported_capability_map()
        self.assertEqual(sorted(capabilities), sorted(BUILTIN_CAPABILITY_IDS))
        self.assertEqual(set(capabilities.values()), {CAPABILITY_SUPPORTED})


class BuiltinCompositionTests(unittest.TestCase):
    def test_full_capability_map_composes_every_builtin(self):
        capabilities = fully_supported_capability_map()
        features = select_available_builtins(capabilities)
        self.assertEqual(sorted(f.feature_id for f in features), sorted(BUILTIN_FEATURE_IDS))
        kernel = compose(features, capabilities, _handlers_for(features))
        self.assertEqual(sorted(kernel.feature_order()), sorted(BUILTIN_FEATURE_IDS))
        self.assertEqual(kernel.unavailable_optional_capabilities(), ())
        self.assertEqual(
            sorted(op.operation_id for op in kernel.operations()),
            list(builtin_operation_ids()),
        )

    def test_core_precedes_its_dependents_in_the_composed_order(self):
        capabilities = fully_supported_capability_map()
        features = select_available_builtins(capabilities)
        order = compose(features, capabilities, _handlers_for(features)).feature_order()
        self.assertLess(order.index(FEATURE_CORE), order.index(FEATURE_SESSIONS_RUNS))
        self.assertLess(order.index(FEATURE_SESSIONS_RUNS), order.index(FEATURE_EVENTS))

    def test_unsupported_provider_execution_degrades_only_the_provider_group(self):
        capabilities = fully_supported_capability_map()
        capabilities[CAPABILITY_PROVIDER_EXECUTION] = CAPABILITY_UNSUPPORTED
        features = select_available_builtins(capabilities)
        composed_ids = sorted(f.feature_id for f in features)
        self.assertEqual(
            composed_ids,
            sorted(fid for fid in BUILTIN_FEATURE_IDS if fid != FEATURE_PROVIDER),
        )
        kernel = compose(features, capabilities, _handlers_for(features))
        self.assertNotIn(FEATURE_PROVIDER, kernel.feature_order())
        self.assertEqual(kernel.unavailable_optional_capabilities(), ())
        # Every operation still dispatches; only the provider feature is gone.
        self.assertEqual(
            sorted(op.operation_id for op in kernel.operations()),
            list(builtin_operation_ids()),
        )

    def test_unsupported_provider_routes_degrades_the_provider_group_in_place(self):
        capabilities = fully_supported_capability_map()
        capabilities["provider.routes"] = CAPABILITY_UNSUPPORTED
        features = select_available_builtins(capabilities)
        self.assertIn(FEATURE_PROVIDER, {f.feature_id for f in features})
        kernel = compose(features, capabilities, _handlers_for(features))
        self.assertEqual(
            kernel.unavailable_optional_capabilities(),
            ((FEATURE_PROVIDER, "provider.routes"),),
        )

    def test_dropping_a_feature_also_drops_its_dependents(self):
        capabilities = fully_supported_capability_map()
        capabilities["sessions.runs"] = CAPABILITY_UNSUPPORTED
        features = select_available_builtins(capabilities)
        dropped = set(BUILTIN_FEATURE_IDS) - {f.feature_id for f in features}
        self.assertEqual(dropped, {FEATURE_SESSIONS_RUNS, FEATURE_EVENTS, FEATURE_PROVIDER})
        # The surviving set is still composable rather than missing a dependency.
        compose(features, capabilities, _handlers_for(features))

    def test_unknown_capability_state_is_not_supported(self):
        capabilities = fully_supported_capability_map()
        capabilities[CAPABILITY_PROVIDER_EXECUTION] = CAPABILITY_UNKNOWN
        features = select_available_builtins(capabilities)
        self.assertNotIn(FEATURE_PROVIDER, {f.feature_id for f in features})

    def test_duplicate_feature_injected_into_the_set_is_diagnosed(self):
        capabilities = fully_supported_capability_map()
        features = select_available_builtins(capabilities)
        duplicated = (*features, builtin_descriptor(FEATURE_CORE))
        with self.assertRaises(CompositionError) as raised:
            compose(duplicated, capabilities, _handlers_for(duplicated))
        self.assertIn("duplicate feature_id", str(raised.exception))

    def test_cycle_injected_into_the_set_is_diagnosed(self):
        capabilities = fully_supported_capability_map()
        features = list(select_available_builtins(capabilities))
        cycling_core = FeatureDescriptor(
            feature_id=FEATURE_CORE,
            version="1.0.0",
            api=KernelApiVersion(major=1, minimum_minor=0),
            dependencies=(FEATURE_SESSIONS_RUNS,),
            operations=builtin_descriptor(FEATURE_CORE).operations,
        )
        features = [cycling_core if f.feature_id == FEATURE_CORE else f for f in features]
        with self.assertRaises(CompositionError) as raised:
            compose(features, capabilities, _handlers_for(features))
        self.assertIn("dependency cycle", str(raised.exception))


class BuiltinHandlerRegistryTests(unittest.TestCase):
    def test_registry_builds_handlers_for_the_selected_features(self):
        descriptor = builtin_descriptor(FEATURE_CORE)
        expected = {operation.operation_id: (lambda params, grants: {})
                    for operation in descriptor.operations}
        registry = BuiltinHandlerRegistry({FEATURE_CORE: lambda d, c: expected})
        handlers = registry.build_handlers([descriptor], {"list_models": object()})
        self.assertEqual(sorted(handlers), sorted(expected))
        self.assertEqual(registry.feature_ids(), (FEATURE_CORE,))

    def test_feature_without_operations_needs_no_adapter(self):
        registry = BuiltinHandlerRegistry({})
        self.assertEqual(registry.build_handlers([builtin_descriptor(FEATURE_PROVIDER)], {}), {})

    def test_missing_adapter_for_a_serving_feature_is_diagnosed(self):
        registry = BuiltinHandlerRegistry({})
        with self.assertRaises(BuiltinWiringError):
            registry.build_handlers([builtin_descriptor(FEATURE_CORE)], {})

    def test_adapter_binding_a_foreign_operation_is_diagnosed(self):
        registry = BuiltinHandlerRegistry(
            {FEATURE_CORE: lambda d, c: {"engine.v1.usage.query": (lambda params, grants: {})}}
        )
        with self.assertRaises(BuiltinWiringError):
            registry.build_handlers([builtin_descriptor(FEATURE_CORE)], {})

    def test_adapter_leaving_an_operation_unbound_is_diagnosed(self):
        descriptor = builtin_descriptor(FEATURE_CORE)
        partial = {descriptor.operations[0].operation_id: (lambda params, grants: {})}
        registry = BuiltinHandlerRegistry({FEATURE_CORE: lambda d, c: partial})
        with self.assertRaises(BuiltinWiringError):
            registry.build_handlers([descriptor], {})

    def test_registering_the_same_feature_twice_is_diagnosed(self):
        registry = BuiltinHandlerRegistry({FEATURE_CORE: lambda d, c: {}})
        with self.assertRaises(BuiltinWiringError):
            registry.with_adapter(FEATURE_CORE, lambda d, c: {})


if __name__ == "__main__":
    unittest.main()

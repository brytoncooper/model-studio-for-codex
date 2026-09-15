"""Built-in engine features expressed as kernel feature descriptors.

Each descriptor names the operations one collaborator group serves and the
capability that group needs. The concrete ports stay injected by bootstrap and
reach the kernel through the handler adapters in ``handlers.py``; nothing here
imports an adapter or an integration.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from model_deck.kernel import FeatureDescriptor, KernelApiVersion, OperationDescriptor

from model_deck.engine.builtins.capabilities import (
    CAPABILITY_CONNECTIONS,
    CAPABILITY_EVENTS,
    CAPABILITY_EXTERNAL_EXTENSIONS,
    CAPABILITY_HOST_OPERATIONS,
    CAPABILITY_HOST_PROJECTION,
    CAPABILITY_HOST_SETTINGS,
    CAPABILITY_JOBS,
    CAPABILITY_MODELS_LIBRARY,
    CAPABILITY_PROVIDER_EXECUTION,
    CAPABILITY_PROVIDER_ROUTES,
    CAPABILITY_SESSIONS_RUNS,
    CAPABILITY_SUPPORTED,
    CAPABILITY_USAGE,
    BUILTIN_CAPABILITY_IDS,
    BuiltinWiringError,
)

BUILTIN_API = KernelApiVersion(major=1, minimum_minor=0)
BUILTIN_VERSION = "1.0.0"

OPERATION_PREFIX = "engine.v1."
SCHEMA_PREFIX = "contracts/engine.v1/methods/"

FEATURE_CORE = "modeldeck.builtin.core"
FEATURE_MODELS_LIBRARY = "modeldeck.builtin.models-library"
FEATURE_CONNECTIONS = "modeldeck.builtin.connections"
FEATURE_SESSIONS_RUNS = "modeldeck.builtin.sessions-runs"
FEATURE_EVENTS = "modeldeck.builtin.events"
FEATURE_HOST_SETTINGS = "modeldeck.builtin.host-settings"
FEATURE_HOST_PROJECTION = "modeldeck.builtin.host-projection"
FEATURE_HOST_OPERATIONS = "modeldeck.builtin.host-operations"
FEATURE_EXTERNAL_EXTENSIONS = "modeldeck.builtin.external-extensions"
FEATURE_JOBS = "modeldeck.builtin.jobs"
FEATURE_USAGE = "modeldeck.builtin.usage"
FEATURE_PROVIDER = "modeldeck.builtin.provider"


def _operation(short_name: str, effect: str) -> OperationDescriptor:
    """Build one operation descriptor from the engine method naming convention."""
    return OperationDescriptor(
        operation_id=f"{OPERATION_PREFIX}{short_name}",
        input_schema_id=f"{SCHEMA_PREFIX}{short_name}.params.schema.json",
        output_schema_id=f"{SCHEMA_PREFIX}{short_name}.result.schema.json",
        effect=effect,
    )


def _feature(
    feature_id: str,
    *,
    operations: Sequence[tuple[str, str]] = (),
    dependencies: Sequence[str] = (),
    required_capabilities: Sequence[str] = (),
    optional_capabilities: Sequence[str] = (),
) -> FeatureDescriptor:
    return FeatureDescriptor(
        feature_id=feature_id,
        version=BUILTIN_VERSION,
        api=BUILTIN_API,
        dependencies=tuple(dependencies),
        required_capabilities=tuple(required_capabilities),
        optional_capabilities=tuple(optional_capabilities),
        operations=tuple(_operation(name, effect) for name, effect in operations),
    )


# The core group mirrors the always-implemented dispatch base methods: they need
# no injected collaborator, so they declare no required capability.
_CORE = _feature(
    FEATURE_CORE,
    operations=(
        ("hello", "read"),
        ("health", "read"),
        ("operations.list", "read"),
        ("capabilities.get", "read"),
        ("models.list", "read"),
    ),
)

_MODELS_LIBRARY = _feature(
    FEATURE_MODELS_LIBRARY,
    operations=(
        ("models.register", "write"),
        ("models.rename", "write"),
        ("models.remove", "write"),
    ),
    dependencies=(FEATURE_CORE,),
    required_capabilities=(CAPABILITY_MODELS_LIBRARY,),
)

_CONNECTIONS = _feature(
    FEATURE_CONNECTIONS,
    operations=(
        ("connections.list", "read"),
        ("connections.save", "write"),
    ),
    dependencies=(FEATURE_CORE,),
    required_capabilities=(CAPABILITY_CONNECTIONS,),
)

# Sessions and runs compose without a provider (fixture execution), so they name
# no provider capability; provider capabilities belong to the provider group below.
_SESSIONS_RUNS = _feature(
    FEATURE_SESSIONS_RUNS,
    operations=(
        ("sessions.create", "write"),
        ("sessions.get", "read"),
        ("sessions.select_model", "write"),
        ("runs.start", "write"),
        ("runs.get", "read"),
        ("runs.cancel", "write"),
        ("runs.submit_tool_result", "write"),
    ),
    dependencies=(FEATURE_CORE,),
    required_capabilities=(CAPABILITY_SESSIONS_RUNS,),
)

_EVENTS = _feature(
    FEATURE_EVENTS,
    operations=(
        ("events.subscribe", "write"),
        ("events.ack", "write"),
        ("events.unsubscribe", "write"),
    ),
    dependencies=(FEATURE_SESSIONS_RUNS,),
    required_capabilities=(CAPABILITY_EVENTS,),
)

_HOST_SETTINGS = _feature(
    FEATURE_HOST_SETTINGS,
    operations=(
        ("hosts.settings.read", "read"),
        ("hosts.settings.validate", "read"),
        ("hosts.settings.preview", "read"),
        ("hosts.settings.save", "write"),
    ),
    dependencies=(FEATURE_CORE,),
    required_capabilities=(CAPABILITY_HOST_SETTINGS,),
)

# hosts.projection_status is dispatched today but is absent from the static
# dispatch operation catalog; the descriptor keeps the method set complete.
_HOST_PROJECTION = _feature(
    FEATURE_HOST_PROJECTION,
    operations=(("hosts.projection_status", "read"),),
    dependencies=(FEATURE_CORE,),
    required_capabilities=(CAPABILITY_HOST_PROJECTION,),
)

_HOST_OPERATIONS = _feature(
    FEATURE_HOST_OPERATIONS,
    operations=(
        ("hosts.list", "read"),
        ("hosts.prepare", "write"),
    ),
    dependencies=(FEATURE_CORE,),
    required_capabilities=(CAPABILITY_HOST_OPERATIONS,),
)

_EXTERNAL_EXTENSIONS = _feature(
    FEATURE_EXTERNAL_EXTENSIONS,
    operations=(
        ("extensions.install", "write"),
        ("extensions.enable", "write"),
        ("extensions.disable", "write"),
        ("extensions.get", "read"),
        ("extensions.list", "read"),
        ("extensions.inspect", "read"),
        ("extensions.update", "write"),
        ("extensions.remove", "write"),
        ("operations.invoke", "write"),
        ("ui.contributions.list", "read"),
        ("ui.panel.get", "read"),
    ),
    dependencies=(FEATURE_CORE,),
    required_capabilities=(CAPABILITY_EXTERNAL_EXTENSIONS,),
)

# Jobs answer for whatever job repository is composed, so they depend on core
# rather than on the external extension host: an engine with no plugins still
# has first-party refresh jobs to observe and cancel. The `jobs` capability is
# what says a repository reached the engine.
_JOBS = _feature(
    FEATURE_JOBS,
    operations=(
        ("jobs.get", "read"),
        ("jobs.cancel", "write"),
    ),
    dependencies=(FEATURE_CORE,),
    required_capabilities=(CAPABILITY_JOBS,),
)

_USAGE = _feature(
    FEATURE_USAGE,
    operations=(("usage.query", "read"),),
    dependencies=(FEATURE_CORE,),
    required_capabilities=(CAPABILITY_USAGE,),
)

# A specialized provider port is a descriptor, not a kernel port: it declares the
# capability it provides and serves no operation of its own. Bootstrap supplies
# the concrete execution port through this feature's handler adapter.
_PROVIDER = _feature(
    FEATURE_PROVIDER,
    dependencies=(FEATURE_SESSIONS_RUNS,),
    required_capabilities=(CAPABILITY_PROVIDER_EXECUTION,),
    optional_capabilities=(CAPABILITY_PROVIDER_ROUTES,),
)

_BUILTIN_DESCRIPTORS: tuple[FeatureDescriptor, ...] = (
    _CORE,
    _MODELS_LIBRARY,
    _CONNECTIONS,
    _SESSIONS_RUNS,
    _EVENTS,
    _HOST_SETTINGS,
    _HOST_PROJECTION,
    _HOST_OPERATIONS,
    _EXTERNAL_EXTENSIONS,
    _JOBS,
    _USAGE,
    _PROVIDER,
)

BUILTIN_FEATURE_IDS: tuple[str, ...] = tuple(
    descriptor.feature_id for descriptor in _BUILTIN_DESCRIPTORS
)


def builtin_feature_descriptors() -> tuple[FeatureDescriptor, ...]:
    """Every built-in descriptor, in declaration order."""
    return _BUILTIN_DESCRIPTORS


def builtin_descriptor(feature_id: str) -> FeatureDescriptor:
    """One built-in descriptor by feature ID."""
    for descriptor in _BUILTIN_DESCRIPTORS:
        if descriptor.feature_id == feature_id:
            return descriptor
    raise BuiltinWiringError(f"unknown built-in feature: {feature_id!r}")


def builtin_operation_ids() -> tuple[str, ...]:
    """Every operation ID the built-in descriptors serve, sorted."""
    return tuple(
        sorted(
            operation.operation_id
            for descriptor in _BUILTIN_DESCRIPTORS
            for operation in descriptor.operations
        )
    )


def select_available_builtins(
    capabilities: Mapping[str, str],
    features: Sequence[FeatureDescriptor] | None = None,
) -> tuple[FeatureDescriptor, ...]:
    """Drop features whose required capabilities are not supported.

    A feature that depends on a dropped feature is dropped too, so the result is
    always composable: ``compose`` would otherwise fail on a missing dependency.
    Optional capabilities are left alone; the kernel degrades those itself.
    """
    if not isinstance(capabilities, Mapping):
        raise BuiltinWiringError("capabilities must be a mapping of capability ID to state")
    candidates = tuple(_BUILTIN_DESCRIPTORS if features is None else features)
    for descriptor in candidates:
        if not isinstance(descriptor, FeatureDescriptor):
            raise BuiltinWiringError(f"malformed built-in feature: {descriptor!r}")
    kept: dict[str, FeatureDescriptor] = {
        descriptor.feature_id: descriptor
        for descriptor in candidates
        if all(
            capabilities.get(capability) == CAPABILITY_SUPPORTED
            for capability in descriptor.required_capabilities
        )
    }
    # Repeat until stable so one drop propagates along a whole dependency chain.
    dropped_this_pass = True
    while dropped_this_pass:
        dropped_this_pass = False
        for feature_id, descriptor in tuple(kept.items()):
            if any(dependency not in kept for dependency in descriptor.dependencies):
                del kept[feature_id]
                dropped_this_pass = True
    return tuple(descriptor for descriptor in candidates if descriptor.feature_id in kept)


def unknown_capabilities(features: Sequence[FeatureDescriptor] | None = None) -> tuple[str, ...]:
    """Capabilities a descriptor names that the vocabulary does not define."""
    descriptors = _BUILTIN_DESCRIPTORS if features is None else tuple(features)
    named = {
        capability
        for descriptor in descriptors
        for capability in (*descriptor.required_capabilities, *descriptor.optional_capabilities)
    }
    return tuple(sorted(named - set(BUILTIN_CAPABILITY_IDS)))

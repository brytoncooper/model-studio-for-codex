"""Capability vocabulary for the built-in engine features.

A capability ID names one collaborator group that bootstrap either injects or
leaves out. Descriptors in this package refer to capabilities only by these
constants, so the kernel never learns what a concrete port is made of.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from types import MappingProxyType

from model_deck.kernel import CompositionError

CAPABILITY_SUPPORTED = "supported"
CAPABILITY_UNSUPPORTED = "unsupported"
CAPABILITY_UNKNOWN = "unknown"
CAPABILITY_STATES: tuple[str, ...] = (
    CAPABILITY_SUPPORTED,
    CAPABILITY_UNSUPPORTED,
    CAPABILITY_UNKNOWN,
)

CAPABILITY_MODELS_LIBRARY = "models.library"
CAPABILITY_CONNECTIONS = "connections"
CAPABILITY_SESSIONS_RUNS = "sessions.runs"
CAPABILITY_EVENTS = "events"
CAPABILITY_HOST_SETTINGS = "hosts.settings"
CAPABILITY_HOST_PROJECTION = "hosts.projection"
CAPABILITY_HOST_OPERATIONS = "hosts.operations"
CAPABILITY_EXTERNAL_EXTENSIONS = "extensions.external"
CAPABILITY_JOBS = "jobs"
CAPABILITY_USAGE = "usage"
CAPABILITY_PROVIDER_EXECUTION = "provider.execution"
CAPABILITY_PROVIDER_ROUTES = "provider.routes"

CAPABILITY_MEANINGS: Mapping[str, str] = MappingProxyType(
    {
        CAPABILITY_MODELS_LIBRARY: (
            "Model registration, rename and removal ports are injected; the read-only "
            "catalog listing belongs to the always-present core group instead."
        ),
        CAPABILITY_CONNECTIONS: "Connection listing and saving ports are injected.",
        CAPABILITY_SESSIONS_RUNS: (
            "Session and run use cases are injected, so sessions and runs can be created, "
            "read and cancelled."
        ),
        CAPABILITY_EVENTS: (
            "A run repository and an event replay port are injected, so event "
            "subscriptions can be served."
        ),
        CAPABILITY_HOST_SETTINGS: "A host settings document port and caller context are injected.",
        CAPABILITY_HOST_PROJECTION: "A projection coordinator is injected.",
        CAPABILITY_HOST_OPERATIONS: "A host integration port is injected.",
        CAPABILITY_EXTERNAL_EXTENSIONS: "An external extension host is injected.",
        CAPABILITY_JOBS: (
            "A job repository is reachable for reads and cancels: the engine's own "
            "first-party jobs, the injected external extension host's, or both."
        ),
        CAPABILITY_USAGE: "A usage query use case is injected.",
        CAPABILITY_PROVIDER_EXECUTION: (
            "A provider execution port is injected, so runs reach a real provider instead "
            "of degrading to fixture execution."
        ),
        CAPABILITY_PROVIDER_ROUTES: (
            "Provider route definitions are injected alongside the execution port, so "
            "routes can be resolved by name."
        ),
    }
)

BUILTIN_CAPABILITY_IDS: tuple[str, ...] = tuple(CAPABILITY_MEANINGS)


class BuiltinWiringError(CompositionError):
    """A built-in descriptor set, availability record or handler binding is wrong."""


@dataclass(frozen=True)
class BuiltinAvailability:
    """What bootstrap actually injected, one field per capability ID.

    ``True`` means the collaborator was supplied, ``False`` means it was not, and
    ``None`` means the caller could not determine it. ``None`` becomes the kernel
    state ``"unknown"``, which never satisfies a required capability.
    """

    models_library: bool | None = None
    connections: bool | None = None
    sessions_runs: bool | None = None
    events: bool | None = None
    host_settings: bool | None = None
    host_projection: bool | None = None
    host_operations: bool | None = None
    external_extensions: bool | None = None
    jobs: bool | None = None
    usage: bool | None = None
    provider_execution: bool | None = None
    provider_routes: bool | None = None

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if value is not None and not isinstance(value, bool):
                raise BuiltinWiringError(
                    f"availability for {field.name} must be True, False or None: {value!r}"
                )


_FIELD_TO_CAPABILITY: Mapping[str, str] = MappingProxyType(
    {
        "models_library": CAPABILITY_MODELS_LIBRARY,
        "connections": CAPABILITY_CONNECTIONS,
        "sessions_runs": CAPABILITY_SESSIONS_RUNS,
        "events": CAPABILITY_EVENTS,
        "host_settings": CAPABILITY_HOST_SETTINGS,
        "host_projection": CAPABILITY_HOST_PROJECTION,
        "host_operations": CAPABILITY_HOST_OPERATIONS,
        "external_extensions": CAPABILITY_EXTERNAL_EXTENSIONS,
        "jobs": CAPABILITY_JOBS,
        "usage": CAPABILITY_USAGE,
        "provider_execution": CAPABILITY_PROVIDER_EXECUTION,
        "provider_routes": CAPABILITY_PROVIDER_ROUTES,
    }
)


def state_for_injection(injected: bool | None) -> str:
    """Translate one injection fact into a kernel capability state."""
    if injected is None:
        return CAPABILITY_UNKNOWN
    return CAPABILITY_SUPPORTED if injected else CAPABILITY_UNSUPPORTED


def build_capability_map(availability: BuiltinAvailability) -> dict[str, str]:
    """Build the tri-state capability map ``compose`` expects.

    Every capability in the vocabulary is present in the result, so a capability
    the caller forgot reads as ``"unknown"`` rather than disappearing.
    """
    if not isinstance(availability, BuiltinAvailability):
        raise BuiltinWiringError("availability must be a BuiltinAvailability")
    capabilities = {
        capability: state_for_injection(getattr(availability, field_name))
        for field_name, capability in _FIELD_TO_CAPABILITY.items()
    }
    missing = [capability for capability in BUILTIN_CAPABILITY_IDS if capability not in capabilities]
    if missing:
        raise BuiltinWiringError(f"capability map is missing vocabulary entries: {sorted(missing)}")
    return capabilities


def fully_supported_capability_map() -> dict[str, str]:
    """Every vocabulary capability marked supported; useful for a complete host."""
    return {capability: CAPABILITY_SUPPORTED for capability in BUILTIN_CAPABILITY_IDS}

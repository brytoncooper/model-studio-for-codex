"""Application-independent atomic composition registry for B17 kernel descriptors."""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

KERNEL_API_MAJOR = 1
KERNEL_API_MINOR = 0

_REVERSE_DOMAIN = re.compile(r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9_-]*)+$")
_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z0-9.]+)?$")
_EFFECTS = ("read", "write")
_CAPABILITY_STATES = ("supported", "unsupported", "unknown")
_MAX_GRANTS = 32
_MAX_SCHEMA_LEN = 256
_MAX_GRANT_LEN = 64

Handler = Callable[[Any, frozenset[str]], Any]


class CompositionError(Exception):
    """Atomic composition of feature descriptors failed; no registry was produced."""


class UnknownOperationError(LookupError):
    """No registered handler exists for the requested operation ID."""


class GrantDeniedError(PermissionError):
    """Caller grants do not satisfy the operation required grants."""


def _check_id(value: str, kind: str) -> str:
    if not isinstance(value, str) or len(value) < 3 or len(value) > 256 or not _REVERSE_DOMAIN.match(value):
        raise CompositionError(f"malformed {kind}: {value!r}")
    return value


def _check_schema_id(value: str, kind: str) -> str:
    if not isinstance(value, str) or len(value) > _MAX_SCHEMA_LEN:
        raise CompositionError(f"malformed {kind}: {value!r}")
    return value


def _check_grant(value: str) -> str:
    if not isinstance(value, str) or len(value) > _MAX_GRANT_LEN:
        raise CompositionError(f"malformed grant: {value!r}")
    return value


def _check_capability(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise CompositionError(f"malformed capability: {value!r}")
    return value


def _as_tuple(value: Any, field: str, owner: str) -> tuple[Any, ...]:
    if value is None or isinstance(value, (str, bytes, bytearray)):
        raise CompositionError(f"malformed {field} for {owner}: {value!r}")
    if isinstance(value, Mapping):
        raise CompositionError(f"malformed {field} for {owner}: mapping")
    if isinstance(value, (set, frozenset)):
        raise CompositionError(f"malformed {field} for {owner}: unordered set")
    if not isinstance(value, Sequence):
        raise CompositionError(f"malformed {field} for {owner}: {value!r}")
    return tuple(value)


def _check_grants(values: Any, owner: str) -> tuple[str, ...]:
    items = _as_tuple(values, "required_grants", owner)
    grants = tuple(_check_grant(g) for g in items)
    if len(grants) > _MAX_GRANTS:
        raise CompositionError(f"too many required_grants for {owner}")
    return grants


@dataclass(frozen=True)
class KernelApiVersion:
    """Minimum kernel API release a feature requires."""
    major: int
    minimum_minor: int

    def __post_init__(self) -> None:
        if isinstance(self.major, bool) or isinstance(self.minimum_minor, bool):
            raise CompositionError(f"malformed api version: {(self.major, self.minimum_minor)!r}")
        if not isinstance(self.major, int) or not isinstance(self.minimum_minor, int):
            raise CompositionError(f"malformed api version: {(self.major, self.minimum_minor)!r}")
        if self.major < 1 or self.minimum_minor < 0:
            raise CompositionError(f"malformed api version: {(self.major, self.minimum_minor)!r}")


@dataclass(frozen=True)
class OperationDescriptor:
    """Frozen operation descriptor matching the frozen vocabulary shape."""
    operation_id: str
    input_schema_id: str
    output_schema_id: str
    effect: str
    required_grants: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _check_id(self.operation_id, "operation_id")
        _check_schema_id(self.input_schema_id, "input_schema_id")
        _check_schema_id(self.output_schema_id, "output_schema_id")
        if self.effect not in _EFFECTS:
            raise CompositionError(f"malformed effect: {self.effect!r}")
        object.__setattr__(self, "required_grants", _check_grants(self.required_grants, self.operation_id))


@dataclass(frozen=True)
class EventDescriptor:
    """Frozen event registration metadata for this slice."""
    event_id: str
    schema_id: str
    required_grants: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _check_id(self.event_id, "event_id")
        _check_schema_id(self.schema_id, "schema_id")
        object.__setattr__(self, "required_grants", _check_grants(self.required_grants, self.event_id))


@dataclass(frozen=True)
class FeatureDescriptor:
    """Frozen built-in feature descriptor."""
    feature_id: str
    version: str
    api: KernelApiVersion
    dependencies: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    optional_capabilities: tuple[str, ...] = ()
    operations: tuple[OperationDescriptor, ...] = ()
    events: tuple[EventDescriptor, ...] = ()

    def __post_init__(self) -> None:
        _check_id(self.feature_id, "feature_id")
        if not isinstance(self.version, str) or not _SEMVER.match(self.version):
            raise CompositionError(f"malformed version for {self.feature_id}: {self.version!r}")
        if not isinstance(self.api, KernelApiVersion):
            raise CompositionError(f"malformed api for {self.feature_id}")
        deps = _as_tuple(self.dependencies, "dependencies", self.feature_id)
        req = _as_tuple(self.required_capabilities, "required_capabilities", self.feature_id)
        opt = _as_tuple(self.optional_capabilities, "optional_capabilities", self.feature_id)
        ops = _as_tuple(self.operations, "operations", self.feature_id)
        evts = _as_tuple(self.events, "events", self.feature_id)
        object.__setattr__(self, "dependencies", deps)
        object.__setattr__(self, "required_capabilities", req)
        object.__setattr__(self, "optional_capabilities", opt)
        object.__setattr__(self, "operations", ops)
        object.__setattr__(self, "events", evts)
        for dep in deps:
            _check_id(dep, "dependency")
        for cap in (*req, *opt):
            _check_capability(cap)
        if len(set(deps)) != len(deps):
            raise CompositionError(f"duplicate dependencies for {self.feature_id}")
        if len(set(req)) != len(req):
            raise CompositionError(f"duplicate required_capabilities for {self.feature_id}")
        if len(set(opt)) != len(opt):
            raise CompositionError(f"duplicate optional_capabilities for {self.feature_id}")
        overlap = set(req) & set(opt)
        if overlap:
            raise CompositionError(
                f"capability in both required and optional for {self.feature_id}: {sorted(overlap)}"
            )
        for op in ops:
            if not isinstance(op, OperationDescriptor):
                raise CompositionError(f"malformed operation in {self.feature_id}")
        for evt in evts:
            if not isinstance(evt, EventDescriptor):
                raise CompositionError(f"malformed event in {self.feature_id}")


@dataclass(frozen=True)
class _ResolvedFeature:
    descriptor: FeatureDescriptor
    unavailable_optional: tuple[str, ...] = ()


class ComposedKernel:
    """Immutable composed registry with generic operation dispatch."""

    def __init__(
        self,
        ordered: tuple[_ResolvedFeature, ...],
        operations: dict[str, OperationDescriptor],
        events: dict[str, EventDescriptor],
        handlers: dict[str, Handler],
    ) -> None:
        object.__setattr__(self, "_ordered", ordered)
        object.__setattr__(self, "_operations", dict(operations))
        object.__setattr__(self, "_events", dict(events))
        object.__setattr__(self, "_handlers", dict(handlers))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("ComposedKernel is immutable")

    def feature_order(self) -> tuple[str, ...]:
        """Deterministic dependency-resolved feature IDs."""
        return tuple(r.descriptor.feature_id for r in self._ordered)

    def features(self) -> tuple[FeatureDescriptor, ...]:
        """Feature descriptors in dependency order."""
        return tuple(r.descriptor for r in self._ordered)

    def operations(self) -> tuple[OperationDescriptor, ...]:
        """Operation descriptors sorted by operation ID."""
        return tuple(self._operations[k] for k in sorted(self._operations))

    def events(self) -> tuple[EventDescriptor, ...]:
        """Event descriptors sorted by event ID."""
        return tuple(self._events[k] for k in sorted(self._events))

    def unavailable_optional_capabilities(self) -> tuple[tuple[str, str], ...]:
        """Sorted (feature_id, capability) pairs degraded at composition."""
        pairs = [
            (r.descriptor.feature_id, cap)
            for r in self._ordered
            for cap in r.unavailable_optional
        ]
        return tuple(sorted(pairs))

    def invoke(self, operation_id: str, params: Any, grants: Sequence[str] = ()) -> Any:
        """Dispatch a registered handler after verifying caller grants."""
        descriptor = self._operations.get(operation_id)
        if descriptor is None:
            raise UnknownOperationError(f"unknown operation: {operation_id!r}")
        if grants is None or isinstance(grants, (str, bytes, bytearray)):
            raise GrantDeniedError(f"malformed caller grants for {operation_id}: {grants!r}")
        if isinstance(grants, Mapping):
            raise GrantDeniedError(f"malformed caller grants for {operation_id}: mapping")
        if isinstance(grants, (set, frozenset)):
            granted = frozenset(grants)
            for g in granted:
                if not isinstance(g, str):
                    raise GrantDeniedError(f"malformed caller grant for {operation_id}: {g!r}")
        elif isinstance(grants, Sequence):
            for g in grants:
                if not isinstance(g, str):
                    raise GrantDeniedError(f"malformed caller grant for {operation_id}: {g!r}")
            granted = frozenset(grants)
        else:
            raise GrantDeniedError(f"malformed caller grants for {operation_id}: {grants!r}")
        missing = [g for g in descriptor.required_grants if g not in granted]
        if missing:
            raise GrantDeniedError(f"missing grants for {operation_id}: {sorted(missing)}")
        return self._handlers[operation_id](params, granted)


def _topological_order(features: Mapping[str, FeatureDescriptor]) -> list[str]:
    order: list[str] = []
    permanent: set[str] = set()
    stack: list[str] = []

    def visit(node: str) -> None:
        if node in permanent:
            return
        if node in stack:
            cycle = stack[stack.index(node):] + [node]
            raise CompositionError(f"dependency cycle: {' -> '.join(cycle)}")
        stack.append(node)
        for dep in sorted(features[node].dependencies):
            visit(dep)
        stack.pop()
        permanent.add(node)
        order.append(node)

    for fid in sorted(features):
        visit(fid)
    return order


def compose(
    features: Sequence[FeatureDescriptor],
    capabilities: Mapping[str, str],
    handlers: Mapping[str, Handler],
) -> ComposedKernel:
    """Atomically compose descriptors into an immutable dispatchable registry."""
    by_feature: dict[str, FeatureDescriptor] = {}
    for feature in features:
        if not isinstance(feature, FeatureDescriptor):
            raise CompositionError(f"malformed feature: {feature!r}")
        if feature.feature_id in by_feature:
            raise CompositionError(f"duplicate feature_id: {feature.feature_id}")
        by_feature[feature.feature_id] = feature
    for feature in features:
        if feature.api.major != KERNEL_API_MAJOR:
            raise CompositionError(
                f"incompatible kernel API major for {feature.feature_id}: "
                f"need {feature.api.major}, have {KERNEL_API_MAJOR}"
            )
        if feature.api.minimum_minor > KERNEL_API_MINOR:
            raise CompositionError(
                f"incompatible kernel API minor for {feature.feature_id}: "
                f"need >={feature.api.minimum_minor}, have {KERNEL_API_MINOR}"
            )
        for dep in feature.dependencies:
            if dep not in by_feature:
                raise CompositionError(f"missing dependency for {feature.feature_id}: {dep}")
    operations: dict[str, OperationDescriptor] = {}
    operation_owner: dict[str, str] = {}
    events: dict[str, EventDescriptor] = {}
    for feature in features:
        for op in feature.operations:
            if op.operation_id in operations:
                raise CompositionError(
                    f"duplicate operation_id {op.operation_id} "
                    f"({operation_owner[op.operation_id]} vs {feature.feature_id})"
                )
            operations[op.operation_id] = op
            operation_owner[op.operation_id] = feature.feature_id
        for evt in feature.events:
            if evt.event_id in events:
                raise CompositionError(f"duplicate event_id: {evt.event_id}")
            events[evt.event_id] = evt
    for cap_name, state in capabilities.items():
        _check_capability(cap_name)
        if state not in _CAPABILITY_STATES:
            raise CompositionError(f"malformed capability state for {cap_name}: {state!r}")
    resolved: list[_ResolvedFeature] = []
    for feature in features:
        for cap in feature.required_capabilities:
            if capabilities.get(cap) != "supported":
                raise CompositionError(
                    f"missing required capability for {feature.feature_id}: "
                    f"{cap}={capabilities.get(cap, 'unknown')}"
                )
        unavailable = tuple(
            sorted(c for c in feature.optional_capabilities if capabilities.get(c) != "supported")
        )
        resolved.append(_ResolvedFeature(descriptor=feature, unavailable_optional=unavailable))
    for op_id in handlers:
        if op_id not in operations:
            raise CompositionError(f"handler for unknown operation: {op_id}")
    for op_id in operations:
        if op_id not in handlers:
            raise CompositionError(f"missing handler for operation: {op_id}")
        if not callable(handlers[op_id]):
            raise CompositionError(f"non-callable handler for operation: {op_id}")
    ordered_ids = _topological_order(by_feature)
    by_id = {r.descriptor.feature_id: r for r in resolved}
    ordered = tuple(by_id[fid] for fid in ordered_ids)
    return ComposedKernel(ordered, operations, events, dict(handlers))

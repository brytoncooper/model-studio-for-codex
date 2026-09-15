"""Bind built-in descriptors to the collaborators bootstrap already built.

The kernel only knows operation IDs and handlers. An adapter turns one feature's
descriptor into that feature's handler map, reading whatever ports or use cases
the caller passes in. Adapters live in an explicit registry object, never in a
module-level singleton, so composition stays a value the caller owns.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from model_deck.kernel import FeatureDescriptor
from model_deck.kernel.registry import Handler

from model_deck.engine.builtins.capabilities import BuiltinWiringError

BuiltinCollaborators = Mapping[str, Any]


@runtime_checkable
class BuiltinHandlerAdapter(Protocol):
    """Build one feature's handler map from injected collaborators.

    The returned mapping must cover exactly the operation IDs of the descriptor
    it was called with; anything else is a wiring mistake, not a degradation.
    """

    def __call__(
        self,
        descriptor: FeatureDescriptor,
        collaborators: BuiltinCollaborators,
    ) -> Mapping[str, Handler]:
        ...


class BuiltinHandlerRegistry:
    """Immutable map of feature ID to handler adapter."""

    def __init__(self, adapters: Mapping[str, BuiltinHandlerAdapter]) -> None:
        if not isinstance(adapters, Mapping):
            raise BuiltinWiringError("adapters must be a mapping of feature ID to adapter")
        for feature_id, adapter in adapters.items():
            if not isinstance(feature_id, str) or not feature_id:
                raise BuiltinWiringError(f"malformed adapter feature ID: {feature_id!r}")
            if not callable(adapter):
                raise BuiltinWiringError(f"adapter for {feature_id} is not callable")
        self._adapters: Mapping[str, BuiltinHandlerAdapter] = MappingProxyType(dict(adapters))

    def feature_ids(self) -> tuple[str, ...]:
        """Feature IDs that have an adapter, sorted."""
        return tuple(sorted(self._adapters))

    def adapter_for(self, feature_id: str) -> BuiltinHandlerAdapter | None:
        """The adapter registered for one feature, or None."""
        return self._adapters.get(feature_id)

    def with_adapter(
        self,
        feature_id: str,
        adapter: BuiltinHandlerAdapter,
    ) -> "BuiltinHandlerRegistry":
        """A new registry with one more adapter; registering twice is an error."""
        if feature_id in self._adapters:
            raise BuiltinWiringError(f"duplicate handler adapter for {feature_id}")
        return BuiltinHandlerRegistry({**self._adapters, feature_id: adapter})

    def build_handlers(
        self,
        features: Sequence[FeatureDescriptor],
        collaborators: BuiltinCollaborators,
    ) -> dict[str, Handler]:
        """Build the handler map ``compose`` needs for the given features.

        Fails loudly when a feature that serves operations has no adapter, when an
        adapter returns an operation that is not the feature's own, or when it
        leaves one of the feature's operations unbound.
        """
        if not isinstance(collaborators, Mapping):
            raise BuiltinWiringError("collaborators must be a mapping")
        handlers: dict[str, Handler] = {}
        for descriptor in features:
            if not isinstance(descriptor, FeatureDescriptor):
                raise BuiltinWiringError(f"malformed built-in feature: {descriptor!r}")
            expected = {operation.operation_id for operation in descriptor.operations}
            adapter = self._adapters.get(descriptor.feature_id)
            if adapter is None:
                if expected:
                    raise BuiltinWiringError(
                        f"no handler adapter registered for {descriptor.feature_id}"
                    )
                continue
            produced = adapter(descriptor, collaborators)
            if not isinstance(produced, Mapping):
                raise BuiltinWiringError(
                    f"handler adapter for {descriptor.feature_id} did not return a mapping"
                )
            unexpected = sorted(set(produced) - expected)
            if unexpected:
                raise BuiltinWiringError(
                    f"handler adapter for {descriptor.feature_id} bound foreign operations: "
                    f"{unexpected}"
                )
            unbound = sorted(expected - set(produced))
            if unbound:
                raise BuiltinWiringError(
                    f"handler adapter for {descriptor.feature_id} left operations unbound: "
                    f"{unbound}"
                )
            for operation_id, handler in produced.items():
                if not callable(handler):
                    raise BuiltinWiringError(f"non-callable handler for {operation_id}")
                if operation_id in handlers:
                    raise BuiltinWiringError(f"duplicate handler for {operation_id}")
                handlers[operation_id] = handler
        return handlers

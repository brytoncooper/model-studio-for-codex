"""Handler adapters that bind the built-in descriptors at engine startup.

Discovery is descriptor-driven: once bootstrap composes these features, the
composed kernel owns the operation catalog that ``engine.v1.operations.list``
returns. Routing is deliberately *not* migrated with it. ``EngineDispatch``
still routes every built-in operation through its own method chain so the
engine's domain error vocabulary (``conflict``, ``not_found``,
``unsupported_capability``, ...) survives; the kernel's generic invocation path
would flatten all of those into one ``internal`` error.

So the handlers bound here stand for operations that dispatch serves directly.
They exist to make the composition complete and to fail loudly rather than
quietly if a built-in operation is ever invoked through the kernel instead.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from model_deck.kernel import FeatureDescriptor
from model_deck.kernel.registry import Handler

from model_deck.engine.builtins.capabilities import BuiltinWiringError
from model_deck.engine.builtins.descriptors import BUILTIN_FEATURE_IDS, FEATURE_PROVIDER
from model_deck.engine.builtins.handlers import BuiltinCollaborators, BuiltinHandlerRegistry

COLLABORATOR_PROVIDER_EXECUTION = "provider_execution"
COLLABORATOR_PROVIDER_ROUTES = "provider_routes"


class DispatchRoutedOperationError(RuntimeError):
    """A built-in operation was invoked through the kernel instead of dispatch."""


def _dispatch_routed_handler(operation_id: str) -> Handler:
    def handler(params: Any, grants: frozenset[str]) -> Any:
        raise DispatchRoutedOperationError(operation_id)

    return handler


def dispatch_routed_handlers(
    descriptor: FeatureDescriptor,
    collaborators: BuiltinCollaborators,
) -> dict[str, Handler]:
    """Bind one built-in feature's operations to the dispatch-routed handler."""
    return {
        operation.operation_id: _dispatch_routed_handler(operation.operation_id)
        for operation in descriptor.operations
    }


def provider_feature_handlers(
    descriptor: FeatureDescriptor,
    collaborators: BuiltinCollaborators,
) -> dict[str, Handler]:
    """Bind the provider feature, whose injected ports are its whole contribution.

    The provider feature serves no operation of its own: it declares that a
    provider execution port (and optionally its route definitions) reached the
    engine. Composing it without the port would advertise a capability the
    engine cannot honour, so that is a wiring error, not a degradation.
    """
    if descriptor.operations:
        raise BuiltinWiringError(f"{descriptor.feature_id} must not declare operations")
    execution = collaborators.get(COLLABORATOR_PROVIDER_EXECUTION)
    if execution is None or not callable(getattr(execution, "start", None)):
        raise BuiltinWiringError(
            "provider execution is composed as supported but no execution port was injected"
        )
    routes = collaborators.get(COLLABORATOR_PROVIDER_ROUTES)
    if routes is not None and not isinstance(routes, Mapping):
        raise BuiltinWiringError("provider route definitions must be a mapping")
    return {}


def builtin_handler_registry() -> BuiltinHandlerRegistry:
    """One adapter per built-in feature, in a registry the caller owns."""
    adapters = {feature_id: dispatch_routed_handlers for feature_id in BUILTIN_FEATURE_IDS}
    adapters[FEATURE_PROVIDER] = provider_feature_handlers
    return BuiltinHandlerRegistry(adapters)

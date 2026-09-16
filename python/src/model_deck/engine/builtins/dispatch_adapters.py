"""The handler adapter for the one built-in feature that serves no operation.

Every other built-in feature is bound in ``dispatch_binding.py``, which turns
each built-in operation into a registered handler over the dispatch method that
already serves it. The provider feature has nothing to route: it exists to say
that a provider execution port reached the engine, so its adapter checks the
injected port and returns an empty handler map.
"""
from __future__ import annotations

from collections.abc import Mapping

from model_deck.kernel import FeatureDescriptor
from model_deck.kernel.registry import Handler

from model_deck.engine.builtins.capabilities import BuiltinWiringError
from model_deck.engine.builtins.handlers import BuiltinCollaborators

COLLABORATOR_PROVIDER_EXECUTION = "provider_execution"
COLLABORATOR_PROVIDER_ROUTES = "provider_routes"


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

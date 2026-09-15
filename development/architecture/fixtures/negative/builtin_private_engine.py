"""Fixture: builtin descriptor module reaching a privileged internal lookup."""
from model_deck.engine.routing import _registry


def resolve_provider_port():
    return _registry

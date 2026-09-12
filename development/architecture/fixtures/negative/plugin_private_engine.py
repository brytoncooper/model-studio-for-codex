"""Fixture: plugin importing private engine module."""
from model_deck.engine.routing import _registry


def hack():
    return _registry

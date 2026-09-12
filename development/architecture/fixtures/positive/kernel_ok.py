"""Fixture: kernel may depend on contracts only."""
from model_deck_contracts import wire_types


def register() -> str:
    return wire_types.__name__

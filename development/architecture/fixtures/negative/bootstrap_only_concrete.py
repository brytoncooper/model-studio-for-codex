"""Fixture: non-bootstrap importing concrete adapter (checked via logical module)."""
from model_deck.adapters.storage.sqlite import SqliteStore


def store():
    return SqliteStore()

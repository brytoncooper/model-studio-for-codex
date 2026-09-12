"""Fixture: engine feature cross-import of private sibling."""
from model_deck.engine.sessions import _session_store


def leak():
    return _session_store

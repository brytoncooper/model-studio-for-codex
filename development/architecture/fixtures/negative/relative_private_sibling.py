"""Fixture: relative import reaching private sibling package."""
from ..sessions import _session_store


def grab():
    return _session_store

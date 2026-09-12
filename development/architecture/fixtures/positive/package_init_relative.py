"""Fixture: package __init__ relative import stays in same package."""
from . import registry


def boot():
    return registry

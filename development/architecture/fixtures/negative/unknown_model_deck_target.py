"""Fixture: unresolved model_deck submodule must fail closed."""
from model_deck import unknown_pkg


def leak():
    return unknown_pkg

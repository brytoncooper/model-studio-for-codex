"""Fixture: explicit dynamic loader exception."""
import importlib


def load_plugin(name: str):
    return importlib.import_module("model_deck.bootstrap.loader")

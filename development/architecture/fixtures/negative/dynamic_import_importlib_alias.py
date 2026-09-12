"""Fixture: dynamic import via importlib alias still requires allowlist."""
import importlib as il


def load(name: str):
    return il.import_module(name)

"""Fixture: kernel must not import platform modules."""
import fcntl


def lock() -> None:
    _ = fcntl

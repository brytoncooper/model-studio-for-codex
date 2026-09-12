"""Fixture: providers must not import router authority."""
import local_router


def route():
    return local_router

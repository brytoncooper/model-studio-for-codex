"""Fixture: registry service locator in core layer."""


def resolve():
    return get_service("storage")

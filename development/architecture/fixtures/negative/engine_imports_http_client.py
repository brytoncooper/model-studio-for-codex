"""Fixture: engine must not import stdlib networking (http not on core allowlist)."""
import http.client


def connect():
    return http.client

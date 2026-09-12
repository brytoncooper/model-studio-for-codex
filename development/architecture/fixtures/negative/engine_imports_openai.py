"""Fixture: engine must not import third-party SDK outside stdlib allowlist."""
import openai


def client():
    return openai

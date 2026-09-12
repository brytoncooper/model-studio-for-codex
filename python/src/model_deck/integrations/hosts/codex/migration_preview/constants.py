from __future__ import annotations

import uuid

from model_deck.integrations.hosts.codex.legacy_models import AGENT_MARKER, PROVIDER

LEGACY_CONNECTION_PROVIDER_ID = "com.modeldeck.legacy.openrouter-settings"
LEGACY_MANAGED_PROVIDER_ID = "com.modeldeck.legacy.openrouter-settings"

_CONNECTION_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-4789-a012-3456789abcde")
_MODEL_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
_ENDPOINT_NAMESPACE = uuid.UUID("7c9e6679-7425-40de-944b-e07fc1f90ae7")

KNOWN_SOURCE_PATHS = (
    "preferences.json",
    "endpoints.json",
    "display-names.json",
    "agents",
)

SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "bearer_token",
        "experimental_bearer_token",
        "env_key",
        "http_headers",
        "env_http_headers",
        "token",
        "secret",
        "password",
        "authorization",
    }
)

CLASSIFICATIONS = frozenset(
    {
        "managed_match",
        "drift",
        "foreign",
        "malformed",
        "symlink",
        "collision",
        "missing",
        "orphan",
    }
)

__all__ = [
    "AGENT_MARKER",
    "PROVIDER",
    "LEGACY_CONNECTION_PROVIDER_ID",
    "LEGACY_MANAGED_PROVIDER_ID",
    "KNOWN_SOURCE_PATHS",
    "SECRET_FIELD_NAMES",
    "CLASSIFICATIONS",
    "_CONNECTION_NAMESPACE",
    "_MODEL_NAMESPACE",
    "_ENDPOINT_NAMESPACE",
]

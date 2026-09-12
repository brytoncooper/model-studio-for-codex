from __future__ import annotations

import uuid

from model_deck.integrations.hosts.codex.migration_preview.constants import (
    _CONNECTION_NAMESPACE,
    _ENDPOINT_NAMESPACE,
    _MODEL_NAMESPACE,
)


def connection_id_for_account(account_id: str) -> str:
    return str(uuid.uuid5(_CONNECTION_NAMESPACE, f"account:{account_id.lower()}"))


def connection_id_for_keyless_endpoint(base_url: str) -> str:
    normalized = base_url.rstrip("/").lower()
    return str(uuid.uuid5(_CONNECTION_NAMESPACE, f"endpoint:{normalized}"))


def endpoint_config_ref(base_url: str, wire: str | None) -> str:
    normalized = f"{base_url.rstrip('/').lower()}|{wire or 'auto'}"
    digest = uuid.uuid5(_ENDPOINT_NAMESPACE, normalized).hex
    return f"ref:endpoint.{digest}"


def credential_ref(account_id: str) -> str:
    token = account_id.lower().replace("-", "")
    return f"ref:credential.{token}"


def registration_id(provider_model_id: str, role: str) -> str:
    return str(uuid.uuid5(_MODEL_NAMESPACE, f"{role}:{provider_model_id}"))

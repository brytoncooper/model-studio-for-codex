from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from model_deck.integrations.hosts.codex.migration_preview.managed_agent import (
    _is_cursor_url,
    _validate_base_url,
)

WIRE_FORMATS = frozenset({"auto", "responses", "chat", "cursor"})
PREFERENCES_TOP_LEVEL_KEYS = frozenset(
    {"accounts", "models", "selectedAccount", "selectedModel", "version", "schema_version"}
)
ACCOUNT_KEYS = frozenset({"id", "name", "baseURL", "wire", "hasKey"})
ENDPOINT_ENTRY_KEYS = frozenset({"name", "base_url", "wire"})


@dataclass(frozen=True, slots=True)
class PreferencesView:
    accounts: dict[str, dict[str, Any]]
    models: frozenset[str]
    duplicate_account_ids: frozenset[str]


def _valid_uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    if str(parsed) != value.lower():
        return None
    return value.lower()


def _valid_printable_bounded(value: object, *, max_len: int) -> bool:
    if not isinstance(value, str) or not 0 < len(value) <= max_len:
        return False
    return not any(character.isspace() and character not in " \t" or ord(character) < 32 for character in value)


def _valid_account_name(value: object) -> bool:
    if not isinstance(value, str) or not 0 < len(value) <= 64:
        return False
    return not any(ord(character) < 32 for character in value)


def _valid_model_string(value: object) -> bool:
    if not isinstance(value, str) or not 0 < len(value) <= 256:
        return False
    return not any(character.isspace() or ord(character) < 32 for character in value)


def resolved_endpoint_wire(wire: str | None, base_url: str | None) -> str:
    if base_url is not None and _is_cursor_url(base_url):
        return "cursor"
    if isinstance(wire, str) and wire in {"responses", "chat", "cursor"}:
        return wire
    return "auto"


def _endpoint_map_key(key: object) -> str | None:
    if not isinstance(key, str) or not key.strip():
        return None
    account_id = _valid_uuid(key)
    if account_id is not None:
        return account_id
    base_url = _validate_base_url(key)
    if base_url is not None:
        return base_url.rstrip("/")
    return None


def validate_preferences_document(document: object) -> tuple[PreferencesView | None, str | None]:
    if not isinstance(document, dict):
        return None, "preferences shape invalid"
    if not set(document) <= PREFERENCES_TOP_LEVEL_KEYS:
        return None, "preferences unexpected field"
    accounts_raw = document.get("accounts")
    if not isinstance(accounts_raw, list):
        return None, "preferences accounts invalid"
    models_raw = document.get("models")
    if not isinstance(models_raw, list) or not all(_valid_model_string(item) for item in models_raw):
        return None, "preferences models invalid"
    seen_ids: list[str] = []
    accounts: dict[str, dict[str, Any]] = {}
    for item in accounts_raw:
        if not isinstance(item, dict):
            return None, "preferences account entry invalid"
        if not set(item) <= ACCOUNT_KEYS:
            return None, "preferences account unexpected field"
        account_id = _valid_uuid(item.get("id"))
        if account_id is None:
            return None, "preferences account id invalid"
        if not _valid_account_name(item.get("name")):
            return None, "preferences account name invalid"
        has_key = item.get("hasKey")
        if has_key is not None and not isinstance(has_key, bool):
            return None, "preferences account hasKey invalid"
        base_url = item.get("baseURL")
        if base_url is not None and _validate_base_url(base_url) is None:
            return None, "preferences account baseURL invalid"
        wire = item.get("wire")
        if wire is not None and (not isinstance(wire, str) or wire not in WIRE_FORMATS):
            return None, "preferences account wire invalid"
        seen_ids.append(account_id)
        accounts[account_id] = item
    duplicates = {account_id for account_id in seen_ids if seen_ids.count(account_id) > 1}
    selected = document.get("selectedAccount", "")
    if selected != "" and _valid_uuid(selected) is None:
        return None, "preferences selectedAccount invalid"
    selected_model = document.get("selectedModel", "openai/gpt-6-astra")
    if not _valid_model_string(selected_model):
        return None, "preferences selectedModel invalid"
    return (
        PreferencesView(
            accounts=accounts,
            models=frozenset(models_raw),
            duplicate_account_ids=frozenset(duplicates),
        ),
        None,
    )


def validate_endpoints_document(document: object) -> tuple[dict[str, dict[str, Any]] | None, str | None]:
    if not isinstance(document, dict):
        return None, "endpoints shape invalid"
    parsed: dict[str, dict[str, Any]] = {}
    for key, value in document.items():
        canonical_key = _endpoint_map_key(key)
        if canonical_key is None:
            return None, "endpoints key invalid"
        if not isinstance(value, dict):
            return None, "endpoints entry invalid"
        if not set(value) <= ENDPOINT_ENTRY_KEYS:
            return None, "endpoints entry unexpected field"
        if "name" in value and not isinstance(value.get("name"), str):
            return None, "endpoints name invalid"
        if "wire" in value:
            wire = value.get("wire")
            if not isinstance(wire, str) or wire not in WIRE_FORMATS:
                return None, "endpoints wire invalid"
        if "base_url" in value and _validate_base_url(value.get("base_url")) is None:
            return None, "endpoints base_url invalid"
        parsed[canonical_key] = value
    return parsed, None


def validate_display_names_document(document: object) -> tuple[dict[str, str] | None, str | None]:
    if not isinstance(document, dict):
        return None, "display-names shape invalid"
    parsed: dict[str, str] = {}
    for key, value in document.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return None, "display-names entry invalid"
        parsed[key] = value
    return parsed, None

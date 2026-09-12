from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from model_deck.integrations.hosts.codex.migration_preview.constants import SECRET_FIELD_NAMES

_SECRET_FIELD_BY_COMPACT_NAME = {
    "".join(character for character in field if character.isalnum()): field
    for field in SECRET_FIELD_NAMES
}


def normalize_field_name(name: object) -> str | None:
    if not isinstance(name, str):
        return None
    compact = "".join(character for character in name.lower() if character.isalnum())
    if not compact:
        return None
    return _SECRET_FIELD_BY_COMPACT_NAME.get(compact)


def find_secret_field_names(document: Any) -> tuple[str, ...]:
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                secret = normalize_field_name(key)
                if secret is not None:
                    found.add(secret)
                else:
                    walk(nested)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                walk(item)

    walk(document)
    return tuple(sorted(found))

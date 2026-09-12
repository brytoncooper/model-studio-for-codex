from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from typing import Any

from model_deck.engine.connections.ports import (
    ConnectionRecord,
    ConnectionRepository,
    SaveConnectionCommand,
)

_PROVIDER_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9-]*)+$")
_OPAQUE_REF_PATTERN = re.compile(r"^ref:[a-z][a-z0-9._-]{0,120}$")
_MAX_IDEMPOTENCY_KEY = 128
_MAX_OPAQUE_REF = 128
_MIN_PROVIDER_ID = 3
_MAX_PROVIDER_ID = 256

def _reject_unknown_keys(
    mapping: Mapping[str, Any],
    allowed: frozenset[str],
    *,
    context: str,
) -> None:
    unknown = set(mapping.keys()) - allowed
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"unknown {context} field(s): {names}")


def _canonical_uuid_spelling(value: str) -> bool:
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return str(parsed).casefold() == value.casefold()


def _validate_uuid_field(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    if not _canonical_uuid_spelling(value):
        raise ValueError(f"{name} must be a UUID")
    return value


def _validate_expected_revision(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("expected_revision must be an integer")
    if value < 0:
        raise ValueError("expected_revision must be >= 0")
    return value


def _validate_idempotency_key(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("idempotency_key must be a non-empty string")
    if len(value) > _MAX_IDEMPOTENCY_KEY:
        raise ValueError("idempotency_key must be at most 128 characters")
    return value


def _validate_provider_id(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("provider_id must be a non-empty string")
    if len(value) < _MIN_PROVIDER_ID or len(value) > _MAX_PROVIDER_ID:
        raise ValueError("provider_id must be between 3 and 256 characters")
    if not _PROVIDER_ID_PATTERN.fullmatch(value):
        raise ValueError("provider_id must be a reverse-domain identifier")
    return value


def _validate_opaque_ref(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    if len(value) > _MAX_OPAQUE_REF:
        raise ValueError(f"{name} must be at most 128 characters")
    if _OPAQUE_REF_PATTERN.fullmatch(value):
        return value
    if _canonical_uuid_spelling(value):
        return value
    raise ValueError(f"{name} must be a UUID or ref: opaque reference")


def _optional_opaque_ref(name: str, connection: Mapping[str, Any]) -> str | None:
    if name not in connection:
        return None
    return _validate_opaque_ref(name, connection[name])


def _connection_payload(record: ConnectionRecord) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "connection_id": record.connection_id,
        "provider_id": record.provider_id,
        "revision": record.revision,
    }
    if record.endpoint_config_ref is not None:
        payload["endpoint_config_ref"] = record.endpoint_config_ref
    if record.credential_ref is not None:
        payload["credential_ref"] = record.credential_ref
    return payload


class ListConnectionsUseCase:
    def __init__(self, repository: ConnectionRepository) -> None:
        self._repository = repository

    def execute(self, params: Mapping[str, Any] | None) -> dict[str, Any]:
        params = dict(params or {})
        _reject_unknown_keys(params, frozenset(), context="params")
        connections = [
            _connection_payload(row) for row in self._repository.list_connections()
        ]
        return {"connections": connections}


class SaveConnectionUseCase:
    def __init__(self, repository: ConnectionRepository) -> None:
        self._repository = repository

    def execute(self, params: Mapping[str, Any] | None) -> dict[str, Any]:
        params = dict(params or {})
        allowed_top = frozenset({"connection", "expected_revision", "idempotency_key"})
        _reject_unknown_keys(params, allowed_top, context="params")
        connection_raw = params.get("connection")
        if not isinstance(connection_raw, Mapping):
            raise ValueError("connection must be an object")
        connection = dict(connection_raw)
        allowed_connection = frozenset(
            {
                "connection_id",
                "provider_id",
                "endpoint_config_ref",
                "credential_ref",
            }
        )
        _reject_unknown_keys(connection, allowed_connection, context="connection")
        command = SaveConnectionCommand(
            connection_id=_validate_uuid_field(
                "connection_id", connection.get("connection_id")
            ),
            provider_id=_validate_provider_id(connection.get("provider_id")),
            endpoint_config_ref=_optional_opaque_ref("endpoint_config_ref", connection),
            credential_ref=_optional_opaque_ref("credential_ref", connection),
            expected_revision=_validate_expected_revision(params.get("expected_revision")),
            idempotency_key=_validate_idempotency_key(params.get("idempotency_key")),
        )
        record = self._repository.save(command)
        return {"connection": _connection_payload(record)}

from __future__ import annotations

import json
import sqlite3
from typing import Any

AGGREGATE_TYPE_REGISTERED_MODEL = "registered_model"
AGGREGATE_TYPE_CONNECTION = "connection"

EVENT_REGISTERED_MODEL_UPSERTED = "registered_model.upserted"
EVENT_REGISTERED_MODEL_REMOVED = "registered_model.removed"
EVENT_CONNECTION_SAVED = "connection.saved"

OUTBOX_STATE_PENDING = "pending"

_PROJECTION_OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS projection_outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    aggregate_revision INTEGER NOT NULL CHECK (aggregate_revision >= 1),
    event_kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'applied', 'conflict')),
    UNIQUE (aggregate_type, aggregate_id, aggregate_revision, event_kind)
);
"""


def ensure_projection_outbox_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_PROJECTION_OUTBOX_SCHEMA)


def canonical_payload_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def enqueue_registered_model_upserted(
    conn: sqlite3.Connection,
    *,
    registration_id: str,
    revision: int,
    provider_model_id: str,
    connection_id: str,
    display_name: str,
) -> None:
    payload_json = canonical_payload_json(
        {
            "connection_id": connection_id,
            "display_name": display_name,
            "provider_model_id": provider_model_id,
            "registration_id": registration_id,
            "revision": revision,
        }
    )
    _insert_outbox_event(
        conn,
        aggregate_type=AGGREGATE_TYPE_REGISTERED_MODEL,
        aggregate_id=registration_id,
        aggregate_revision=revision,
        event_kind=EVENT_REGISTERED_MODEL_UPSERTED,
        payload_json=payload_json,
    )


def enqueue_registered_model_removed(
    conn: sqlite3.Connection,
    *,
    registration_id: str,
    revision: int,
) -> None:
    payload_json = canonical_payload_json(
        {
            "registration_id": registration_id,
            "removed": True,
            "revision": revision,
        }
    )
    _insert_outbox_event(
        conn,
        aggregate_type=AGGREGATE_TYPE_REGISTERED_MODEL,
        aggregate_id=registration_id,
        aggregate_revision=revision,
        event_kind=EVENT_REGISTERED_MODEL_REMOVED,
        payload_json=payload_json,
    )


def enqueue_connection_saved(
    conn: sqlite3.Connection,
    *,
    connection_id: str,
    revision: int,
    provider_id: str,
    endpoint_config_ref: str | None,
    credential_ref: str | None,
) -> None:
    payload_json = canonical_payload_json(
        {
            "connection_id": connection_id,
            "credential_ref": credential_ref,
            "endpoint_config_ref": endpoint_config_ref,
            "provider_id": provider_id,
            "revision": revision,
        }
    )
    _insert_outbox_event(
        conn,
        aggregate_type=AGGREGATE_TYPE_CONNECTION,
        aggregate_id=connection_id,
        aggregate_revision=revision,
        event_kind=EVENT_CONNECTION_SAVED,
        payload_json=payload_json,
    )


def _insert_outbox_event(
    conn: sqlite3.Connection,
    *,
    aggregate_type: str,
    aggregate_id: str,
    aggregate_revision: int,
    event_kind: str,
    payload_json: str,
) -> None:
    conn.execute(
        "INSERT INTO projection_outbox "
        "(aggregate_type, aggregate_id, aggregate_revision, event_kind, payload_json, state) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            aggregate_type,
            aggregate_id,
            aggregate_revision,
            event_kind,
            payload_json,
            OUTBOX_STATE_PENDING,
        ),
    )

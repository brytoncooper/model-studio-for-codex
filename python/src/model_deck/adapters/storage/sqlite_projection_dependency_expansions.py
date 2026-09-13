"""Durable evidence that a connection event expanded into model invalidations."""
import json
import sqlite3
from collections.abc import Sequence

from model_deck.engine.model_library.ports import RegisteredModelRecord


def ensure_dependency_expansions_schema(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        raise ValueError("dependency schema initialization requires no active transaction")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS projection_dependency_expansions (
            outbox_id INTEGER PRIMARY KEY REFERENCES projection_outbox(outbox_id),
            connection_id TEXT NOT NULL,
            connection_revision INTEGER NOT NULL CHECK (connection_revision >= 1),
            payload_json TEXT NOT NULL,
            expanded_models_json TEXT NOT NULL
        );
    """)


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def record_connection_expansion(
    connection: sqlite3.Connection, *, outbox_id: int,
    expanded_models: Sequence[RegisteredModelRecord],
) -> None:
    """Bind the original connection row to its exact newly queued model upserts.

    Caller owns the transaction and must supply every model returned by its
    atomic invalidation step. This receipt records expansion, never file apply.
    """
    if not connection.in_transaction:
        raise ValueError("dependency expansion requires an active transaction")
    row = connection.execute(
        "SELECT aggregate_type, aggregate_id, aggregate_revision, event_kind, payload_json, state "
        "FROM projection_outbox WHERE outbox_id = ?", (outbox_id,),
    ).fetchone()
    if row is None or row[0] != "connection" or row[3] != "connection.saved" or row[5] != "pending":
        raise ValueError("dependency source event rejected")
    payload = json.loads(row[4])
    keys = {"connection_id", "credential_ref", "endpoint_config_ref", "provider_id", "revision"}
    if (type(payload) is not dict or set(payload) != keys or _canonical(payload) != row[4]
            or payload["connection_id"] != row[1] or type(payload["revision"]) is not int
            or payload["revision"] != row[2]):
        raise ValueError("dependency source binding rejected")
    models = sorted(expanded_models, key=lambda model: model.registration_id)
    if len({model.registration_id for model in models}) != len(models):
        raise ValueError("duplicate expanded model")
    evidence = []
    for model in models:
        if model.connection_id != row[1]:
            raise ValueError("foreign expanded model")
        expected = _canonical({"connection_id": model.connection_id, "display_name": model.display_name,
            "provider_model_id": model.provider_model_id, "registration_id": model.registration_id,
            "revision": model.revision})
        found = connection.execute(
            "SELECT 1 FROM projection_outbox WHERE outbox_id > ? AND aggregate_type = 'registered_model' "
            "AND aggregate_id = ? AND aggregate_revision = ? AND event_kind = 'registered_model.upserted' "
            "AND payload_json = ? AND state = 'pending'",
            (outbox_id, model.registration_id, model.revision, expected),
        ).fetchone()
        if found is None:
            raise ValueError("expanded model event missing")
        evidence.append({"registration_id": model.registration_id, "revision": model.revision})
    values = (row[1], row[2], row[4], _canonical(evidence))
    prior = connection.execute(
        "SELECT connection_id, connection_revision, payload_json, expanded_models_json "
        "FROM projection_dependency_expansions WHERE outbox_id = ?", (outbox_id,),
    ).fetchone()
    if prior is not None:
        if prior != values:
            raise ValueError("dependency expansion receipt conflict")
        return
    connection.execute(
        "INSERT INTO projection_dependency_expansions "
        "(outbox_id, connection_id, connection_revision, payload_json, expanded_models_json) VALUES (?, ?, ?, ?, ?)",
        (outbox_id, *values),
    )

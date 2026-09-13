"""Invalidate connection-dependent model projections inside the caller transaction."""
import sqlite3

from model_deck.adapters.storage.sqlite_outbox import enqueue_registered_model_upserted
from model_deck.engine.model_library.ports import RegisteredModelRecord


def invalidate_connection_models(
    connection: sqlite3.Connection, *, connection_id: str,
) -> tuple[RegisteredModelRecord, ...]:
    """Advance active model revisions and enqueue their normal upserts atomically.

    The owner must initialize schemas, acquire BEGIN IMMEDIATE, and commit or
    roll back its entire unit. This function opens no connection and never
    commits, initializes schemas, or changes inactive registrations.
    """
    if not connection.in_transaction:
        raise ValueError("model invalidation requires an active transaction")
    rows = connection.execute(
        "SELECT registration_id, provider_model_id, connection_id, display_name, revision "
        "FROM registered_models WHERE connection_id = ? AND active = 1 ORDER BY registration_id",
        (connection_id,),
    ).fetchall()
    changed = []
    for registration_id, provider_model_id, owner, display_name, revision in rows:
        updated = connection.execute(
            "UPDATE registered_models SET revision = revision + 1 "
            "WHERE registration_id = ? AND revision = ? AND active = 1",
            (registration_id, revision),
        )
        if updated.rowcount != 1:
            raise RuntimeError("model invalidation lost transaction ownership")
        record = RegisteredModelRecord(registration_id, provider_model_id, owner, display_name, revision + 1)
        enqueue_registered_model_upserted(
            connection, registration_id=record.registration_id, revision=record.revision,
            provider_model_id=record.provider_model_id, connection_id=record.connection_id,
            display_name=record.display_name,
        )
        changed.append(record)
    return tuple(changed)

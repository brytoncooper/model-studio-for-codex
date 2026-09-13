"""Shared model storage schema, initialized before any mutation transaction."""
import sqlite3


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS registered_models (
    registration_id TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL,
    provider_model_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    revision INTEGER NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_registered_models_active_pair
    ON registered_models (connection_id, provider_model_id)
    WHERE active = 1;
CREATE TABLE IF NOT EXISTS model_idempotency (
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_payload TEXT NOT NULL,
    result_payload TEXT NOT NULL,
    PRIMARY KEY (operation, idempotency_key)
);
"""


def ensure_model_schema(connection: sqlite3.Connection) -> None:
    """Initialize existing table shapes; never implicitly commit a caller transaction."""
    if connection.in_transaction:
        raise ValueError("model schema initialization requires no active transaction")
    connection.executescript(_SCHEMA_SQL)

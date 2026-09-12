from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from model_deck.adapters.storage.sqlite_outbox import (
    ensure_projection_outbox_schema,
    enqueue_connection_saved,
)
from model_deck.engine.connections.ports import (
    ConnectionIdempotencyConflictError,
    ConnectionRecord,
    ConnectionRevisionConflictError,
    SaveConnectionCommand,
)

_OPERATION_SAVE = "save"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS connections (
    connection_id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    endpoint_config_ref TEXT,
    credential_ref TEXT,
    revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS connection_idempotency (
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_payload TEXT NOT NULL,
    result_payload TEXT NOT NULL,
    PRIMARY KEY (operation, idempotency_key)
);
"""


class SQLiteConnectionRepository:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def list_connections(self) -> list[ConnectionRecord]:
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            rows = conn.execute(
                "SELECT connection_id, provider_id, endpoint_config_ref, credential_ref, revision "
                "FROM connections ORDER BY provider_id, connection_id"
            ).fetchall()
            return [_row_to_record(row) for row in rows]
        finally:
            conn.close()

    def save(self, command: SaveConnectionCommand) -> ConnectionRecord:
        conn = self._connect()
        request_payload = _canonical_save_request(command)
        try:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT request_payload, result_payload FROM connection_idempotency "
                    "WHERE operation = ? AND idempotency_key = ?",
                    (_OPERATION_SAVE, command.idempotency_key),
                ).fetchone()
                if existing is not None:
                    stored_request, stored_result = existing
                    if stored_request != request_payload:
                        raise ConnectionIdempotencyConflictError(
                            "idempotency key reused with different request payload"
                        )
                    conn.commit()
                    return _deserialize_result(stored_result)
                record = self._save_mutation(conn, command)
                enqueue_connection_saved(
                    conn,
                    connection_id=record.connection_id,
                    revision=record.revision,
                    provider_id=record.provider_id,
                    endpoint_config_ref=record.endpoint_config_ref,
                    credential_ref=record.credential_ref,
                )
                result_payload = _serialize_result(record)
                conn.execute(
                    "INSERT INTO connection_idempotency "
                    "(operation, idempotency_key, request_payload, result_payload) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        _OPERATION_SAVE,
                        command.idempotency_key,
                        request_payload,
                        result_payload,
                    ),
                )
                conn.commit()
                return record
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def _save_mutation(
        self, conn: sqlite3.Connection, command: SaveConnectionCommand
    ) -> ConnectionRecord:
        row = conn.execute(
            "SELECT provider_id, endpoint_config_ref, credential_ref, revision "
            "FROM connections WHERE connection_id = ?",
            (command.connection_id,),
        ).fetchone()
        if row is None:
            if command.expected_revision != 0:
                raise ConnectionRevisionConflictError("connection not found")
            record = ConnectionRecord(
                connection_id=command.connection_id,
                provider_id=command.provider_id,
                revision=1,
                endpoint_config_ref=command.endpoint_config_ref,
                credential_ref=command.credential_ref,
            )
            conn.execute(
                "INSERT INTO connections "
                "(connection_id, provider_id, endpoint_config_ref, credential_ref, revision) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    record.connection_id,
                    record.provider_id,
                    record.endpoint_config_ref,
                    record.credential_ref,
                    record.revision,
                ),
            )
            return record
        current_revision = row[3]
        if command.expected_revision == 0:
            raise ConnectionRevisionConflictError("connection already exists")
        if current_revision != command.expected_revision:
            raise ConnectionRevisionConflictError("stale revision")
        new_revision = current_revision + 1
        conn.execute(
            "UPDATE connections SET provider_id = ?, endpoint_config_ref = ?, "
            "credential_ref = ?, revision = ? WHERE connection_id = ?",
            (
                command.provider_id,
                command.endpoint_config_ref,
                command.credential_ref,
                new_revision,
                command.connection_id,
            ),
        )
        return ConnectionRecord(
            connection_id=command.connection_id,
            provider_id=command.provider_id,
            revision=new_revision,
            endpoint_config_ref=command.endpoint_config_ref,
            credential_ref=command.credential_ref,
        )

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(_SCHEMA_SQL)
        ensure_projection_outbox_schema(conn)


def _row_to_record(row: tuple[Any, ...]) -> ConnectionRecord:
    return ConnectionRecord(
        connection_id=row[0],
        provider_id=row[1],
        revision=row[4],
        endpoint_config_ref=row[2],
        credential_ref=row[3],
    )


def _canonical_save_request(command: SaveConnectionCommand) -> str:
    return _canonical_json(
        {
            "connection_id": command.connection_id,
            "provider_id": command.provider_id,
            "expected_revision": command.expected_revision,
            "endpoint_config_ref": command.endpoint_config_ref,
            "credential_ref": command.credential_ref,
        }
    )


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _serialize_result(record: ConnectionRecord) -> str:
    return _canonical_json(
        {
            "connection_id": record.connection_id,
            "provider_id": record.provider_id,
            "revision": record.revision,
            "endpoint_config_ref": record.endpoint_config_ref,
            "credential_ref": record.credential_ref,
        }
    )


def _deserialize_result(payload: str) -> ConnectionRecord:
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("invalid stored connection result")
    return ConnectionRecord(
        connection_id=data["connection_id"],
        provider_id=data["provider_id"],
        revision=data["revision"],
        endpoint_config_ref=data.get("endpoint_config_ref"),
        credential_ref=data.get("credential_ref"),
    )

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from model_deck.adapters.storage.sqlite_model_schema import ensure_model_schema

from model_deck.adapters.storage.sqlite_outbox import (
    ensure_projection_outbox_schema,
    enqueue_registered_model_removed,
    enqueue_registered_model_upserted,
)
from model_deck.engine.model_library.ports import (
    ModelIdempotencyConflictError,
    ModelRegistrationNotFoundError,
    ModelRevisionConflictError,
    RegisterModelCommand,
    RegisteredModelRecord,
    RemoveModelCommand,
    RenameModelCommand,
)

_OPERATION_REGISTER = "register"
_OPERATION_RENAME = "rename"
_OPERATION_REMOVE = "remove"

class SQLiteModelRepository:
    def __init__(
        self,
        db_path: Path,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._db_path = db_path
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))

    def list_registered(self, *, connection_id: str | None = None) -> list[RegisteredModelRecord]:
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            query = (
                "SELECT registration_id, provider_model_id, connection_id, display_name, revision "
                "FROM registered_models WHERE active = 1"
            )
            params: tuple[Any, ...] = ()
            if connection_id is not None:
                query += " AND connection_id = ?"
                params = (connection_id,)
            query += " ORDER BY connection_id, provider_model_id, registration_id"
            rows = conn.execute(query, params).fetchall()
            return [
                RegisteredModelRecord(
                    registration_id=row[0],
                    provider_model_id=row[1],
                    connection_id=row[2],
                    display_name=row[3],
                    revision=row[4],
                )
                for row in rows
            ]
        finally:
            conn.close()

    def register(self, command: RegisterModelCommand) -> RegisteredModelRecord:
        return self._run_mutation(
            operation=_OPERATION_REGISTER,
            idempotency_key=command.idempotency_key,
            request_payload=_canonical_register_request(command),
            mutate=lambda conn: self._register_mutation(conn, command),
        )

    def rename(self, command: RenameModelCommand) -> RegisteredModelRecord:
        return self._run_mutation(
            operation=_OPERATION_RENAME,
            idempotency_key=command.idempotency_key,
            request_payload=_canonical_rename_request(command),
            mutate=lambda conn: self._rename_mutation(conn, command),
        )

    def remove(self, command: RemoveModelCommand) -> bool:
        return self._run_mutation(
            operation=_OPERATION_REMOVE,
            idempotency_key=command.idempotency_key,
            request_payload=_canonical_remove_request(command),
            mutate=lambda conn: self._remove_mutation(conn, command),
        )

    def _run_mutation(
        self,
        *,
        operation: str,
        idempotency_key: str,
        request_payload: str,
        mutate: Callable[[sqlite3.Connection], RegisteredModelRecord | bool],
    ) -> RegisteredModelRecord | bool:
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT request_payload, result_payload FROM model_idempotency "
                    "WHERE operation = ? AND idempotency_key = ?",
                    (operation, idempotency_key),
                ).fetchone()
                if existing is not None:
                    stored_request, stored_result = existing
                    if stored_request != request_payload:
                        raise ModelIdempotencyConflictError(
                            "idempotency key reused with different request payload"
                        )
                    conn.commit()
                    return _deserialize_result(operation, stored_result)
                result = mutate(conn)
                self._enqueue_model_projection(conn, operation, result)
                result_payload = _serialize_result(operation, result)
                conn.execute(
                    "INSERT INTO model_idempotency (operation, idempotency_key, request_payload, result_payload) "
                    "VALUES (?, ?, ?, ?)",
                    (operation, idempotency_key, request_payload, result_payload),
                )
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def _enqueue_model_projection(
        self,
        conn: sqlite3.Connection,
        operation: str,
        result: RegisteredModelRecord | bool,
    ) -> None:
        if operation == _OPERATION_REMOVE:
            if not isinstance(result, bool):
                raise TypeError("remove result must be bool")
            return
        if not isinstance(result, RegisteredModelRecord):
            raise TypeError("mutation result must be RegisteredModelRecord")
        enqueue_registered_model_upserted(
            conn,
            registration_id=result.registration_id,
            revision=result.revision,
            provider_model_id=result.provider_model_id,
            connection_id=result.connection_id,
            display_name=result.display_name,
        )

    def _register_mutation(
        self, conn: sqlite3.Connection, command: RegisterModelCommand
    ) -> RegisteredModelRecord:
        if command.expected_revision != 0:
            raise ModelRevisionConflictError("register requires expected_revision 0")
        duplicate = conn.execute(
            "SELECT 1 FROM registered_models "
            "WHERE connection_id = ? AND provider_model_id = ? AND active = 1",
            (command.connection_id, command.provider_model_id),
        ).fetchone()
        if duplicate is not None:
            raise ModelRevisionConflictError("active model already registered for connection")
        registration_id = self._id_factory()
        record = RegisteredModelRecord(
            registration_id=registration_id,
            provider_model_id=command.provider_model_id,
            connection_id=command.connection_id,
            display_name=command.display_name,
            revision=1,
        )
        try:
            conn.execute(
                "INSERT INTO registered_models "
                "(registration_id, connection_id, provider_model_id, display_name, revision, active) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (
                    record.registration_id,
                    record.connection_id,
                    record.provider_model_id,
                    record.display_name,
                    record.revision,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ModelRevisionConflictError("active model already registered for connection") from exc
        return record

    def _rename_mutation(
        self, conn: sqlite3.Connection, command: RenameModelCommand
    ) -> RegisteredModelRecord:
        row = self._load_active_row(conn, command.registration_id)
        if row is None:
            raise ModelRegistrationNotFoundError("registration not found")
        current_revision = row[4]
        if current_revision != command.expected_revision:
            raise ModelRevisionConflictError("stale revision")
        new_revision = current_revision + 1
        conn.execute(
            "UPDATE registered_models SET display_name = ?, revision = ? WHERE registration_id = ?",
            (command.display_name, new_revision, command.registration_id),
        )
        return RegisteredModelRecord(
            registration_id=row[0],
            provider_model_id=row[1],
            connection_id=row[2],
            display_name=command.display_name,
            revision=new_revision,
        )

    def _remove_mutation(self, conn: sqlite3.Connection, command: RemoveModelCommand) -> bool:
        row = self._load_active_row(conn, command.registration_id)
        if row is None:
            raise ModelRegistrationNotFoundError("registration not found")
        current_revision = row[4]
        if current_revision != command.expected_revision:
            raise ModelRevisionConflictError("stale revision")
        new_revision = current_revision + 1
        conn.execute(
            "UPDATE registered_models SET revision = ?, active = 0 WHERE registration_id = ?",
            (new_revision, command.registration_id),
        )
        enqueue_registered_model_removed(
            conn,
            registration_id=command.registration_id,
            revision=new_revision,
        )
        return True

    def _load_active_row(
        self, conn: sqlite3.Connection, registration_id: str
    ) -> tuple[str, str, str, str, int] | None:
        row = conn.execute(
            "SELECT registration_id, provider_model_id, connection_id, display_name, revision "
            "FROM registered_models WHERE registration_id = ? AND active = 1",
            (registration_id,),
        ).fetchone()
        if row is None:
            return None
        return (row[0], row[1], row[2], row[3], row[4])

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        ensure_model_schema(conn)
        ensure_projection_outbox_schema(conn)


def _canonical_register_request(command: RegisterModelCommand) -> str:
    return _canonical_json(
        {
            "connection_id": command.connection_id,
            "provider_model_id": command.provider_model_id,
            "display_name": command.display_name,
            "expected_revision": command.expected_revision,
        }
    )


def _canonical_rename_request(command: RenameModelCommand) -> str:
    return _canonical_json(
        {
            "registration_id": command.registration_id,
            "display_name": command.display_name,
            "expected_revision": command.expected_revision,
        }
    )


def _canonical_remove_request(command: RemoveModelCommand) -> str:
    return _canonical_json(
        {
            "registration_id": command.registration_id,
            "expected_revision": command.expected_revision,
        }
    )


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _serialize_result(operation: str, result: RegisteredModelRecord | bool) -> str:
    if operation == _OPERATION_REMOVE:
        if not isinstance(result, bool):
            raise TypeError("remove result must be bool")
        return _canonical_json({"removed": result})
    if not isinstance(result, RegisteredModelRecord):
        raise TypeError("mutation result must be RegisteredModelRecord")
    return _canonical_json(
        {
            "registration_id": result.registration_id,
            "provider_model_id": result.provider_model_id,
            "connection_id": result.connection_id,
            "display_name": result.display_name,
            "revision": result.revision,
        }
    )


def _deserialize_result(operation: str, payload: str) -> RegisteredModelRecord | bool:
    data = json.loads(payload)
    if operation == _OPERATION_REMOVE:
        if not isinstance(data, dict) or "removed" not in data or not isinstance(data["removed"], bool):
            raise ValueError("invalid stored remove result")
        return data["removed"]
    if not isinstance(data, dict):
        raise ValueError("invalid stored model result")
    return RegisteredModelRecord(
        registration_id=data["registration_id"],
        provider_model_id=data["provider_model_id"],
        connection_id=data["connection_id"],
        display_name=data["display_name"],
        revision=data["revision"],
    )

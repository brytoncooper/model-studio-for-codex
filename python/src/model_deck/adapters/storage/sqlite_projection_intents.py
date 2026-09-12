from __future__ import annotations

import sqlite3
from pathlib import Path

from model_deck.engine.projections.intents import (
    OPERATION_DELETE,
    OPERATION_WRITE,
    ProjectionMutationIntent,
    ProjectionMutationIntentJournal,
    compute_payload_sha256,
    validate_operation,
    validate_outbox_id,
)
from model_deck.engine.projections.ports import ProjectionOutboxEvent
from model_deck.engine.projections.receipts import (
    ProjectionOutboxEventMismatchError,
    ProjectionOutboxStateError,
    ProjectionReceiptConflictError,
    events_match_row,
    validate_artifact_ref,
    validate_consumer_id,
    validate_output_sha256,
    validate_projection_event,
)

_INTENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS projection_mutation_intents (
    outbox_id INTEGER NOT NULL,
    consumer_id TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    aggregate_revision INTEGER NOT NULL,
    event_kind TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    operation TEXT NOT NULL,
    artifact_ref TEXT NOT NULL,
    expected_sha256 TEXT,
    desired_sha256 TEXT,
    PRIMARY KEY (outbox_id, consumer_id)
);
"""

_EXISTING_INTENT_QUERY = (
    "SELECT aggregate_type, aggregate_id, aggregate_revision, event_kind, "
    "payload_sha256, operation, artifact_ref, expected_sha256, desired_sha256 "
    "FROM projection_mutation_intents WHERE outbox_id = ? AND consumer_id = ?"
)

_OUTBOX_ROW_QUERY = (
    "SELECT outbox_id, aggregate_type, aggregate_id, aggregate_revision, "
    "event_kind, payload_json, state FROM projection_outbox WHERE outbox_id = ?"
)

_INTENT_SCHEMA_EXISTS_QUERY = (
    "SELECT 1 FROM sqlite_master "
    "WHERE type = 'table' AND name = 'projection_mutation_intents'"
)

_INSERT_INTENT = (
    "INSERT INTO projection_mutation_intents "
    "(outbox_id, consumer_id, aggregate_type, aggregate_id, "
    "aggregate_revision, event_kind, payload_sha256, operation, "
    "artifact_ref, expected_sha256, desired_sha256) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def ensure_projection_intent_schema(conn: sqlite3.Connection) -> None:
    """Ensure the additive projection_mutation_intents table exists.

    Requires an existing ``projection_outbox`` table; raises
    ``ProjectionOutboxStateError`` if absent so the additive schema is never
    installed before its producer schema.
    """
    outbox_exists = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name = 'projection_outbox'"
    ).fetchone()
    if outbox_exists is None:
        raise ProjectionOutboxStateError("projection_outbox schema is required")
    conn.executescript(_INTENT_SCHEMA)


def _intent_from_row(
    outbox_id: int,
    consumer_id: str,
    row: tuple[object, ...],
) -> ProjectionMutationIntent:
    return ProjectionMutationIntent(
        outbox_id=outbox_id,
        consumer_id=consumer_id,
        aggregate_type=row[0],
        aggregate_id=row[1],
        aggregate_revision=row[2],
        event_kind=row[3],
        payload_sha256=row[4],
        operation=row[5],
        artifact_ref=row[6],
        expected_sha256=row[7],
        desired_sha256=row[8],
    )


class SQLiteProjectionMutationIntentJournal:
    """SQLite implementation of :class:`ProjectionMutationIntentJournal`.

    Audit-only: never modifies ``projection_outbox``. Every call verifies
    that the supplied event matches the persisted outbox row before checking
    any recorded intent. Replay of an existing exact-match intent succeeds
    regardless of the current outbox state; a new intent requires the outbox
    row to be ``pending``. Changed parameters raise
    ``ProjectionReceiptConflictError``.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)

    def get_intent(
        self, *, outbox_id: int, consumer_id: str
    ) -> ProjectionMutationIntent | None:
        outbox_id = validate_outbox_id(outbox_id)
        consumer_id = validate_consumer_id(consumer_id)
        db_path = self._db_path
        if not db_path.is_file():
            raise FileNotFoundError(f"intent database not found: {db_path}")
        uri = db_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            schema_exists = conn.execute(_INTENT_SCHEMA_EXISTS_QUERY).fetchone()
            if schema_exists is None:
                return None
            row = conn.execute(
                _EXISTING_INTENT_QUERY, (outbox_id, consumer_id)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return _intent_from_row(outbox_id, consumer_id, row)

    def record_intent(
        self,
        event: ProjectionOutboxEvent,
        *,
        consumer_id: str,
        operation: str,
        artifact_ref: str,
        expected_sha256: str | None,
        desired_sha256: str | None,
    ) -> ProjectionMutationIntent:
        validate_projection_event(event)
        consumer_id = validate_consumer_id(consumer_id)
        operation = validate_operation(operation)
        artifact_ref = validate_artifact_ref(artifact_ref)

        if operation == OPERATION_WRITE:
            if desired_sha256 is None:
                raise ValueError("write operation requires desired_sha256")
            validated_desired: str | None = validate_output_sha256(desired_sha256)
        else:  # OPERATION_DELETE
            if desired_sha256 is not None:
                raise ValueError("delete operation requires desired_sha256 to be None")
            validated_desired = None

        if expected_sha256 is not None:
            validate_output_sha256(expected_sha256)
        validated_expected = expected_sha256

        outbox_id = event.outbox_id
        payload_sha256 = compute_payload_sha256(event)

        db_path = self._db_path
        if not db_path.is_file():
            raise FileNotFoundError(f"intent database not found: {db_path}")

        conn = sqlite3.connect(db_path)
        try:
            ensure_projection_intent_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                outbox_row = conn.execute(
                    _OUTBOX_ROW_QUERY, (outbox_id,)
                ).fetchone()
                if outbox_row is None:
                    raise ProjectionOutboxEventMismatchError(
                        "outbox row not found"
                    )
                if not events_match_row(event, outbox_row):
                    raise ProjectionOutboxEventMismatchError(
                        "outbox event does not match persisted row"
                    )

                existing = conn.execute(
                    _EXISTING_INTENT_QUERY, (outbox_id, consumer_id)
                ).fetchone()
                if existing is not None:
                    existing_intent = _intent_from_row(
                        outbox_id, consumer_id, existing
                    )
                    if (
                        existing_intent.aggregate_type == event.aggregate_type
                        and existing_intent.aggregate_id == event.aggregate_id
                        and existing_intent.aggregate_revision
                        == event.aggregate_revision
                        and existing_intent.event_kind == event.event_kind
                        and existing_intent.payload_sha256 == payload_sha256
                        and existing_intent.operation == operation
                        and existing_intent.artifact_ref == artifact_ref
                        and existing_intent.expected_sha256 == validated_expected
                        and existing_intent.desired_sha256 == validated_desired
                    ):
                        # Exact replay returns regardless of outbox state.
                        conn.commit()
                        return existing_intent
                    raise ProjectionReceiptConflictError(
                        "recorded intent parameters differ"
                    )

                if outbox_row[6] != "pending":
                    raise ProjectionOutboxStateError(
                        f"outbox row state is '{outbox_row[6]}', not 'pending'"
                    )

                conn.execute(
                    _INSERT_INTENT,
                    (
                        outbox_id,
                        consumer_id,
                        event.aggregate_type,
                        event.aggregate_id,
                        event.aggregate_revision,
                        event.event_kind,
                        payload_sha256,
                        operation,
                        artifact_ref,
                        validated_expected,
                        validated_desired,
                    ),
                )
                conn.commit()
                return ProjectionMutationIntent(
                    outbox_id=outbox_id,
                    consumer_id=consumer_id,
                    aggregate_type=event.aggregate_type,
                    aggregate_id=event.aggregate_id,
                    aggregate_revision=event.aggregate_revision,
                    event_kind=event.event_kind,
                    payload_sha256=payload_sha256,
                    operation=operation,
                    artifact_ref=artifact_ref,
                    expected_sha256=validated_expected,
                    desired_sha256=validated_desired,
                )
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

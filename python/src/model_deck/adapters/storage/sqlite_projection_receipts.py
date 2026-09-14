from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from model_deck.engine.projections.ports import ProjectionOutboxEvent
from model_deck.engine.projections.receipts import (
    ProjectionAppliedState,
    ProjectionOutboxConflictReceipt,
    ProjectionOutboxEventMismatchError,
    ProjectionOutboxStateError,
    ProjectionReceiptConflictError,
    events_match_row,
    row_to_applied_state,
    validate_aggregate_id,
    validate_aggregate_type,
    validate_artifact_ref,
    validate_conflict_detail,
    validate_consumer_id,
    validate_output_sha256,
    validate_projection_event,
    validate_strict_int,
)

# Schema version constants for the receipt store. ``v1`` predates deletion
# receipts and uses ``output_sha256 TEXT NOT NULL``. ``v2`` widens
# ``output_sha256`` to nullable so deletion (tombstone) receipts can store
# ``NULL`` without falling back to an empty-file sentinel.
SCHEMA_VERSION_V1 = "1"
SCHEMA_VERSION_V2 = "2"
CURRENT_SCHEMA_VERSION = SCHEMA_VERSION_V2
SCHEMA_VERSION_KEY = "schema_version"

# v2 schema: ``output_sha256`` is nullable. ``projection_outbox_conflicts`` is
# unchanged across versions.
_RECEIPT_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS projection_applied_state (
    consumer_id TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    applied_revision INTEGER NOT NULL,
    applied_outbox_id INTEGER NOT NULL,
    artifact_ref TEXT NOT NULL,
    output_sha256 TEXT,
    PRIMARY KEY (consumer_id, aggregate_type, aggregate_id)
);
CREATE TABLE IF NOT EXISTS projection_outbox_applied_receipts (
    outbox_id INTEGER NOT NULL,
    consumer_id TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    applied_revision INTEGER NOT NULL,
    artifact_ref TEXT NOT NULL,
    output_sha256 TEXT,
    PRIMARY KEY (outbox_id, consumer_id)
);
CREATE TABLE IF NOT EXISTS projection_outbox_conflicts (
    outbox_id INTEGER NOT NULL PRIMARY KEY,
    consumer_id TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""

# Metadata table tracks the receipt schema version. The receipt-store
# migration reads this table to decide whether a v1 -> v2 column change is
# required.
_RECEIPT_METADATA_SCHEMA = """
CREATE TABLE IF NOT EXISTS projection_receipt_schema_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_OUTBOX_SELECT = (
    "SELECT outbox_id, aggregate_type, aggregate_id, aggregate_revision, "
    "event_kind, payload_json, state FROM projection_outbox WHERE outbox_id = ?"
)

_APPLIED_SELECT = (
    "SELECT consumer_id, aggregate_type, aggregate_id, applied_revision, "
    "applied_outbox_id, artifact_ref, output_sha256 "
    "FROM projection_applied_state WHERE consumer_id = ? AND aggregate_type = ? "
    "AND aggregate_id = ?"
)

_APPLIED_RECEIPT_SELECT = (
    "SELECT aggregate_type, aggregate_id, applied_revision, artifact_ref, output_sha256 "
    "FROM projection_outbox_applied_receipts WHERE outbox_id = ? AND consumer_id = ?"
)


def _normalized_schema(sql: str) -> str:
    # Only the known receipt table layouts can be rebuilt safely. Ignore SQL
    # formatting and optional identifier quotes, never columns or constraints.
    normalized = re.sub(r'[\s"`\[\];]', "", sql).lower()
    return normalized.replace("ifnotexists", "")


def _receipt_schema_statements() -> tuple[str, ...]:
    return tuple(statement.strip() for statement in _RECEIPT_SCHEMA_V2.split(";") if statement.strip())


def _migration_plan(conn: sqlite3.Connection) -> list[tuple[str, str, tuple[str, ...]]]:
    """Validate every legacy table before changing any schema or data."""
    plan: list[tuple[str, str, tuple[str, ...]]] = []
    for table, statement in zip(
        ("projection_applied_state", "projection_outbox_applied_receipts"),
        _receipt_schema_statements()[:2],
        strict=True,
    ):
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if row is None:
            continue
        actual = _normalized_schema(row[0])
        if actual == _normalized_schema(statement):
            continue
        legacy = statement.replace("output_sha256 TEXT,", "output_sha256 TEXT NOT NULL,")
        if actual != _normalized_schema(legacy):
            raise ProjectionOutboxStateError(f"unsupported receipt table layout: {table}")
        replacement = table + "_new"
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (replacement,)).fetchone():
            raise ProjectionOutboxStateError(f"migration destination already exists: {replacement}")
        objects = tuple(row[0] for row in conn.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name = ? "
            "AND type IN ('index', 'trigger') AND sql IS NOT NULL ORDER BY type, name", (table,)
        ))
        plan.append((table, statement, objects))
    if plan:
        migrated = {table for table, _, _ in plan}
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
            # SQLite identifiers are quoted independently of values.
            quoted = '"' + table.replace('"', '""') + '"'
            foreign_keys = conn.execute("PRAGMA foreign_key_list(" + quoted + ")").fetchall()
            if any(table in migrated or row[2].lower() in migrated for row in foreign_keys):
                raise ProjectionOutboxStateError(
                    "receipt migration does not support foreign keys involving rebuilt tables"
                )
    return plan


def ensure_projection_receipt_schema(conn: sqlite3.Connection) -> None:
    """Atomically migrate known receipt layouts, preserving indexes and triggers.

    Reject unknown versions/layouts and foreign keys involving rebuilt tables
    before mutation. The caller must not have an active transaction.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'projection_outbox'"
        ).fetchone() is None:
            raise ProjectionOutboxStateError("projection_outbox schema is required")
        metadata_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'projection_receipt_schema_metadata'"
        ).fetchone()
        if metadata_exists:
            version = conn.execute(
                "SELECT value FROM projection_receipt_schema_metadata WHERE key = ?",
                (SCHEMA_VERSION_KEY,),
            ).fetchone()
            if version is not None and version[0] not in (SCHEMA_VERSION_V1, SCHEMA_VERSION_V2):
                raise ProjectionOutboxStateError("unsupported receipt schema version")
        plan = _migration_plan(conn)
        for table, statement, objects in plan:
            replacement = table + "_new"
            conn.execute(statement.replace(table, replacement, 1))
            conn.execute(f"INSERT INTO {replacement} SELECT * FROM {table}")
            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"ALTER TABLE {replacement} RENAME TO {table}")
            for sql in objects:
                conn.execute(sql)
        for statement in _receipt_schema_statements():
            conn.execute(statement)
        conn.execute(_RECEIPT_METADATA_SCHEMA)
        conn.execute(
            "INSERT INTO projection_receipt_schema_metadata (key, value) "
            "VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value "
            "WHERE value != excluded.value",
            (SCHEMA_VERSION_KEY, CURRENT_SCHEMA_VERSION),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


class SQLiteProjectionReceiptStore:
    """SQLite receipt store for projection outbox outcomes."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)

    def latest_unresolved_conflict_detail(self, *, consumer_id: str) -> str | None:
        """Return the newest conflict not superseded by a later applied revision."""
        consumer_id = validate_consumer_id(consumer_id)
        db_path = self._db_path
        if not db_path.is_file():
            raise FileNotFoundError(f"receipt database not found: {db_path}")
        uri = db_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            schema_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'projection_outbox_conflicts'"
            ).fetchone()
            if schema_exists is None:
                return None
            row = conn.execute(
                "SELECT c.detail FROM projection_outbox_conflicts c "
                "JOIN projection_outbox o ON o.outbox_id = c.outbox_id "
                "LEFT JOIN projection_applied_state a ON a.consumer_id = c.consumer_id "
                "AND a.aggregate_type = o.aggregate_type AND a.aggregate_id = o.aggregate_id "
                "WHERE c.consumer_id = ? AND "
                "(a.applied_revision IS NULL OR a.applied_revision <= o.aggregate_revision) "
                "ORDER BY c.outbox_id DESC LIMIT 1",
                (consumer_id,),
            ).fetchone()
        finally:
            conn.close()
        return None if row is None else row[0]

    def get_applied(
        self,
        *,
        consumer_id: str,
        aggregate_type: str,
        aggregate_id: str,
    ) -> ProjectionAppliedState | None:
        consumer_id = validate_consumer_id(consumer_id)
        aggregate_type = validate_aggregate_type(aggregate_type)
        aggregate_id = validate_aggregate_id(aggregate_id)
        db_path = self._db_path
        if not db_path.is_file():
            raise FileNotFoundError(f"receipt database not found: {db_path}")
        uri = db_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            schema_exists = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'projection_applied_state'"
            ).fetchone()
            if schema_exists is None:
                return None
            row = conn.execute(
                _APPLIED_SELECT,
                (consumer_id, aggregate_type, aggregate_id),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return row_to_applied_state(row)

    def record_applied(
        self,
        event: ProjectionOutboxEvent,
        *,
        consumer_id: str,
        artifact_ref: str,
        output_sha256: str,
    ) -> ProjectionAppliedState:
        validate_projection_event(event)
        consumer_id = validate_consumer_id(consumer_id)
        artifact_ref = validate_artifact_ref(artifact_ref)
        output_sha256 = validate_output_sha256(output_sha256)
        outbox_id = validate_strict_int("outbox_id", event.outbox_id)
        db_path = self._db_path
        if not db_path.is_file():
            raise FileNotFoundError(f"receipt database not found: {db_path}")
        conn = sqlite3.connect(db_path)
        try:
            ensure_projection_receipt_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = self._record_applied(
                    conn,
                    event,
                    consumer_id=consumer_id,
                    artifact_ref=artifact_ref,
                    output_sha256=output_sha256,
                    outbox_id=outbox_id,
                )
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def record_deleted(
        self,
        event: ProjectionOutboxEvent,
        *,
        consumer_id: str,
        artifact_ref: str,
    ) -> ProjectionAppliedState:
        """Record a verified applied deletion (tombstone).

        Mirrors ``record_applied``'s atomicity, exact-event verification, and
        revision-monotonic semantics, but persists ``output_sha256 = NULL``.
        Older revisions after a tombstone are acknowledged without rollback
        so the tombstone is preserved and earlier events cannot resurrect the
        artifact.
        """
        validate_projection_event(event)
        consumer_id = validate_consumer_id(consumer_id)
        artifact_ref = validate_artifact_ref(artifact_ref)
        outbox_id = validate_strict_int("outbox_id", event.outbox_id)
        db_path = self._db_path
        if not db_path.is_file():
            raise FileNotFoundError(f"receipt database not found: {db_path}")
        conn = sqlite3.connect(db_path)
        try:
            ensure_projection_receipt_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = self._record_deleted(
                    conn,
                    event,
                    consumer_id=consumer_id,
                    artifact_ref=artifact_ref,
                    outbox_id=outbox_id,
                )
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def record_conflict(
        self,
        event: ProjectionOutboxEvent,
        *,
        consumer_id: str,
        detail: str,
    ) -> ProjectionOutboxConflictReceipt:
        validate_projection_event(event)
        consumer_id = validate_consumer_id(consumer_id)
        detail = validate_conflict_detail(detail)
        outbox_id = validate_strict_int("outbox_id", event.outbox_id)
        db_path = self._db_path
        if not db_path.is_file():
            raise FileNotFoundError(f"receipt database not found: {db_path}")
        conn = sqlite3.connect(db_path)
        try:
            ensure_projection_receipt_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = self._record_conflict(
                    conn,
                    event,
                    consumer_id=consumer_id,
                    detail=detail,
                    outbox_id=outbox_id,
                )
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def _record_applied(
        self,
        conn: sqlite3.Connection,
        event: ProjectionOutboxEvent,
        *,
        consumer_id: str,
        artifact_ref: str,
        output_sha256: str,
        outbox_id: int,
    ) -> ProjectionAppliedState:
        row = conn.execute(_OUTBOX_SELECT, (outbox_id,)).fetchone()
        if row is None:
            raise ProjectionOutboxEventMismatchError("outbox row not found")
        if not events_match_row(event, row):
            raise ProjectionOutboxEventMismatchError(
                "outbox event does not match persisted row"
            )
        outbox_state = row[6]
        event_revision = validate_strict_int("aggregate_revision", event.aggregate_revision)
        current = conn.execute(
            _APPLIED_SELECT,
            (consumer_id, event.aggregate_type, event.aggregate_id),
        ).fetchone()
        ack_row = conn.execute(
            _APPLIED_RECEIPT_SELECT,
            (outbox_id, consumer_id),
        ).fetchone()
        if outbox_state == "applied":
            if ack_row is None:
                raise ProjectionReceiptConflictError("outbox row applied without receipt")
            if ack_row != (
                event.aggregate_type,
                event.aggregate_id,
                event_revision,
                artifact_ref,
                output_sha256,
            ):
                raise ProjectionReceiptConflictError("applied receipt parameters conflict")
            if current is None:
                raise ProjectionReceiptConflictError("applied receipt missing aggregate state")
            return row_to_applied_state(current)
        if outbox_state == "conflict":
            raise ProjectionOutboxStateError("outbox row is conflict, not pending")
        if outbox_state != "pending":
            raise ProjectionOutboxStateError("outbox row is not pending")
        if current is not None:
            current_revision = validate_strict_int("applied_revision", current[3])
            if event_revision == current_revision:
                if current[5] != artifact_ref or current[6] != output_sha256:
                    raise ProjectionReceiptConflictError(
                        "equal revision with different artifact or hash"
                    )
            elif event_revision > current_revision:
                pass
            else:
                pass
        conn.execute(
            "INSERT INTO projection_outbox_applied_receipts "
            "(outbox_id, consumer_id, aggregate_type, aggregate_id, applied_revision, "
            "artifact_ref, output_sha256) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                outbox_id,
                consumer_id,
                event.aggregate_type,
                event.aggregate_id,
                event_revision,
                artifact_ref,
                output_sha256,
            ),
        )
        if current is None or event_revision > current[3]:
            conn.execute(
                "INSERT INTO projection_applied_state "
                "(consumer_id, aggregate_type, aggregate_id, applied_revision, "
                "applied_outbox_id, artifact_ref, output_sha256) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(consumer_id, aggregate_type, aggregate_id) DO UPDATE SET "
                "applied_revision = excluded.applied_revision, "
                "applied_outbox_id = excluded.applied_outbox_id, "
                "artifact_ref = excluded.artifact_ref, "
                "output_sha256 = excluded.output_sha256",
                (
                    consumer_id,
                    event.aggregate_type,
                    event.aggregate_id,
                    event_revision,
                    outbox_id,
                    artifact_ref,
                    output_sha256,
                ),
            )
        conn.execute(
            "UPDATE projection_outbox SET state = 'applied' WHERE outbox_id = ?",
            (outbox_id,),
        )
        current = conn.execute(
            _APPLIED_SELECT,
            (consumer_id, event.aggregate_type, event.aggregate_id),
        ).fetchone()
        if current is None:
            raise ProjectionReceiptConflictError("applied state missing after write")
        return row_to_applied_state(current)

    def _record_deleted(
        self,
        conn: sqlite3.Connection,
        event: ProjectionOutboxEvent,
        *,
        consumer_id: str,
        artifact_ref: str,
        outbox_id: int,
    ) -> ProjectionAppliedState:
        row = conn.execute(_OUTBOX_SELECT, (outbox_id,)).fetchone()
        if row is None:
            raise ProjectionOutboxEventMismatchError("outbox row not found")
        if not events_match_row(event, row):
            raise ProjectionOutboxEventMismatchError(
                "outbox event does not match persisted row"
            )
        outbox_state = row[6]
        event_revision = validate_strict_int("aggregate_revision", event.aggregate_revision)
        current = conn.execute(
            _APPLIED_SELECT,
            (consumer_id, event.aggregate_type, event.aggregate_id),
        ).fetchone()
        ack_row = conn.execute(
            _APPLIED_RECEIPT_SELECT,
            (outbox_id, consumer_id),
        ).fetchone()
        # ``output_sha256`` for a deletion receipt is always ``NULL``. We
        # compare against the stored value with None-aware semantics; an
        # existing tombstone is idempotent, a previously stored hash conflicts.
        if outbox_state == "applied":
            if ack_row is None:
                raise ProjectionReceiptConflictError("outbox row applied without receipt")
            if ack_row != (
                event.aggregate_type,
                event.aggregate_id,
                event_revision,
                artifact_ref,
                None,
            ):
                raise ProjectionReceiptConflictError(
                    "applied deletion receipt parameters conflict"
                )
            if current is None:
                raise ProjectionReceiptConflictError("applied receipt missing aggregate state")
            return ProjectionAppliedState(
                consumer_id=consumer_id,
                aggregate_type=event.aggregate_type,
                aggregate_id=event.aggregate_id,
                applied_revision=event_revision,
                applied_outbox_id=outbox_id,
                artifact_ref=artifact_ref,
                output_sha256=None,
            )
        if outbox_state == "conflict":
            raise ProjectionOutboxStateError("outbox row is conflict, not pending")
        if outbox_state != "pending":
            raise ProjectionOutboxStateError("outbox row is not pending")
        if current is not None:
            current_revision = validate_strict_int("applied_revision", current[3])
            if event_revision == current_revision:
                # An existing tombstone at the same revision is idempotent
                # when the artifact_ref matches; any other stored state at
                # the same revision is a conflict.
                if current[5] != artifact_ref or current[6] is not None:
                    raise ProjectionReceiptConflictError(
                        "equal revision with non-tombstone applied state"
                    )
            elif event_revision > current_revision:
                pass
            else:
                pass
        conn.execute(
            "INSERT INTO projection_outbox_applied_receipts "
            "(outbox_id, consumer_id, aggregate_type, aggregate_id, applied_revision, "
            "artifact_ref, output_sha256) VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (
                outbox_id,
                consumer_id,
                event.aggregate_type,
                event.aggregate_id,
                event_revision,
                artifact_ref,
            ),
        )
        if current is None or event_revision > current[3]:
            conn.execute(
                "INSERT INTO projection_applied_state "
                "(consumer_id, aggregate_type, aggregate_id, applied_revision, "
                "applied_outbox_id, artifact_ref, output_sha256) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(consumer_id, aggregate_type, aggregate_id) DO UPDATE SET "
                "applied_revision = excluded.applied_revision, "
                "applied_outbox_id = excluded.applied_outbox_id, "
                "artifact_ref = excluded.artifact_ref, "
                "output_sha256 = NULL",
                (
                    consumer_id,
                    event.aggregate_type,
                    event.aggregate_id,
                    event_revision,
                    outbox_id,
                    artifact_ref,
                ),
            )
        conn.execute(
            "UPDATE projection_outbox SET state = 'applied' WHERE outbox_id = ?",
            (outbox_id,),
        )
        current = conn.execute(
            _APPLIED_SELECT,
            (consumer_id, event.aggregate_type, event.aggregate_id),
        ).fetchone()
        if current is None:
            raise ProjectionReceiptConflictError("applied state missing after write")
        return row_to_applied_state(current)

    def _record_conflict(
        self,
        conn: sqlite3.Connection,
        event: ProjectionOutboxEvent,
        *,
        consumer_id: str,
        detail: str,
        outbox_id: int,
    ) -> ProjectionOutboxConflictReceipt:
        row = conn.execute(_OUTBOX_SELECT, (outbox_id,)).fetchone()
        if row is None:
            raise ProjectionOutboxEventMismatchError("outbox row not found")
        if not events_match_row(event, row):
            raise ProjectionOutboxEventMismatchError(
                "outbox event does not match persisted row"
            )
        outbox_state = row[6]
        existing = conn.execute(
            "SELECT consumer_id, detail FROM projection_outbox_conflicts WHERE outbox_id = ?",
            (outbox_id,),
        ).fetchone()
        if existing is not None:
            if existing[0] == consumer_id and existing[1] == detail:
                return ProjectionOutboxConflictReceipt(
                    outbox_id=outbox_id,
                    consumer_id=consumer_id,
                    detail=detail,
                )
            raise ProjectionReceiptConflictError("conflict receipt parameters conflict")
        if outbox_state == "applied":
            raise ProjectionOutboxStateError("outbox row is applied, not pending")
        if outbox_state == "conflict":
            raise ProjectionOutboxStateError("outbox row already conflict without receipt")
        if outbox_state != "pending":
            raise ProjectionOutboxStateError("outbox row is not pending")
        conn.execute(
            "INSERT INTO projection_outbox_conflicts (outbox_id, consumer_id, detail) "
            "VALUES (?, ?, ?)",
            (outbox_id, consumer_id, detail),
        )
        conn.execute(
            "UPDATE projection_outbox SET state = 'conflict' WHERE outbox_id = ?",
            (outbox_id,),
        )
        return ProjectionOutboxConflictReceipt(
            outbox_id=outbox_id,
            consumer_id=consumer_id,
            detail=detail,
        )

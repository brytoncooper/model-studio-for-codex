from __future__ import annotations

import sqlite3
from pathlib import Path

from model_deck.engine.projections.ports import (
    OUTBOX_LIMIT_DEFAULT,
    ProjectionOutboxEvent,
    validate_outbox_limit,
)

_PENDING_QUERY = (
    "SELECT outbox_id, aggregate_type, aggregate_id, aggregate_revision, "
    "event_kind, payload_json FROM projection_outbox "
    "WHERE state = 'pending' ORDER BY outbox_id ASC LIMIT ?"
)

_UNEXPANDED_PENDING_QUERY = (
    "SELECT o.outbox_id, o.aggregate_type, o.aggregate_id, o.aggregate_revision, o.event_kind, o.payload_json "
    "FROM projection_outbox o WHERE o.state = 'pending' AND NOT ("
    "o.aggregate_type = 'connection' AND o.event_kind = 'connection.saved' AND EXISTS ("
    "SELECT 1 FROM projection_dependency_expansions e WHERE e.outbox_id = o.outbox_id "
    "AND e.connection_id = o.aggregate_id AND e.connection_revision = o.aggregate_revision "
    "AND e.payload_json = o.payload_json)) ORDER BY o.outbox_id ASC LIMIT ?"
)


class SQLiteProjectionOutboxReader:
    """SQLite read-only implementation of ProjectionOutboxReader."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def list_pending(self, *, limit: int = OUTBOX_LIMIT_DEFAULT) -> tuple[ProjectionOutboxEvent, ...]:
        """Return pending outbox events ordered by outbox_id ascending.

        :param limit: Maximum events to return, strict int in 1..1000.
        :returns: Snapshot tuple of immutable events.
        :raises TypeError: If ``limit`` is not a strict ``int``.
        :raises ValueError: If ``limit`` is outside 1..1000.
        :raises FileNotFoundError: If the database file does not exist.
        """
        validate_outbox_limit(limit)
        db_path = Path(self._db_path)
        if not db_path.is_file():
            raise FileNotFoundError(f"outbox database not found: {db_path}")
        uri = db_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            has_expansions = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'projection_dependency_expansions'"
            ).fetchone() is not None
            query = _UNEXPANDED_PENDING_QUERY if has_expansions else _PENDING_QUERY
            rows = conn.execute(query, (limit,)).fetchall()
        finally:
            conn.close()
        return tuple(
            ProjectionOutboxEvent(
                outbox_id=row[0],
                aggregate_type=row[1],
                aggregate_id=row[2],
                aggregate_revision=row[3],
                event_kind=row[4],
                payload_json=row[5],
            )
            for row in rows
        )

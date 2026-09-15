"""SQLite home of the evidence cache. Storage never fetches."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from model_deck.engine.evidence.ports import (
    REFRESH_ERROR_CODES,
    EvidenceSnapshot,
    require_evidence_kind,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence_snapshots (
    kind TEXT PRIMARY KEY,
    source_id TEXT,
    fetched_at TEXT,
    snapshot_json TEXT,
    last_error TEXT,
    attempted_at TEXT
);
"""


def _document_to_canonical(snapshot: EvidenceSnapshot) -> str:
    return json.dumps(
        snapshot.to_document(), sort_keys=True, separators=(",", ":"), allow_nan=False
    )


class SqliteEvidenceCacheRepository:
    """One row per evidence kind: the last good snapshot plus its last failure.

    `source_id` and `fetched_at` are inspection copies of values inside
    `snapshot_json`; the document is the single source of truth on read.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        with closing(self._connect()) as connection, connection:
            connection.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self._db_path), timeout=30.0)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def get_snapshot(self, kind: str) -> EvidenceSnapshot | None:
        require_evidence_kind(kind)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT snapshot_json, last_error, attempted_at"
                " FROM evidence_snapshots WHERE kind = ?",
                (kind,),
            ).fetchone()
        if row is None:
            return None
        document: dict[str, Any] | None = json.loads(row[0]) if row[0] else None
        return EvidenceSnapshot.from_document(
            kind, document, last_refresh_error=row[1], attempted_at=row[2]
        )

    def put_snapshot(self, kind: str, snapshot: EvidenceSnapshot) -> None:
        """Replace the cached snapshot in one statement, clearing the last failure."""
        require_evidence_kind(kind)
        if snapshot.kind != kind:
            raise ValueError(
                f"snapshot kind {snapshot.kind!r} does not match {kind!r}"
            )
        if snapshot.fetched_at is None or snapshot.source_id is None:
            raise ValueError(
                "a cached snapshot must name the source it came from and when it"
                " was fetched"
            )
        canonical = _document_to_canonical(snapshot)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO evidence_snapshots"
                " (kind, source_id, fetched_at, snapshot_json, last_error, attempted_at)"
                " VALUES (?, ?, ?, ?, NULL, NULL)"
                " ON CONFLICT(kind) DO UPDATE SET"
                " source_id = excluded.source_id,"
                " fetched_at = excluded.fetched_at,"
                " snapshot_json = excluded.snapshot_json,"
                " last_error = NULL,"
                " attempted_at = NULL",
                (kind, snapshot.source_id, snapshot.fetched_at, canonical),
            )

    def record_refresh_failure(
        self, kind: str, attempted_at: str, error_code: str
    ) -> None:
        """Keep the last good snapshot and remember why the newest attempt failed."""
        require_evidence_kind(kind)
        if error_code not in REFRESH_ERROR_CODES:
            raise ValueError(f"unsupported refresh error code: {error_code!r}")
        if not isinstance(attempted_at, str) or not attempted_at:
            raise ValueError("refresh failures must record when they were attempted")
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO evidence_snapshots"
                " (kind, source_id, fetched_at, snapshot_json, last_error, attempted_at)"
                " VALUES (?, NULL, NULL, NULL, ?, ?)"
                " ON CONFLICT(kind) DO UPDATE SET"
                " last_error = excluded.last_error,"
                " attempted_at = excluded.attempted_at",
                (kind, error_code, attempted_at),
            )

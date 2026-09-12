from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from model_deck.engine.usage.ports import (
    USAGE_QUERY_MAX_RECORDS,
    QueryUsageResult,
    RecordUsageResult,
    UsageConflictError,
    UsageRecord,
    UsageResourceExhaustedError,
    normalize_observed_at,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_records (
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    observed_norm TEXT NOT NULL,
    record_json TEXT NOT NULL,
    PRIMARY KEY (run_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_usage_records_observed
    ON usage_records (observed_norm, run_id, sequence);
"""

def _record_to_canonical(record: UsageRecord) -> str:
    return json.dumps(record.to_wire(), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _record_from_dict(payload: dict[str, Any]) -> UsageRecord:
    return UsageRecord.from_wire(payload)


class SqliteUsageRepository:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        with closing(self._connect()) as connection, connection:
            connection.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self._db_path), timeout=30.0)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def store(self, record: UsageRecord, sequence: int) -> RecordUsageResult:
        canonical = _record_to_canonical(record)
        observed_norm = normalize_observed_at(record.observed_at)
        with closing(self._connect()) as connection, connection:
            try:
                connection.execute(
                    "INSERT INTO usage_records"
                    " (run_id, sequence, session_id, observed_at, observed_norm, record_json)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        record.run_id,
                        sequence,
                        record.session_id,
                        record.observed_at,
                        observed_norm,
                        canonical,
                    ),
                )
            except sqlite3.IntegrityError:
                row = connection.execute(
                    "SELECT record_json FROM usage_records"
                    " WHERE run_id = ? AND sequence = ?",
                    (record.run_id, sequence),
                ).fetchone()
                if row is not None and row[0] == canonical:
                    return RecordUsageResult(record=record, duplicate=True)
                raise UsageConflictError(
                    f"conflicting usage record for run {record.run_id}"
                    f" sequence {sequence}"
                ) from None
        return RecordUsageResult(record=record, duplicate=False)

    def query(self, since: str | None, until: str | None) -> QueryUsageResult:
        clauses: list[str] = []
        args: list[Any] = []
        if since is not None:
            clauses.append("observed_norm >= ?")
            args.append(since)
        if until is not None:
            clauses.append("observed_norm <= ?")
            args.append(until)
        where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT record_json FROM usage_records"
                " " + where + " ORDER BY observed_norm, run_id, sequence LIMIT ?",
                [*args, USAGE_QUERY_MAX_RECORDS + 1],
            ).fetchall()
        if len(rows) > USAGE_QUERY_MAX_RECORDS:
            raise UsageResourceExhaustedError(
                f"usage query exceeds {USAGE_QUERY_MAX_RECORDS} records"
            )
        return QueryUsageResult(
            records=tuple(
                _record_from_dict(json.loads(row[0])) for row in rows
            ),
        )

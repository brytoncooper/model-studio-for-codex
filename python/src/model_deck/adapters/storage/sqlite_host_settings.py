from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from model_deck.engine.host_settings.ports import (
    CLAIM_ADMITTED,
    CLAIM_IN_PROGRESS,
    CLAIM_REPLAY,
    PreviewExistsError,
    PreviewRecord,
    ReceiptConflictError,
    SaveClaimBinding,
)

_BUSY_TIMEOUT_S = 10.0

_RESULT_FIELDS = frozenset(
    {
        "saved",
        "changed",
        "document_id",
        "previous_content_hash",
        "document_revision",
        "backup",
        "application_effects",
        "context_revision",
    }
)

_PREVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS host_preview_tokens (
    preview_id TEXT PRIMARY KEY,
    principal TEXT NOT NULL,
    host_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    base_content_hash TEXT NOT NULL,
    context_revision TEXT NOT NULL,
    candidate_content_hash TEXT NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS host_save_receipts (
    principal TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    host_id TEXT,
    document_id TEXT,
    base_content_hash TEXT,
    context_revision TEXT,
    candidate_content_hash TEXT,
    preview_id TEXT,
    result_json TEXT,
    settled INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (principal, idempotency_key, fingerprint)
);
CREATE TABLE IF NOT EXISTS host_preview_reservations (
    preview_id TEXT PRIMARY KEY,
    principal TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL
);
"""


def ensure_host_settings_schema(conn: sqlite3.Connection) -> None:
    """Create host-settings ledger tables when absent."""
    conn.executescript(_PREVIEW_SCHEMA)
    conn.commit()


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=_BUSY_TIMEOUT_S)
    ensure_host_settings_schema(conn)
    return conn


def _row_to_preview(row: tuple[Any, ...]) -> PreviewRecord:
    return PreviewRecord(
        preview_id=row[0],
        principal=row[1],
        host_id=row[2],
        document_id=row[3],
        base_content_hash=row[4],
        context_revision=row[5],
        candidate_content_hash=row[6],
    )


import re as _re

_ALLOWED_EFFECTS = frozenset({"immediate", "future_session", "host_restart", "model_deck_restart", "unknown"})
_SHA_RE = _re.compile(r"^sha256:[0-9a-f]{64}$")


def _is_content_hash(value: object, *, allow_absent: bool) -> bool:
    if allow_absent and value == "absent":
        return True
    return isinstance(value, str) and _SHA_RE.match(value) is not None


def validate_save_result(result: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe copy of a save result or raise ValueError."""
    if not isinstance(result, dict):
        raise ValueError("settings save result rejected") from None
    unknown = set(result) - _RESULT_FIELDS
    if unknown:
        raise ValueError("settings save result rejected") from None
    for key in _RESULT_FIELDS:
        if key not in result:
            raise ValueError("settings save result rejected") from None
    if result["saved"] is not True:
        raise ValueError("settings save result rejected") from None
    if not isinstance(result["changed"], bool):
        raise ValueError("settings save result rejected") from None
    doc_id = result["document_id"]
    if not isinstance(doc_id, str) or not 1 <= len(doc_id) <= 256:
        raise ValueError("settings save result rejected") from None
    if not _is_content_hash(result["previous_content_hash"], allow_absent=True):
        raise ValueError("settings save result rejected") from None
    if not _is_content_hash(result["document_revision"], allow_absent=False):
        raise ValueError("settings save result rejected") from None
    ctx = result["context_revision"]
    if not isinstance(ctx, str) or not 1 <= len(ctx) <= 256:
        raise ValueError("settings save result rejected") from None
    backup = result["backup"]
    if backup is not None:
        if not isinstance(backup, dict) or set(backup) != {"backup_id", "display_path"}:
            raise ValueError("settings save result rejected") from None
        if not isinstance(backup["backup_id"], str) or not 1 <= len(backup["backup_id"]) <= 256:
            raise ValueError("settings save result rejected") from None
        if not isinstance(backup["display_path"], str) or not 1 <= len(backup["display_path"]) <= 2048:
            raise ValueError("settings save result rejected") from None
    effects = result["application_effects"]
    if not isinstance(effects, list) or len(effects) > 16:
        raise ValueError("settings save result rejected") from None
    for item in effects:
        if not isinstance(item, str) or item not in _ALLOWED_EFFECTS:
            raise ValueError("settings save result rejected") from None
    try:
        canonical = json.loads(json.dumps(result))
    except (TypeError, ValueError):
        raise ValueError("settings save result rejected") from None
    if not isinstance(canonical, dict):
        raise ValueError("settings save result rejected") from None
    return canonical


class SQLitePreviewStore:
    """Durable single-use admission ledger for preview tokens."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)

    def admit(self, record: PreviewRecord) -> None:
        if not isinstance(record.preview_id, str) or not record.preview_id:
            raise ValueError("preview token must carry a nonempty preview_id")
        conn = _connect(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO host_preview_tokens (preview_id, principal, host_id, document_id, "
                    "base_content_hash, context_revision, candidate_content_hash, consumed) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                    (
                        record.preview_id,
                        record.principal,
                        record.host_id,
                        record.document_id,
                        record.base_content_hash,
                        record.context_revision,
                        record.candidate_content_hash,
                    ),
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                raise PreviewExistsError("duplicate preview token") from None
            conn.commit()
        finally:
            conn.close()

    def lookup(self, preview_id: str) -> PreviewRecord | None:
        conn = _connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT preview_id, principal, host_id, document_id, base_content_hash, "
                "context_revision, candidate_content_hash FROM host_preview_tokens "
                "WHERE preview_id = ? AND consumed = 0",
                (preview_id,),
            ).fetchone()
            return _row_to_preview(row) if row is not None else None
        finally:
            conn.close()

    def is_consumed(self, preview_id: str) -> bool:
        conn = _connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT consumed FROM host_preview_tokens WHERE preview_id = ?",
                (preview_id,),
            ).fetchone()
            return row is not None and row[0] == 1
        finally:
            conn.close()

    def consume(self, preview_id: str) -> PreviewRecord | None:
        conn = _connect(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT preview_id, principal, host_id, document_id, base_content_hash, "
                "context_revision, candidate_content_hash, consumed FROM host_preview_tokens "
                "WHERE preview_id = ?",
                (preview_id,),
            ).fetchone()
            if row is None or row[7] == 1:
                conn.rollback()
                return None
            conn.execute(
                "UPDATE host_preview_tokens SET consumed = 1 WHERE preview_id = ?",
                (preview_id,),
            )
            conn.commit()
            return _row_to_preview(row)
        finally:
            conn.close()


class SQLiteSaveReceiptStore:
    """Durable idempotency receipt ledger with atomic claim/settle."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)

    def lookup(
        self, principal: str, idempotency_key: str, fingerprint: str
    ) -> dict[str, Any] | None:
        conn = _connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT result_json, settled FROM host_save_receipts "
                "WHERE principal = ? AND idempotency_key = ? AND fingerprint = ?",
                (principal, idempotency_key, fingerprint),
            ).fetchone()
            if row is None or row[1] != 1 or row[0] is None:
                return None
            stored = json.loads(row[0])
            return dict(stored) if isinstance(stored, dict) else None
        finally:
            conn.close()

    def store(
        self,
        principal: str,
        idempotency_key: str,
        fingerprint: str,
        result: dict[str, Any],
    ) -> None:
        canonical = validate_save_result(result)
        conn = _connect(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT fingerprint, result_json, settled FROM host_save_receipts "
                "WHERE principal = ? AND idempotency_key = ?",
                (principal, idempotency_key),
            ).fetchall()
            if not rows:
                conn.rollback()
                raise ReceiptConflictError("no admitted claim")
            for row in rows:
                if row[0] != fingerprint:
                    conn.rollback()
                    raise ReceiptConflictError("idempotency key reuse")
            if rows[0][1] is not None and json.loads(rows[0][1]) != canonical:
                conn.rollback()
                raise ReceiptConflictError("settled receipt is immutable")
            conn.execute(
                "UPDATE host_save_receipts SET result_json = ?, settled = 1 "
                "WHERE principal = ? AND idempotency_key = ? AND fingerprint = ?",
                (json.dumps(canonical), principal, idempotency_key, fingerprint),
            )
            conn.commit()
        finally:
            conn.close()

    def claim(
        self, binding: SaveClaimBinding
    ) -> tuple[str, dict[str, Any] | None]:
        for _ in range(2):
            conn = _connect(self._db_path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                owner = conn.execute(
                    "SELECT principal, idempotency_key, fingerprint "
                    "FROM host_preview_reservations WHERE preview_id = ?",
                    (binding.preview_id,),
                ).fetchone()
                mine = (binding.principal, binding.idempotency_key, binding.fingerprint)
                if owner is not None and tuple(owner) != mine:
                    conn.rollback()
                    raise ReceiptConflictError("preview already reserved")
                rows = conn.execute(
                    "SELECT fingerprint, result_json, settled FROM host_save_receipts "
                    "WHERE principal = ? AND idempotency_key = ?",
                    (binding.principal, binding.idempotency_key),
                ).fetchall()
                for row in rows:
                    if row[0] != binding.fingerprint:
                        conn.rollback()
                        raise ReceiptConflictError("idempotency key reuse")
                if rows:
                    if rows[0][1] is not None:
                        stored = json.loads(rows[0][1])
                        conn.rollback()
                        return (CLAIM_REPLAY, dict(stored))
                    conn.rollback()
                    return (CLAIM_IN_PROGRESS, None)
                if owner is not None:
                    conn.rollback()
                    return (CLAIM_IN_PROGRESS, None)
                try:
                    conn.execute(
                        "INSERT INTO host_save_receipts (principal, idempotency_key, fingerprint, "
                        "host_id, document_id, base_content_hash, context_revision, "
                        "candidate_content_hash, preview_id, result_json, settled) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)",
                        (
                            binding.principal,
                            binding.idempotency_key,
                            binding.fingerprint,
                            binding.host_id,
                            binding.document_id,
                            binding.base_content_hash,
                            binding.context_revision,
                            binding.candidate_content_hash,
                            binding.preview_id,
                        ),
                    )
                    conn.execute(
                        "INSERT INTO host_preview_reservations "
                        "(preview_id, principal, idempotency_key, fingerprint) "
                        "VALUES (?, ?, ?, ?)",
                        (binding.preview_id, binding.principal, binding.idempotency_key, binding.fingerprint),
                    )
                except sqlite3.IntegrityError:
                    conn.rollback()
                    continue
                conn.commit()
                return (CLAIM_ADMITTED, None)
            finally:
                conn.close()
        raise ReceiptConflictError("preview already reserved")

    def settle(
        self,
        principal: str,
        idempotency_key: str,
        fingerprint: str,
        result: dict[str, Any],
    ) -> None:
        canonical = validate_save_result(result)
        conn = _connect(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT fingerprint, result_json, settled FROM host_save_receipts "
                "WHERE principal = ? AND idempotency_key = ?",
                (principal, idempotency_key),
            ).fetchall()
            if not rows:
                conn.rollback()
                raise ReceiptConflictError("no admitted claim")
            for row in rows:
                if row[0] != fingerprint:
                    conn.rollback()
                    raise ReceiptConflictError("idempotency key reuse")
            if rows[0][1] is not None:
                if json.loads(rows[0][1]) != canonical:
                    conn.rollback()
                    raise ReceiptConflictError("settled receipt is immutable")
                conn.commit()
                return
            conn.execute(
                "UPDATE host_save_receipts SET result_json = ?, settled = 1 "
                "WHERE principal = ? AND idempotency_key = ? AND fingerprint = ?",
                (json.dumps(canonical), principal, idempotency_key, fingerprint),
            )
            conn.commit()
        finally:
            conn.close()

    def release(
        self, principal: str, idempotency_key: str, fingerprint: str
    ) -> None:
        conn = _connect(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT fingerprint, result_json, preview_id FROM host_save_receipts "
                "WHERE principal = ? AND idempotency_key = ?",
                (principal, idempotency_key),
            ).fetchall()
            if not rows:
                conn.commit()
                return
            for row in rows:
                if row[0] != fingerprint:
                    conn.rollback()
                    raise ReceiptConflictError("idempotency key reuse")
            if rows[0][1] is not None:
                conn.commit()
                return
            preview_id = rows[0][2]
            conn.execute(
                "DELETE FROM host_save_receipts "
                "WHERE principal = ? AND idempotency_key = ? AND fingerprint = ?",
                (principal, idempotency_key, fingerprint),
            )
            if isinstance(preview_id, str) and preview_id:
                conn.execute(
                    "DELETE FROM host_preview_reservations WHERE preview_id = ? "
                    "AND principal = ? AND idempotency_key = ? AND fingerprint = ?",
                    (preview_id, principal, idempotency_key, fingerprint),
                )
            conn.commit()
        finally:
            conn.close()

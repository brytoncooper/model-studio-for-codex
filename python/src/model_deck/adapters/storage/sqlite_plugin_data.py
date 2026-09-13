from __future__ import annotations

import json
import math
import re
import sqlite3
from pathlib import Path
from typing import Any

from model_deck.engine.plugin_data.ports import (
    LIST_LIMIT_MAX,
    LIST_LIMIT_MIN,
    PluginDataEntry,
    PluginDataListItem,
    PluginDataNotFoundError,
    PluginDataQuota,
    PluginDataQuotaExceededError,
    PluginDataRevisionConflictError,
)

_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9_-]*)+$")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS plugin_data (
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    revision INTEGER NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (namespace, key)
);
"""


class SQLitePluginDataRepository:
    def __init__(self, db_path: Path, quota: PluginDataQuota | None = None) -> None:
        self._db_path = db_path
        self._quota = quota or PluginDataQuota()
        validate_plugin_data_quota(self._quota)

    def get(self, namespace: str, key: str) -> PluginDataEntry:
        _validate_namespace(namespace)
        _validate_key(key)
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT value_json, revision, deleted FROM plugin_data WHERE namespace = ? AND key = ?",
                (namespace, key),
            ).fetchone()
            if row is None or row[2]:
                raise PluginDataNotFoundError(key)
            return PluginDataEntry(namespace=namespace, key=key, value=json.loads(row[0]), revision=row[1])
        finally:
            conn.close()

    def list(self, namespace: str, prefix: str = "", limit: int = 200) -> list[PluginDataListItem]:
        _validate_namespace(namespace)
        _validate_prefix(prefix)
        _validate_limit(limit)
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            if not prefix:
                rows = conn.execute(
                    "SELECT key, revision FROM plugin_data WHERE namespace = ? AND deleted = 0 ORDER BY key ASC LIMIT ?",
                    (namespace, limit),
                ).fetchall()
            else:
                upper = _prefix_upper(prefix)
                if upper is None:
                    rows = conn.execute(
                        "SELECT key, revision FROM plugin_data WHERE namespace = ? AND deleted = 0 AND key >= ? ORDER BY key ASC LIMIT ?",
                        (namespace, prefix, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT key, revision FROM plugin_data WHERE namespace = ? AND deleted = 0 AND key >= ? AND key < ? ORDER BY key ASC LIMIT ?",
                        (namespace, prefix, upper, limit),
                    ).fetchall()
            return [PluginDataListItem(key=r[0], revision=r[1]) for r in rows if r[0].startswith(prefix)]
        finally:
            conn.close()

    def put(self, namespace: str, key: str, value: Any, expected_revision: int | None = None) -> PluginDataEntry:
        _validate_namespace(namespace)
        _validate_key(key)
        _validate_expected_revision(expected_revision)
        canonical = _canonical_value(value)
        _validate_value_size(canonical, self._quota)
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT value_json, revision, deleted FROM plugin_data WHERE namespace = ? AND key = ?",
                    (namespace, key),
                ).fetchone()
                if row is None:
                    if expected_revision not in (None, 0):
                        raise PluginDataRevisionConflictError("stale revision")
                    _check_quota_for_put(conn, namespace, key, canonical, self._quota)
                    conn.execute(
                        "INSERT INTO plugin_data (namespace, key, value_json, revision, deleted) VALUES (?, ?, ?, 1, 0)",
                        (namespace, key, canonical),
                    )
                    conn.commit()
                    return PluginDataEntry(namespace=namespace, key=key, value=json.loads(canonical), revision=1)
                current_rev = row[1]
                if expected_revision is not None and expected_revision != current_rev:
                    raise PluginDataRevisionConflictError("stale revision")
                _check_quota_for_put(conn, namespace, key, canonical, self._quota)
                new_rev = current_rev + 1
                conn.execute(
                    "UPDATE plugin_data SET value_json = ?, revision = ?, deleted = 0 WHERE namespace = ? AND key = ?",
                    (canonical, new_rev, namespace, key),
                )
                conn.commit()
                return PluginDataEntry(namespace=namespace, key=key, value=json.loads(canonical), revision=new_rev)
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def delete(self, namespace: str, key: str, expected_revision: int | None = None) -> bool:
        _validate_namespace(namespace)
        _validate_key(key)
        _validate_expected_revision(expected_revision)
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT revision, deleted FROM plugin_data WHERE namespace = ? AND key = ?",
                    (namespace, key),
                ).fetchone()
                if row is None:
                    if expected_revision not in (None, 0):
                        raise PluginDataRevisionConflictError("stale revision")
                    conn.commit()
                    return False
                current_rev, deleted = row[0], row[1]
                if deleted:
                    if expected_revision is not None and expected_revision != current_rev:
                        raise PluginDataRevisionConflictError("stale revision")
                    conn.commit()
                    return False
                if expected_revision is not None and expected_revision != current_rev:
                    raise PluginDataRevisionConflictError("stale revision")
                conn.execute(
                    "UPDATE plugin_data SET revision = ?, deleted = 1 WHERE namespace = ? AND key = ?",
                    (current_rev + 1, namespace, key),
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=30.0, isolation_level=None, check_same_thread=False)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(_SCHEMA_SQL)


def _check_quota_for_put(conn: sqlite3.Connection, namespace: str, key: str, canonical: str, quota: PluginDataQuota) -> None:
    rows = conn.execute("SELECT key, value_json FROM plugin_data WHERE namespace = ? AND deleted = 0", (namespace,)).fetchall()
    enforce_plugin_data_quota(namespace, key, canonical, rows, quota)


def _canonical_value(value: Any) -> str:
    _reject_nonfinite(value)
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid JSON value: {exc}") from exc


def _reject_nonfinite(value: Any) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("non-finite numbers are not valid JSON values")
        return
    if isinstance(value, tuple):
        raise TypeError("tuples are not valid JSON values")
    if isinstance(value, list):
        for v in value:
            _reject_nonfinite(v)
        return
    if isinstance(value, dict):
        for dict_key, v in value.items():
            if not isinstance(dict_key, str):
                raise TypeError("JSON object keys must be strings")
            _reject_nonfinite(v)
        return
    if value is None or isinstance(value, (str, int)):
        return
    raise TypeError(f"unsupported JSON value type: {type(value).__name__}")


def _validate_namespace(namespace: Any) -> None:
    if not isinstance(namespace, str) or _NAMESPACE_RE.fullmatch(namespace) is None or len(namespace) > 256 or len(namespace) < 3:
        raise ValueError("invalid namespace")


def _has_lone_surrogate(s: str) -> bool:
    return any(0xD800 <= ord(c) <= 0xDFFF for c in s)


def _validate_key(key: Any) -> None:
    if not isinstance(key, str) or len(key) > 256:
        raise ValueError("invalid key")
    if _has_lone_surrogate(key):
        raise ValueError("invalid key")


def _validate_prefix(prefix: Any) -> None:
    if not isinstance(prefix, str) or len(prefix) > 256:
        raise ValueError("invalid prefix")
    if _has_lone_surrogate(prefix):
        raise ValueError("invalid prefix")


def _validate_limit(limit: Any) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not (LIST_LIMIT_MIN <= limit <= LIST_LIMIT_MAX):
        raise ValueError("invalid limit")


def _validate_expected_revision(expected: Any) -> None:
    if expected is None:
        return
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
        raise ValueError("invalid expected_revision")


def _validate_value_size(canonical: str, quota: PluginDataQuota) -> None:
    if len(canonical.encode("utf-8")) > quota.max_value_bytes:
        raise PluginDataQuotaExceededError("value exceeds maximum size")


def _prefix_upper(prefix: str) -> str | None:
    for i in range(len(prefix) - 1, -1, -1):
        code = ord(prefix[i])
        if code < 0x10FFFF:
            nxt = code + 1
            if 0xD800 <= nxt <= 0xDFFF:
                nxt = 0xE000
            return prefix[:i] + chr(nxt)
    return None


def _range_upper(prefix: str) -> str | None:
    return _prefix_upper(prefix)


def validate_plugin_data_quota(quota: PluginDataQuota) -> None:
    if (
        quota.max_bytes <= 0
        or quota.max_keys <= 0
        or quota.max_value_bytes <= 0
    ):
        raise ValueError("quota limits must be positive")


def validate_plugin_data_namespace(namespace: Any) -> None:
    _validate_namespace(namespace)


def validate_plugin_data_key(key: Any) -> None:
    _validate_key(key)


def validate_plugin_data_prefix(prefix: Any) -> None:
    _validate_prefix(prefix)


def validate_plugin_data_limit(limit: Any) -> None:
    _validate_limit(limit)


def validate_plugin_data_expected_revision(expected_revision: Any) -> None:
    _validate_expected_revision(expected_revision)


def canonicalize_plugin_data_value(value: Any) -> str:
    return _canonical_value(value)


def validate_plugin_data_value_size(
    canonical_value: str,
    quota: PluginDataQuota,
) -> None:
    _validate_value_size(canonical_value, quota)


def plugin_data_prefix_upper(prefix: str) -> str | None:
    return _prefix_upper(prefix)


def plugin_data_entry_usage(
    namespace: str,
    key: str,
    canonical_value: str,
) -> int:
    return (
        len(namespace.encode("utf-8"))
        + len(key.encode("utf-8"))
        + len(canonical_value.encode("utf-8"))
    )


def enforce_plugin_data_quota(
    namespace: str,
    key: str,
    canonical_value: str,
    existing_live_entries: list[tuple[str, str]],
    quota: PluginDataQuota,
) -> None:
    new_usage = plugin_data_entry_usage(namespace, key, canonical_value)
    total = 0
    count = 0
    for existing_key, existing_value in existing_live_entries:
        if existing_key == key:
            continue
        total += plugin_data_entry_usage(namespace, existing_key, existing_value)
        count += 1
    total += new_usage
    count += 1
    value_size = len(canonical_value.encode("utf-8"))
    if value_size > quota.max_value_bytes:
        raise PluginDataQuotaExceededError("value exceeds maximum size")
    if total > quota.max_bytes:
        raise PluginDataQuotaExceededError("quota bytes exceeded")
    if count > quota.max_keys:
        raise PluginDataQuotaExceededError("quota keys exceeded")

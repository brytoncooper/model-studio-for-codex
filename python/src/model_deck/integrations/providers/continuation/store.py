"""Private durable SQLite continuation store for B15.

Contract (frozen by lead):

- Trusted scope is constructed from RunRequest identity and contains nine
  opaque fields: ``session_id``, ``connection_id``, ``connection_revision``
  (an ``int``), ``provider_id``, ``provider_model_id``, ``execution_mode``,
  ``endpoint_config_ref`` (opaque ref only), ``credential_ref`` (opaque ref
  only, never secret), and an engine-issued ``continuation_handle``.
- ``ContinuationRouteScope`` is a frozen dataclass with those nine fields;
  ``__repr__`` redacts ``credential_ref`` to ``<opaque>``; the remaining
  refs (endpoint_config_ref, continuation_handle) stay visible because they
  are routing metadata, not secrets.
- ``ContinuationRecord`` is a frozen dataclass; ``__repr__`` omits
  ``response_id`` and ``metadata`` so logs do not leak stored reasoning.
- The store requires an explicit database path; private owner-only
  directory/file checks mirror the legacy ``ProviderContinuationStore``.
- A session binds to one route scope on the first save; later saves or
  loads for the same session with a different route scope raise a sanitized
  ``ContinuationError``. Loads for a different session raise the same error.
- Records persist across reopen; each save is one atomic transaction; a
  failed save installs nothing. An exact duplicate save is idempotent; a
  save that would change ``response_id`` for an existing ``item_ref`` is
  rejected. A load that does not match the recorded visible-item identity
  is rejected.
- Identity-resolver API: ``load_for_item(scope, visible_item)`` queries by
  the stable visible-item identity so V2 normalized history (which strips
  provider item IDs) can find the next-run continuation. An ambiguous
  collision (two records with the same identity in the same session) is
  rejected explicitly rather than returning one. ``load_all(scope)``
  enumerates every record bound to that session, and returns ``[]`` when
  the session is not yet bound (first-turn replay) while still rejecting a
  scope mismatch or a corrupt/incompatible schema.
- Batch atomicity: ``save_response(scope, response_id, items)`` saves the
  whole response (every item and its metadata) in one transaction so a
  provider can land a completed response all-or-nothing. A transaction-owned
  response sequence preserves commit order even if the wall clock repeats or
  moves backward.
- ``clear(scope)`` drops the session binding and every record under it.
  Idempotent for an unbound session; rejects a scope mismatch or a
  corrupt/incompatible schema.
- ``prepare_session_reset`` / ``commit_session_reset`` /
  ``rollback_session_reset`` form the narrow engine-authorized reset path.
  Preparation keeps old state recoverable until the application session
  commit succeeds; a durable pending intent can finish retirement after a
  crash.
- The store records a ``schema_version`` row. No TTL/encryption/signing.
"""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from .translate import ContinuationError, _item_identity


SCHEMA_VERSION = "1"
MAX_RECORD_METADATA_BYTES = 1_048_576
MAX_RESPONSE_METADATA_BYTES = 8_388_608
MAX_RESPONSE_ITEMS = 256
_OPAQUE_REF = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|ref:[a-z][a-z0-9._-]{0,120})$"
)


_SCOPE_FIELDS = (
    "session_id",
    "connection_id",
    "connection_revision",
    "provider_id",
    "provider_model_id",
    "execution_mode",
    "endpoint_config_ref",
    "credential_ref",
    "continuation_handle",
)


@dataclass(frozen=True)
class ContinuationRouteScope:
    """Frozen trusted route scope; nine opaque identity fields from RunRequest."""

    session_id: str
    connection_id: str
    connection_revision: int
    provider_id: str
    provider_model_id: str
    execution_mode: str
    endpoint_config_ref: str
    credential_ref: str
    continuation_handle: str

    def __post_init__(self):
        for name in _SCOPE_FIELDS:
            value = getattr(self, name)
            if name == "connection_revision":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ContinuationError(
                        "ContinuationRouteScope field %r must be an int." % name)
                continue
            if not isinstance(value, str) or not value:
                raise ContinuationError(
                    "ContinuationRouteScope field %r must be a non-empty string." % name)
        for name in ("endpoint_config_ref", "credential_ref", "continuation_handle"):
            if _OPAQUE_REF.fullmatch(getattr(self, name)) is None:
                raise ContinuationError(
                    "ContinuationRouteScope field %r must be an opaque reference." % name
                )

    def __repr__(self):
        parts = []
        for f in fields(self):
            if f.name == "credential_ref":
                value = "<opaque>"
            else:
                value = getattr(self, f.name)
            parts.append("%s=%r" % (f.name, value))
        return "ContinuationRouteScope(%s)" % ", ".join(parts)


@dataclass(frozen=True)
class ContinuationRecord:
    """Sanitized return value from the resolver API."""

    response_id: str
    item_ref: str
    identity_signature: str
    metadata: Mapping[str, Any]
    created_at: float
    item_index: int = 0
    expires_at: Optional[float] = None

    def __repr__(self):
        return (
            "ContinuationRecord(item_ref=%r, identity_signature=%r, "
            "created_at=%r, item_index=%r, expires_at=%r)"
            % (self.item_ref, self.identity_signature, self.created_at, self.item_index, self.expires_at)
        )


def _scope_to_jsonable(scope: ContinuationRouteScope) -> str:
    payload = {name: getattr(scope, name) for name in _SCOPE_FIELDS}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _scope_identity_hash(scope: ContinuationRouteScope) -> str:
    """Stable hash over all nine scope fields; binds a session to one route."""
    return hashlib.sha256(_scope_to_jsonable(scope).encode("utf-8")).hexdigest()


def _identity_signature(visible_item: Mapping[str, Any]) -> str:
    payload = json.dumps(_item_identity(dict(visible_item)), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_visible_item(visible_item: Mapping[str, Any]) -> None:
    if not isinstance(visible_item, Mapping):
        raise ContinuationError("visible_item must be a mapping.")
    try:
        _item_identity(dict(visible_item))
    except ContinuationError:
        raise
    except Exception:
        raise ContinuationError(
            "visible_item could not be normalized for the continuation store.") from None


class ContinuationStore:
    """Private, durable, session-scoped continuation store."""

    def __init__(self, path):
        if path is None:
            raise ContinuationError(
                "ContinuationStore requires an explicit database path.")
        self.path = Path(path)
        self._lock = threading.Lock()

    # -- private helpers -------------------------------------------------

    def _open_private(self):
        path = self.path
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            parent_status = path.parent.lstat()
            if (not stat.S_ISDIR(parent_status.st_mode)
                    or parent_status.st_uid != os.getuid()
                    or parent_status.st_mode & 0o022):
                raise ContinuationError(
                    "The continuation folder must be private and owned by the current user.")
            for suffix in ("", "-journal", "-wal", "-shm"):
                candidate = Path(str(path) + suffix)
                if candidate.is_symlink():
                    raise ContinuationError(
                        "The continuation store is not a regular file.")
            descriptor = os.open(
                path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                opened_status = os.fstat(descriptor)
                if (not stat.S_ISREG(opened_status.st_mode)
                        or opened_status.st_uid != os.getuid()
                        or opened_status.st_nlink != 1):
                    raise ContinuationError(
                        "The continuation store is not a private regular file.")
                os.fchmod(descriptor, 0o600)
                connection = sqlite3.connect(str(path), timeout=5)
                current_status = path.lstat()
                if ((current_status.st_dev, current_status.st_ino)
                        != (opened_status.st_dev, opened_status.st_ino)):
                    connection.close()
                    raise ContinuationError(
                        "The continuation store changed while opening it.")
                return connection
            finally:
                os.close(descriptor)
        except ContinuationError:
            raise
        except (sqlite3.Error, OSError):
            raise ContinuationError(
                "The continuation store could not be opened.") from None

    def _ensure_schema(self, connection):
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                scope_json TEXT NOT NULL,
                scope_hash TEXT NOT NULL,
                bound_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS records (
                session_id TEXT NOT NULL,
                item_ref TEXT NOT NULL,
                response_id TEXT NOT NULL,
                identity_signature TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                response_sequence INTEGER NOT NULL,
                item_index INTEGER NOT NULL,
                schema_version TEXT NOT NULL,
                PRIMARY KEY (session_id, item_ref),
                FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS pending_resets (
                session_id TEXT PRIMARY KEY,
                reset_token TEXT NOT NULL UNIQUE,
                prior_scope_hash TEXT NOT NULL,
                prior_handle TEXT NOT NULL,
                prepared_at REAL NOT NULL
            );
            """
        )
        existing = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,))
        elif existing[0] != SCHEMA_VERSION:
            raise ContinuationError(
                "The continuation store schema version is incompatible.")

    def _connect(self):
        connection = self._open_private()
        try:
            self._ensure_schema(connection)
            connection.commit()
        except (ContinuationError, sqlite3.Error):
            connection.close()
            raise
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _verify_schema(self, connection):
        stored_version = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        if stored_version is None or stored_version[0] != SCHEMA_VERSION:
            raise ContinuationError(
                "Required continuation is missing. Restore the continuation store or start a new task.")

    def _bind_session(self, connection, scope, scope_json, scope_hash, now):
        existing_session = connection.execute(
            "SELECT scope_hash FROM sessions WHERE session_id = ?",
            (scope.session_id,)).fetchone()
        if existing_session is None:
            connection.execute(
                "INSERT INTO sessions (session_id, scope_json, scope_hash, bound_at) "
                "VALUES (?, ?, ?, ?)",
                (scope.session_id, scope_json, scope_hash, now))
        elif existing_session[0] != scope_hash:
            if not self._retire_pending_reset(
                connection, scope.session_id, existing_session[0]
            ):
                raise ContinuationError(
                    "Session continuation scope does not match a previously bound route.")
            connection.execute(
                "INSERT INTO sessions (session_id, scope_json, scope_hash, bound_at) "
                "VALUES (?, ?, ?, ?)",
                (scope.session_id, scope_json, scope_hash, now),
            )

    @staticmethod
    def _retire_pending_reset(
        connection: sqlite3.Connection,
        session_id: str,
        current_scope_hash: str,
    ) -> bool:
        pending = connection.execute(
            "SELECT prior_scope_hash FROM pending_resets WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if pending is None or pending[0] != current_scope_hash:
            return False
        connection.execute("DELETE FROM records WHERE session_id = ?", (session_id,))
        connection.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        connection.execute("DELETE FROM pending_resets WHERE session_id = ?", (session_id,))
        return True

    @staticmethod
    def _response_sequence(connection, session_id: str, response_id: str) -> int:
        """Return one durable commit-order sequence for a provider response."""
        existing = connection.execute(
            "SELECT DISTINCT response_sequence FROM records "
            "WHERE session_id = ? AND response_id = ?",
            (session_id, response_id),
        ).fetchall()
        if len(existing) > 1:
            raise ContinuationError(
                "Required continuation is missing. Restore the continuation store or start a new task."
            )
        if existing:
            return int(existing[0][0])
        row = connection.execute(
            "SELECT COALESCE(MAX(response_sequence), 0) + 1 FROM records "
            "WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row[0])

    def _verify_session_strict(self, connection, scope, scope_hash):
        """Schema + strict scope match: both raise on mismatch, both raise on unbound."""
        self._verify_schema(connection)
        bound = connection.execute(
            "SELECT scope_hash FROM sessions WHERE session_id = ?",
            (scope.session_id,)).fetchone()
        if bound is None or bound[0] != scope_hash:
            raise ContinuationError(
                "This task's route scope changed and its continuation cannot be replayed safely.")

    def _verify_session_lenient(self, connection, scope, scope_hash):
        """Schema is strict; session is permissive: unbound returns False, mismatch raises."""
        self._verify_schema(connection)
        bound = connection.execute(
            "SELECT scope_hash FROM sessions WHERE session_id = ?",
            (scope.session_id,)).fetchone()
        if bound is None:
            return False
        if bound[0] != scope_hash:
            if self._retire_pending_reset(connection, scope.session_id, bound[0]):
                return False
            raise ContinuationError(
                "This task's route scope changed and its continuation cannot be replayed safely.")
        return True

    def _decode_metadata(self, metadata_json):
        if not isinstance(metadata_json, str) or len(metadata_json.encode("utf-8")) > MAX_RECORD_METADATA_BYTES:
            raise ContinuationError(
                "Required continuation is missing. Restore the continuation store or start a new task."
            )
        try:
            metadata = json.loads(metadata_json)
        except (TypeError, ValueError):
            raise ContinuationError(
                "Required continuation is missing. Restore the continuation store or start a new task.") from None
        if not isinstance(metadata, dict):
            raise ContinuationError(
                "Required continuation is missing. Restore the continuation store or start a new task.")
        return metadata

    def _build_record(self, item_ref, response_id, stored_sig, metadata_json, created_at, item_index=0):
        metadata = self._decode_metadata(metadata_json)
        return ContinuationRecord(
            response_id=response_id,
            item_ref=item_ref,
            identity_signature=stored_sig,
            metadata=metadata,
            created_at=created_at,
            item_index=item_index,
        )

    @staticmethod
    def _encode_metadata(metadata: Mapping[str, Any]) -> str:
        try:
            encoded = json.dumps(
                dict(metadata), sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise ContinuationError("metadata is not JSON-encodable.") from None
        if len(encoded.encode("utf-8")) > MAX_RECORD_METADATA_BYTES:
            raise ContinuationError("continuation metadata is too large.")
        return encoded

    # -- public API ------------------------------------------------------

    def save(
        self,
        scope: ContinuationRouteScope,
        response_id: str,
        item_ref: str,
        visible_item: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> None:
        if not isinstance(scope, ContinuationRouteScope):
            raise ContinuationError("scope must be a ContinuationRouteScope.")
        if not isinstance(response_id, str) or not response_id:
            raise ContinuationError("response_id must be a non-empty string.")
        if not isinstance(item_ref, str) or not item_ref:
            raise ContinuationError("item_ref must be a non-empty string.")
        if not isinstance(metadata, Mapping):
            raise ContinuationError("metadata must be a mapping.")
        _validate_visible_item(visible_item)
        metadata_json = self._encode_metadata(metadata)
        identity_sig = _identity_signature(visible_item)
        scope_json = _scope_to_jsonable(scope)
        scope_hash = _scope_identity_hash(scope)
        now = time.time()
        with self._lock:
            try:
                connection = self._connect()
            except ContinuationError:
                raise
            except (sqlite3.Error, OSError):
                raise ContinuationError(
                    "Continuation could not be saved locally. The response was not completed.") from None
            try:
                try:
                    with connection:
                        self._bind_session(connection, scope, scope_json, scope_hash, now)
                        response_sequence = self._response_sequence(
                            connection, scope.session_id, response_id
                        )
                        existing_row = connection.execute(
                            "SELECT response_id, identity_signature, metadata_json FROM records "
                            "WHERE session_id = ? AND item_ref = ?",
                            (scope.session_id, item_ref)).fetchone()
                        if existing_row is not None:
                            prior = (existing_row[0], existing_row[1], existing_row[2])
                            new = (response_id, identity_sig, metadata_json)
                            if prior != new:
                                raise ContinuationError(
                                    "The provider attempted to replace an existing continuation record.")
                        else:
                            connection.execute(
                                "INSERT INTO records (session_id, item_ref, response_id, "
                                "identity_signature, metadata_json, created_at, "
                                "response_sequence, item_index, schema_version) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (scope.session_id, item_ref, response_id, identity_sig,
                                 metadata_json, now, response_sequence, 0, SCHEMA_VERSION))
                except ContinuationError:
                    raise
                except sqlite3.Error:
                    raise ContinuationError(
                        "Continuation could not be saved locally. The response was not completed.") from None
            finally:
                connection.close()

    def save_response(
        self,
        scope: ContinuationRouteScope,
        response_id: str,
        items: Sequence[Tuple[str, Mapping[str, Any], Mapping[str, Any]]],
    ) -> None:
        """Atomically persist every item from one completed response.

        ``items`` is a sequence of ``(item_ref, visible_item, metadata)`` tuples.
        Either every record installs in one transaction or none does — callers
        can call this once per response after the response has fully streamed
        and guarantee atomicity. Duplicate exact (item_ref, response_id,
        identity, metadata) entries are idempotent; any conflicting replacement
        (different response_id / identity / metadata under the same item_ref)
        raises ``ContinuationError``.
        """
        if not isinstance(scope, ContinuationRouteScope):
            raise ContinuationError("scope must be a ContinuationRouteScope.")
        if not isinstance(response_id, str) or not response_id:
            raise ContinuationError("response_id must be a non-empty string.")
        if not isinstance(items, (list, tuple)):
            raise ContinuationError("items must be a sequence of (item_ref, visible_item, metadata) tuples.")
        if not items:
            return
        if len(items) > MAX_RESPONSE_ITEMS:
            raise ContinuationError("continuation response has too many items.")
        prepared = []
        total_metadata_bytes = 0
        for item_index, entry in enumerate(items):
            if not isinstance(entry, (list, tuple)) or len(entry) != 3:
                raise ContinuationError(
                    "Each item must be a (item_ref, visible_item, metadata) tuple.")
            item_ref, visible_item, metadata = entry
            if not isinstance(item_ref, str) or not item_ref:
                raise ContinuationError("item_ref must be a non-empty string.")
            if not isinstance(metadata, Mapping):
                raise ContinuationError("metadata must be a mapping.")
            _validate_visible_item(visible_item)
            metadata_json = self._encode_metadata(metadata)
            total_metadata_bytes += len(metadata_json.encode("utf-8"))
            if total_metadata_bytes > MAX_RESPONSE_METADATA_BYTES:
                raise ContinuationError("continuation response metadata is too large.")
            prepared.append((item_ref, _identity_signature(visible_item), metadata_json, item_index))
        scope_json = _scope_to_jsonable(scope)
        scope_hash = _scope_identity_hash(scope)
        now = time.time()
        with self._lock:
            try:
                connection = self._connect()
            except ContinuationError:
                raise
            except (sqlite3.Error, OSError):
                raise ContinuationError(
                    "Continuation could not be saved locally. The response was not completed.") from None
            try:
                try:
                    with connection:
                        self._bind_session(connection, scope, scope_json, scope_hash, now)
                        response_sequence = self._response_sequence(
                            connection, scope.session_id, response_id
                        )
                        for item_ref, identity_sig, metadata_json, item_index in prepared:
                            existing_row = connection.execute(
                                "SELECT response_id, identity_signature, metadata_json FROM records "
                                "WHERE session_id = ? AND item_ref = ?",
                                (scope.session_id, item_ref)).fetchone()
                            if existing_row is not None:
                                prior = (existing_row[0], existing_row[1], existing_row[2])
                                new = (response_id, identity_sig, metadata_json)
                                if prior != new:
                                    raise ContinuationError(
                                        "The provider attempted to replace an existing continuation record.")
                            else:
                                connection.execute(
                                    "INSERT INTO records (session_id, item_ref, response_id, "
                                    "identity_signature, metadata_json, created_at, "
                                    "response_sequence, item_index, schema_version) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                    (scope.session_id, item_ref, response_id, identity_sig,
                                     metadata_json, now, response_sequence, item_index, SCHEMA_VERSION))
                except ContinuationError:
                    raise
                except sqlite3.Error:
                    raise ContinuationError(
                        "Continuation could not be saved locally. The response was not completed.") from None
            finally:
                connection.close()

    def load(
        self,
        scope: ContinuationRouteScope,
        item_ref: str,
        visible_item: Mapping[str, Any],
    ) -> ContinuationRecord:
        if not isinstance(scope, ContinuationRouteScope):
            raise ContinuationError("scope must be a ContinuationRouteScope.")
        if not isinstance(item_ref, str) or not item_ref:
            raise ContinuationError("item_ref must be a non-empty string.")
        _validate_visible_item(visible_item)
        scope_hash = _scope_identity_hash(scope)
        identity_sig = _identity_signature(visible_item)
        with self._lock:
            try:
                connection = self._connect()
            except ContinuationError:
                raise
            except (sqlite3.Error, OSError):
                raise ContinuationError(
                    "The local continuation store could not be read.") from None
            try:
                try:
                    self._verify_session_strict(connection, scope, scope_hash)
                    row = connection.execute(
                        "SELECT response_id, identity_signature, metadata_json, "
                        "schema_version, created_at, item_index FROM records "
                        "WHERE session_id = ? AND item_ref = ?",
                        (scope.session_id, item_ref)).fetchone()
                except ContinuationError:
                    raise
                except sqlite3.Error:
                    raise ContinuationError(
                        "The local continuation store could not be read.") from None
            finally:
                connection.close()
        if row is None:
            raise ContinuationError(
                "Required continuation is missing. Restore the continuation store or start a new task.")
        if row[3] != SCHEMA_VERSION:
            raise ContinuationError(
                "Required continuation is missing. Restore the continuation store or start a new task."
            )
        record = self._build_record(item_ref, row[0], row[1], row[2], row[4], row[5])
        if record.identity_signature != identity_sig:
            raise ContinuationError(
                "This task's provider history changed and its continuation cannot be replayed safely.")
        return record

    def load_for_item(
        self,
        scope: ContinuationRouteScope,
        visible_item: Mapping[str, Any],
    ) -> ContinuationRecord:
        """Find the bound record whose identity matches ``visible_item``.

        V2 normalized history strips provider item IDs, so a next-run load
        has only the visible-item content. We match by ``identity_signature``
        within the session's scope. An ambiguous collision (two records with
        the same identity in the same session) is rejected explicitly —
        ``clear(scope)`` first, then re-save, before retrying.
        """
        if not isinstance(scope, ContinuationRouteScope):
            raise ContinuationError("scope must be a ContinuationRouteScope.")
        _validate_visible_item(visible_item)
        scope_hash = _scope_identity_hash(scope)
        identity_sig = _identity_signature(visible_item)
        with self._lock:
            try:
                connection = self._connect()
            except ContinuationError:
                raise
            except (sqlite3.Error, OSError):
                raise ContinuationError(
                    "The local continuation store could not be read.") from None
            try:
                try:
                    self._verify_session_strict(connection, scope, scope_hash)
                    rows = connection.execute(
                        "SELECT item_ref, response_id, identity_signature, "
                        "metadata_json, schema_version, created_at, item_index FROM records "
                        "WHERE session_id = ? AND identity_signature = ?",
                        (scope.session_id, identity_sig)).fetchall()
                except ContinuationError:
                    raise
                except sqlite3.Error:
                    raise ContinuationError(
                        "The local continuation store could not be read.") from None
            finally:
                connection.close()
        if not rows:
            raise ContinuationError(
                "Required continuation is missing. Restore the continuation store or start a new task.")
        if len(rows) > 1:
            raise ContinuationError(
                "Multiple continuation records share this identity; clear the session and re-save.")
        item_ref, response_id, stored_sig, metadata_json, record_version, created_at, item_index = rows[0]
        if record_version != SCHEMA_VERSION:
            raise ContinuationError(
                "Required continuation is missing. Restore the continuation store or start a new task."
            )
        metadata = self._decode_metadata(metadata_json)
        return ContinuationRecord(
            response_id=response_id,
            item_ref=item_ref,
            identity_signature=stored_sig,
            metadata=metadata,
            created_at=created_at,
            item_index=item_index,
        )

    def load_all(
        self,
        scope: ContinuationRouteScope,
    ) -> list[ContinuationRecord]:
        """Return every record bound to ``scope.session_id`` under this scope.

        Returns ``[]`` for an unbound first-turn session (the caller has not
        yet saved anything). Still rejects scope mismatch and a
        corrupt/incompatible schema, so a stale or hijacked store cannot
        silently expose unrelated state. Order follows the store's durable
        response commit sequence and the provider's item order within it.
        """
        if not isinstance(scope, ContinuationRouteScope):
            raise ContinuationError("scope must be a ContinuationRouteScope.")
        scope_hash = _scope_identity_hash(scope)
        with self._lock:
            try:
                connection = self._connect()
            except ContinuationError:
                raise
            except (sqlite3.Error, OSError):
                raise ContinuationError(
                    "The local continuation store could not be read.") from None
            try:
                try:
                    with connection:
                        bound = self._verify_session_lenient(connection, scope, scope_hash)
                        if not bound:
                            return []
                        rows = connection.execute(
                            "SELECT item_ref, response_id, identity_signature, "
                            "metadata_json, schema_version, created_at, item_index FROM records "
                            "WHERE session_id = ? "
                            "ORDER BY response_sequence ASC, item_index ASC",
                            (scope.session_id,)).fetchall()
                except ContinuationError:
                    raise
                except sqlite3.Error:
                    raise ContinuationError(
                        "The local continuation store could not be read.") from None
            finally:
                connection.close()
        records: list[ContinuationRecord] = []
        for item_ref, response_id, stored_sig, metadata_json, record_version, created_at, item_index in rows:
            if record_version != SCHEMA_VERSION:
                raise ContinuationError(
                    "Required continuation is missing. Restore the continuation store or start a new task."
                )
            metadata = self._decode_metadata(metadata_json)
            records.append(ContinuationRecord(
                response_id=response_id,
                item_ref=item_ref,
                identity_signature=stored_sig,
                metadata=metadata,
                created_at=created_at,
                item_index=item_index,
            ))
        return records

    def clear(
        self,
        scope: ContinuationRouteScope,
    ) -> None:
        """Drop the session binding and every record under it.

        Idempotent for an unbound session (no-op). Rejects scope mismatch
        and a corrupt/incompatible schema, so callers cannot accidentally
        delete state that does not belong to this scope.
        """
        if not isinstance(scope, ContinuationRouteScope):
            raise ContinuationError("scope must be a ContinuationRouteScope.")
        scope_hash = _scope_identity_hash(scope)
        with self._lock:
            try:
                connection = self._connect()
            except ContinuationError:
                raise
            except (sqlite3.Error, OSError):
                raise ContinuationError(
                    "The local continuation store could not be read.") from None
            try:
                try:
                    self._verify_schema(connection)
                    bound = connection.execute(
                        "SELECT scope_hash FROM sessions WHERE session_id = ?",
                        (scope.session_id,)).fetchone()
                    if bound is None:
                        return
                    if bound[0] != scope_hash:
                        raise ContinuationError(
                            "This task's route scope changed and its continuation cannot be replayed safely.")
                    with connection:
                        connection.execute(
                            "DELETE FROM records WHERE session_id = ?",
                            (scope.session_id,))
                        connection.execute(
                            "DELETE FROM sessions WHERE session_id = ?",
                            (scope.session_id,))
                except ContinuationError:
                    raise
                except sqlite3.Error:
                    raise ContinuationError(
                        "The local continuation store could not be read.") from None
            finally:
                connection.close()

    def prepare_session_reset(
        self, session_id: str, continuation_handle: str
    ) -> str | None:
        """Persist reset intent while leaving old continuation recoverable."""
        if not isinstance(session_id, str) or not session_id:
            raise ContinuationError("session_id must be a non-empty string.")
        if (
            not isinstance(continuation_handle, str)
            or _OPAQUE_REF.fullmatch(continuation_handle) is None
        ):
            raise ContinuationError("continuation_handle must be an opaque reference.")
        with self._lock:
            try:
                connection = self._connect()
            except ContinuationError:
                raise
            except (sqlite3.Error, OSError):
                raise ContinuationError(
                    "The local continuation store could not be read."
                ) from None
            try:
                try:
                    self._verify_schema(connection)
                    row = connection.execute(
                        "SELECT scope_json, scope_hash FROM sessions WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()
                    if row is None:
                        return None
                    try:
                        stored_scope = json.loads(row[0])
                    except (TypeError, ValueError):
                        raise ContinuationError(
                            "Required continuation is missing. Restore the continuation store or start a new task."
                        ) from None
                    if (
                        not isinstance(stored_scope, dict)
                        or stored_scope.get("session_id") != session_id
                        or stored_scope.get("continuation_handle") != continuation_handle
                    ):
                        raise ContinuationError(
                            "This task's route scope changed and its continuation cannot be reset safely."
                        )
                    reset_token = "reset_" + secrets.token_urlsafe(24)
                    with connection:
                        connection.execute(
                            "INSERT INTO pending_resets "
                            "(session_id, reset_token, prior_scope_hash, prior_handle, prepared_at) "
                            "VALUES (?, ?, ?, ?, ?) "
                            "ON CONFLICT(session_id) DO UPDATE SET "
                            "reset_token = excluded.reset_token, "
                            "prior_scope_hash = excluded.prior_scope_hash, "
                            "prior_handle = excluded.prior_handle, "
                            "prepared_at = excluded.prepared_at",
                            (
                                session_id,
                                reset_token,
                                row[1],
                                continuation_handle,
                                time.time(),
                            ),
                        )
                    return reset_token
                except ContinuationError:
                    raise
                except sqlite3.Error:
                    raise ContinuationError(
                        "The local continuation store could not be read."
                    ) from None
            finally:
                connection.close()

    def commit_session_reset(self, reset_token: str) -> None:
        """Retire old state after the application session commit succeeds."""
        self._finish_session_reset(reset_token, commit=True)

    def rollback_session_reset(self, reset_token: str) -> None:
        """Discard reset intent while preserving the old bound state."""
        self._finish_session_reset(reset_token, commit=False)

    def _finish_session_reset(self, reset_token: str, *, commit: bool) -> None:
        if not isinstance(reset_token, str) or not reset_token.startswith("reset_"):
            raise ContinuationError("reset_token must be an opaque reset reference.")
        with self._lock:
            try:
                connection = self._connect()
            except ContinuationError:
                raise
            except (sqlite3.Error, OSError):
                raise ContinuationError(
                    "The local continuation store could not be read."
                ) from None
            try:
                try:
                    self._verify_schema(connection)
                    pending = connection.execute(
                        "SELECT session_id, prior_scope_hash FROM pending_resets "
                        "WHERE reset_token = ?",
                        (reset_token,),
                    ).fetchone()
                    if pending is None:
                        return
                    with connection:
                        if commit:
                            bound = connection.execute(
                                "SELECT scope_hash FROM sessions WHERE session_id = ?",
                                (pending[0],),
                            ).fetchone()
                            if bound is not None and bound[0] != pending[1]:
                                raise ContinuationError(
                                    "This task's continuation reset no longer matches its stored route."
                                )
                            connection.execute(
                                "DELETE FROM records WHERE session_id = ?", (pending[0],)
                            )
                            connection.execute(
                                "DELETE FROM sessions WHERE session_id = ?", (pending[0],)
                            )
                        connection.execute(
                            "DELETE FROM pending_resets WHERE reset_token = ?",
                            (reset_token,),
                        )
                except ContinuationError:
                    raise
                except sqlite3.Error:
                    raise ContinuationError(
                        "The local continuation store could not be read."
                    ) from None
            finally:
                connection.close()


__all__ = [
    "ContinuationError",
    "ContinuationRecord",
    "ContinuationRouteScope",
    "ContinuationStore",
    "MAX_RECORD_METADATA_BYTES",
    "MAX_RESPONSE_ITEMS",
    "MAX_RESPONSE_METADATA_BYTES",
    "SCHEMA_VERSION",
]

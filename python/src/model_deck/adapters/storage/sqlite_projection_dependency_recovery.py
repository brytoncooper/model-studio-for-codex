"""Bounded recovery of historical connection projection invalidations."""
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3

from model_deck.adapters.storage.sqlite_model_projection_invalidation import invalidate_connection_models
from model_deck.adapters.storage.sqlite_model_schema import ensure_model_schema
from model_deck.adapters.storage.sqlite_projection_dependency_expansions import (
    ensure_dependency_expansions_schema, record_connection_expansion,
)
from model_deck.engine.projections.ports import OUTBOX_LIMIT_DEFAULT, validate_outbox_limit


@dataclass(frozen=True)
class DependencyRecoveryConflict:
    outbox_id: int
    reason: str


@dataclass(frozen=True)
class DependencyRecoveryBatch:
    expanded_outbox_ids: tuple[int, ...]
    conflicts: tuple[DependencyRecoveryConflict, ...]


class DependencyRecoveryError(RuntimeError):
    def __init__(self):
        super().__init__("dependency recovery failed")


_CANDIDATES = """
SELECT o.outbox_id, o.aggregate_id, o.aggregate_revision, o.payload_json
FROM projection_outbox o
WHERE o.state = 'pending' AND o.aggregate_type = 'connection' AND o.event_kind = 'connection.saved'
AND NOT EXISTS (
    SELECT 1 FROM projection_dependency_expansions e WHERE e.outbox_id = o.outbox_id
    AND e.connection_id = o.aggregate_id AND e.connection_revision = o.aggregate_revision
    AND e.payload_json = o.payload_json
)
ORDER BY o.outbox_id LIMIT ?
"""


def _valid_fields(provider, endpoint, credential) -> bool:
    return (type(provider) is str and bool(provider)
            and all(value is None or (type(value) is str and bool(value)) for value in (endpoint, credential)))


def _source_valid(row) -> bool:
    _, identifier, revision, raw = row
    try:
        payload = json.loads(raw)
        return (type(identifier) is str and bool(identifier) and type(revision) is int and revision >= 1
                and type(payload) is dict
                and set(payload) == {"connection_id", "revision", "provider_id", "endpoint_config_ref", "credential_ref"}
                and json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) == raw
                and payload["connection_id"] == identifier and type(payload["revision"]) is int
                and payload["revision"] == revision
                and _valid_fields(payload["provider_id"], payload["endpoint_config_ref"], payload["credential_ref"]))
    except (ValueError, TypeError, RecursionError):
        return False


def recover_connection_dependencies(db_path: Path, *, limit: int = OUTBOX_LIMIT_DEFAULT) -> DependencyRecoveryBatch:
    """Expand valid historical events against current committed desired state.

    One write transaction groups selected events by connection and invalidates
    each group's active models once. Source receipts remain bound to historical
    rows; the newly queued upserts describe current desired state. Conflicts
    remain pending. This never writes projection files or marks them applied.
    """
    validate_outbox_limit(limit)
    connection = None
    try:
        # mode=rw refuses to create an unrelated empty database by accident.
        connection = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=rw", uri=True)
        connection.execute("PRAGMA foreign_keys = ON")
        ensure_model_schema(connection)
        ensure_dependency_expansions_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            selected = connection.execute(_CANDIDATES, (limit,)).fetchall()
            groups: dict[str, list[int]] = {}
            conflicts = []
            # Validate every selected source/proof/current dependency before the
            # first model mutation, so malformed siblings cannot be half-applied.
            for row in selected:
                outbox_id, identifier, revision, _ = row
                reason = None
                if not _source_valid(row):
                    reason = "event_invalid"
                elif connection.execute("SELECT 1 FROM projection_dependency_expansions WHERE outbox_id = ?", (outbox_id,)).fetchone():
                    reason = "proof_conflict"
                else:
                    current = connection.execute(
                        "SELECT revision, provider_id, endpoint_config_ref, credential_ref FROM connections WHERE connection_id = ?",
                        (identifier,),
                    ).fetchone()
                    if current is None:
                        reason = "connection_missing"
                    elif type(current[0]) is not int or current[0] < 1 or not _valid_fields(*current[1:]):
                        reason = "connection_invalid"
                    elif current[0] < revision:
                        reason = "connection_revision_future"
                if reason is not None:
                    conflicts.append(DependencyRecoveryConflict(outbox_id, reason))
                else:
                    groups.setdefault(identifier, []).append(outbox_id)
            expanded = []
            for identifier, outbox_ids in groups.items():
                models = invalidate_connection_models(connection, connection_id=identifier)
                for outbox_id in outbox_ids:
                    record_connection_expansion(connection, outbox_id=outbox_id, expanded_models=models)
                    expanded.append(outbox_id)
            connection.commit()
            return DependencyRecoveryBatch(tuple(sorted(expanded)), tuple(conflicts))
        except Exception:
            connection.rollback()
            raise
    except Exception:
        raise DependencyRecoveryError() from None
    finally:
        if connection is not None:
            connection.close()

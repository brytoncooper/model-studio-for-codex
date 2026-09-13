"""SQLite-backed activation state for plugin authority decisions."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from model_deck.engine.extensions.ports import SelectedInstallation
from model_deck.engine.plugin_authority import (
    ActivationIdentity,
    ActivationState,
    OperationAuthority,
    OriginState,
)


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS plugin_activation_authority (
    identity_key TEXT PRIMARY KEY,
    engine_instance_id TEXT NOT NULL,
    audience TEXT NOT NULL,
    activation_id TEXT NOT NULL,
    plugin_id TEXT NOT NULL,
    plugin_version TEXT NOT NULL,
    selection_json TEXT NOT NULL,
    effects_json TEXT NOT NULL,
    resource_scopes_json TEXT NOT NULL,
    capability_grants_json TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revocation_generation INTEGER NOT NULL CHECK (revocation_generation >= 0),
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    revoked INTEGER NOT NULL CHECK (revoked IN (0, 1)),
    runtime_epoch TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plugin_activation_selection (
    selection_key TEXT PRIMARY KEY,
    selection_json TEXT NOT NULL,
    identity_key TEXT NOT NULL,
    FOREIGN KEY (identity_key) REFERENCES plugin_activation_authority(identity_key)
);
"""

ActivationPolicy = Callable[
    [ActivationIdentity, tuple[str, ...]],
    ActivationState,
]
MutationGuard = Callable[[], AbstractContextManager[None]]


class ActivationAuthorityConfigurationError(ValueError):
    def __init__(self) -> None:
        super().__init__("invalid activation authority configuration")


class ActivationAuthorityConflictError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("activation authority conflict")


def _nonempty(value: object) -> str:
    if type(value) is not str or not value:
        raise ActivationAuthorityConfigurationError()
    return value


def _identity_document(identity: ActivationIdentity) -> dict[str, str]:
    if type(identity) is not ActivationIdentity:
        raise ActivationAuthorityConflictError()
    values = {
        "engine_instance_id": identity.engine_instance_id,
        "audience": identity.audience,
        "activation_id": identity.activation_id,
        "plugin_id": identity.plugin_id,
        "plugin_version": identity.plugin_version,
    }
    if any(type(value) is not str or not value for value in values.values()):
        raise ActivationAuthorityConflictError()
    return values


def _identity_key(identity: ActivationIdentity) -> str:
    document = _identity_document(identity)
    encoded = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _selection_json(selected: SelectedInstallation) -> str:
    if type(selected) is not SelectedInstallation:
        raise ActivationAuthorityConflictError()
    executable = selected.executable
    document = {
        "activation_generation": selected.activation_generation,
        "approved_scopes": list(selected.approved_scopes),
        "data_ref": selected.data_ref,
        "executable": {
            "artifact_id": executable.artifact_id,
            "extension_id": executable.extension_id,
            "requested_scopes": list(executable.requested_scopes),
            "version": executable.version,
        },
        "grant_generation": selected.grant_generation,
    }
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _selection_key(selection_json: str) -> str:
    return hashlib.sha256(selection_json.encode("utf-8")).hexdigest()


def _permission_json(values: frozenset[str]) -> str:
    if type(values) is not frozenset or any(
        type(value) is not str or not value for value in values
    ):
        raise ActivationAuthorityConfigurationError()
    return json.dumps(sorted(values), separators=(",", ":"))


def _policy_values(
    policy: ActivationPolicy,
    identity: ActivationIdentity,
    approved_scopes: tuple[str, ...],
) -> tuple[str, str, str, str]:
    try:
        state = policy(identity, approved_scopes)
    except Exception:
        raise ActivationAuthorityConfigurationError() from None
    if type(state) is not ActivationState or state.identity != identity:
        raise ActivationAuthorityConfigurationError()
    expiry = state.expires_at
    if (
        not isinstance(expiry, datetime)
        or expiry.tzinfo is None
        or expiry.utcoffset() is None
    ):
        raise ActivationAuthorityConfigurationError()
    return (
        _permission_json(state.effects),
        _permission_json(state.resource_scopes),
        _permission_json(state.capability_grants),
        expiry.isoformat(),
    )


def _identity_from_row(row: sqlite3.Row) -> ActivationIdentity:
    return ActivationIdentity(
        row["engine_instance_id"],
        row["audience"],
        row["activation_id"],
        row["plugin_id"],
        row["plugin_version"],
    )


class SQLiteActivationAuthorityController:
    """Own trusted activation admission while delegating origin/operation reads.

    The SQLite mapping is durable cleanup evidence. Serving authority also binds
    to this controller instance's ``runtime_epoch`` and therefore fails closed
    after construction of a replacement controller, including after restart.
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        engine_instance_id: str,
        audience: str,
        activation_policy: ActivationPolicy,
        origin_reader: Callable[[str], OriginState | None],
        operation_reader: Callable[[str], OperationAuthority | None],
        mutation_guard: MutationGuard,
    ) -> None:
        try:
            path = Path(db_path)
        except TypeError:
            raise ActivationAuthorityConfigurationError() from None
        if (
            not callable(activation_policy)
            or not callable(origin_reader)
            or not callable(operation_reader)
            or not callable(mutation_guard)
        ):
            raise ActivationAuthorityConfigurationError()
        self._db_path = path
        self._engine_instance_id = _nonempty(engine_instance_id)
        self._audience = _nonempty(audience)
        self._activation_policy = activation_policy
        self._origin_reader = origin_reader
        self._operation_reader = operation_reader
        self._mutation_guard = mutation_guard
        self._runtime_epoch = str(uuid.uuid4())
        with self._mutation_guard():
            connection = self._connect()
            try:
                self._ensure_schema(connection)
            finally:
                connection.close()

    def register_non_serving(
        self,
        identity: ActivationIdentity,
        selected: SelectedInstallation,
    ) -> None:
        identity_key = _identity_key(identity)
        selection_json = _selection_json(selected)
        self._require_current_identity(identity, selected)
        policy_values = _policy_values(
            self._activation_policy,
            identity,
            selected.approved_scopes,
        )
        selection_key = _selection_key(selection_json)
        with self._mutation_guard():
            connection = self._connect()
            try:
                self._ensure_schema(connection)
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM plugin_activation_authority WHERE identity_key = ?",
                    (identity_key,),
                ).fetchone()
                if existing is not None:
                    if not self._same_non_serving_registration(
                        existing,
                        identity,
                        selection_json,
                        policy_values,
                    ):
                        raise ActivationAuthorityConflictError()
                    connection.commit()
                    return
                mapped = connection.execute(
                    "SELECT s.selection_json, s.identity_key, a.revoked "
                    "FROM plugin_activation_selection AS s "
                    "JOIN plugin_activation_authority AS a "
                    "ON a.identity_key = s.identity_key "
                    "WHERE s.selection_key = ?",
                    (selection_key,),
                ).fetchone()
                if mapped is not None:
                    if mapped["selection_json"] != selection_json:
                        raise ActivationAuthorityConflictError()
                    if mapped["identity_key"] != identity_key and mapped["revoked"] != 1:
                        raise ActivationAuthorityConflictError()
                identity_document = _identity_document(identity)
                connection.execute(
                    "INSERT INTO plugin_activation_authority "
                    "(identity_key, engine_instance_id, audience, activation_id, "
                    "plugin_id, plugin_version, selection_json, effects_json, "
                    "resource_scopes_json, capability_grants_json, expires_at, "
                    "revocation_generation, active, revoked, runtime_epoch) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?)",
                    (
                        identity_key,
                        identity_document["engine_instance_id"],
                        identity_document["audience"],
                        identity_document["activation_id"],
                        identity_document["plugin_id"],
                        identity_document["plugin_version"],
                        selection_json,
                        *policy_values,
                        self._runtime_epoch,
                    ),
                )
                connection.execute(
                    "INSERT INTO plugin_activation_selection "
                    "(selection_key, selection_json, identity_key) VALUES (?, ?, ?) "
                    "ON CONFLICT(selection_key) DO UPDATE SET "
                    "selection_json = excluded.selection_json, "
                    "identity_key = excluded.identity_key",
                    (selection_key, selection_json, identity_key),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def revoke(self, identity: ActivationIdentity) -> None:
        identity_key = _identity_key(identity)
        with self._mutation_guard():
            connection = self._connect()
            try:
                self._ensure_schema(connection)
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM plugin_activation_authority "
                    "WHERE identity_key = ?",
                    (identity_key,),
                ).fetchone()
                if row is None:
                    raise ActivationAuthorityConflictError()
                if _identity_from_row(row) != identity:
                    raise ActivationAuthorityConflictError()
                if row["revoked"] != 1:
                    connection.execute(
                        "UPDATE plugin_activation_authority SET "
                        "active = 0, revoked = 1, "
                        "revocation_generation = revocation_generation + 1 "
                        "WHERE identity_key = ? AND revoked = 0",
                        (identity_key,),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def admit(
        self,
        identity: ActivationIdentity,
        selected: SelectedInstallation,
    ) -> None:
        identity_key = _identity_key(identity)
        selection_json = _selection_json(selected)
        self._require_current_identity(identity, selected)
        selection_key = _selection_key(selection_json)
        with self._mutation_guard():
            connection = self._connect()
            try:
                self._ensure_schema(connection)
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM plugin_activation_authority WHERE identity_key = ?",
                    (identity_key,),
                ).fetchone()
                mapped = connection.execute(
                    "SELECT selection_json, identity_key "
                    "FROM plugin_activation_selection WHERE selection_key = ?",
                    (selection_key,),
                ).fetchone()
                if (
                    row is None
                    or _identity_from_row(row) != identity
                    or row["selection_json"] != selection_json
                    or row["revoked"] != 0
                    or row["runtime_epoch"] != self._runtime_epoch
                    or mapped is None
                    or mapped["selection_json"] != selection_json
                    or mapped["identity_key"] != identity_key
                ):
                    raise ActivationAuthorityConflictError()
                connection.execute(
                    "UPDATE plugin_activation_authority SET active = 1 "
                    "WHERE identity_key = ?",
                    (identity_key,),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def identity_for(
        self,
        selected: SelectedInstallation,
    ) -> ActivationIdentity | None:
        selection_json = _selection_json(selected)
        selection_key = _selection_key(selection_json)
        with self._mutation_guard():
            connection = self._connect()
            try:
                self._ensure_schema(connection)
                row = connection.execute(
                    "SELECT a.* FROM plugin_activation_selection AS s "
                    "JOIN plugin_activation_authority AS a "
                    "ON a.identity_key = s.identity_key "
                    "WHERE s.selection_key = ? AND s.selection_json = ?",
                    (selection_key, selection_json),
                ).fetchone()
                return None if row is None else _identity_from_row(row)
            finally:
                connection.close()

    def activation(self, identity: ActivationIdentity) -> ActivationState | None:
        identity_key = _identity_key(identity)
        with self._mutation_guard():
            connection = self._connect()
            try:
                self._ensure_schema(connection)
                row = connection.execute(
                    "SELECT * FROM plugin_activation_authority WHERE identity_key = ?",
                    (identity_key,),
                ).fetchone()
                if row is None or _identity_from_row(row) != identity:
                    return None
                serving = (
                    row["active"] == 1
                    and row["revoked"] == 0
                    and row["runtime_epoch"] == self._runtime_epoch
                    and identity.engine_instance_id == self._engine_instance_id
                    and identity.audience == self._audience
                )
                return ActivationState(
                    identity,
                    frozenset(json.loads(row["effects_json"])),
                    frozenset(json.loads(row["resource_scopes_json"])),
                    frozenset(json.loads(row["capability_grants_json"])),
                    datetime.fromisoformat(row["expires_at"]),
                    row["revocation_generation"],
                    active=serving,
                )
            finally:
                connection.close()

    def origin(self, principal_id: str) -> OriginState | None:
        with self._mutation_guard():
            return self._origin_reader(principal_id)

    def operation(self, operation_id: str) -> OperationAuthority | None:
        with self._mutation_guard():
            return self._operation_reader(operation_id)

    def _require_current_identity(
        self,
        identity: ActivationIdentity,
        selected: SelectedInstallation,
    ) -> None:
        if (
            identity.engine_instance_id != self._engine_instance_id
            or identity.audience != self._audience
            or identity.plugin_id != selected.executable.extension_id
            or identity.plugin_version != selected.executable.version
        ):
            raise ActivationAuthorityConflictError()

    def _same_non_serving_registration(
        self,
        row: sqlite3.Row,
        identity: ActivationIdentity,
        selection_json: str,
        policy_values: tuple[str, str, str, str],
    ) -> bool:
        return (
            _identity_from_row(row) == identity
            and row["selection_json"] == selection_json
            and row["effects_json"] == policy_values[0]
            and row["resource_scopes_json"] == policy_values[1]
            and row["capability_grants_json"] == policy_values[2]
            and row["expires_at"] == policy_values[3]
            and row["revoked"] == 0
            and row["active"] == 0
            and row["runtime_epoch"] == self._runtime_epoch
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            return connection
        except Exception:
            connection.close()
            raise

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(_SCHEMA_SQL)

"""Generation-bound SQLite storage for extension lifecycle data."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from model_deck.adapters.storage.sqlite_plugin_data import (
    canonicalize_plugin_data_value,
    enforce_plugin_data_quota,
    plugin_data_prefix_upper,
    validate_plugin_data_expected_revision,
    validate_plugin_data_key,
    validate_plugin_data_limit,
    validate_plugin_data_namespace,
    validate_plugin_data_prefix,
    validate_plugin_data_quota,
    validate_plugin_data_value_size,
)
from model_deck.engine.extensions.ports import (
    ExecutableArtifact,
    FrozenData,
    LifecycleConflictError,
    SelectedInstallation,
    StagedData,
)
from model_deck.engine.plugin_data.ports import (
    PluginDataEntry,
    PluginDataListItem,
    PluginDataNotFoundError,
    PluginDataQuota,
    PluginDataRevisionConflictError,
)
from model_deck.engine.plugin_data.versioning import (
    PluginDataBinding,
    PluginDataMigration,
    PluginDataVersioningContractError,
)
from model_deck_contracts.validator import SchemaValidationError, validate_schema_ref


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS versioned_plugin_data_generations (
    data_ref TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    activation_generation INTEGER,
    dataset_revision INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('staging', 'sealed', 'selected', 'retained')),
    writable INTEGER NOT NULL CHECK (writable IN (0, 1)),
    frozen INTEGER NOT NULL CHECK (frozen IN (0, 1)),
    active_freeze_ref TEXT,
    binding_valid INTEGER NOT NULL CHECK (binding_valid IN (0, 1)),
    thaw_operation_id TEXT,
    stage_operation_id TEXT NOT NULL UNIQUE,
    stage_incarnation INTEGER NOT NULL,
    source_data_ref TEXT,
    source_revision INTEGER,
    staged_revision INTEGER,
    migration_receipt_ref TEXT
);

CREATE TABLE IF NOT EXISTS versioned_plugin_data_entries (
    data_ref TEXT NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    revision INTEGER NOT NULL,
    deleted INTEGER NOT NULL CHECK (deleted IN (0, 1)),
    PRIMARY KEY (data_ref, key),
    FOREIGN KEY (data_ref) REFERENCES versioned_plugin_data_generations(data_ref)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS versioned_plugin_data_freezes (
    operation_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    data_ref TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    selection_digest TEXT NOT NULL,
    activation_generation INTEGER NOT NULL,
    freeze_purpose TEXT NOT NULL CHECK (freeze_purpose IN ('selected', 'staged_candidate')),
    freeze_ref TEXT NOT NULL UNIQUE,
    final_revision INTEGER NOT NULL,
    PRIMARY KEY (operation_id, data_ref)
);

CREATE TABLE IF NOT EXISTS versioned_plugin_data_activations (
    operation_id TEXT NOT NULL,
    data_ref TEXT NOT NULL,
    activation_generation INTEGER NOT NULL,
    artifact_id TEXT NOT NULL,
    selection_digest TEXT NOT NULL,
    expected_data_revision INTEGER NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    PRIMARY KEY (operation_id, data_ref, activation_generation)
);
"""


class SQLiteVersionedPluginDataStore:
    """Owns plugin-data generations and their shared mutation barrier."""

    def __init__(
        self,
        db_path: Path | str,
        quota: PluginDataQuota | None = None,
        migrations: Mapping[str, PluginDataMigration] | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._quota = quota or PluginDataQuota()
        validate_plugin_data_quota(self._quota)
        self._migrations = dict(migrations or {})
        if any(type(key) is not str or not callable(value)
               for key, value in self._migrations.items()):
            raise PluginDataVersioningContractError()
        self._mutation_lock = threading.RLock()

    def mutation_barrier(self) -> AbstractContextManager[None]:
        return self._mutation_lock

    def repository_for(self, binding: PluginDataBinding) -> _BoundPluginDataRepository:
        if type(binding) is not PluginDataBinding:
            raise PluginDataVersioningContractError()
        return _BoundPluginDataRepository(self, binding=binding)

    def freeze(
        self,
        operation_id: str,
        selected: SelectedInstallation,
    ) -> FrozenData:
        _validate_operation_id(operation_id)
        if type(selected) is not SelectedInstallation:
            raise PluginDataVersioningContractError()
        namespace = selected.executable.extension_id
        selection_digest = _selection_digest(selected)
        with self._mutation_lock:
            connection = self._connect()
            try:
                self._ensure_schema(connection)
                connection.execute("BEGIN IMMEDIATE")
                row = self._load_generation(connection, selected.data_ref)
                if row is None or row["namespace"] != namespace:
                    raise LifecycleConflictError("selected data generation was not found")
                if row["artifact_id"] != selected.executable.artifact_id:
                    raise LifecycleConflictError("selected artifact does not own data generation")
                stored_generation = row["activation_generation"]
                binding_is_current = (
                    row["binding_valid"] == 1
                    and stored_generation == selected.activation_generation
                )
                sealed_candidate_is_current = (
                    row["state"] == "sealed"
                    and row["binding_valid"] == 0
                    and row["stage_operation_id"] == operation_id
                    and stored_generation in (None, selected.activation_generation)
                )
                if not binding_is_current and not sealed_candidate_is_current:
                    raise LifecycleConflictError("selected activation generation is stale")
                replay = connection.execute(
                    "SELECT namespace, artifact_id, selection_digest, activation_generation, "
                    "freeze_purpose, freeze_ref, final_revision "
                    "FROM versioned_plugin_data_freezes "
                    "WHERE operation_id = ? AND data_ref = ?",
                    (operation_id, selected.data_ref),
                ).fetchone()
                if replay is not None:
                    if (
                        replay[0] != namespace
                        or replay[1] != selected.executable.artifact_id
                        or replay[2] != selection_digest
                        or replay[3] != selected.activation_generation
                        or replay[4] != _freeze_purpose(row)
                    ):
                        raise LifecycleConflictError("freeze binding changed")
                    if (
                        row["frozen"] != 1
                        or row["writable"] != 0
                        or row["dataset_revision"] != replay[6]
                        or row["active_freeze_ref"] != replay[5]
                    ):
                        raise LifecycleConflictError(
                            "freeze receipt is no longer current"
                        )
                    connection.commit()
                    return FrozenData(replay[5], selected.data_ref, replay[6])
                if row["frozen"] == 1:
                    raise LifecycleConflictError("data generation is frozen by another operation")
                freeze_ref = _freeze_ref(operation_id, selected.data_ref)
                connection.execute(
                    "UPDATE versioned_plugin_data_generations SET writable = 0, frozen = 1, "
                    "activation_generation = COALESCE(activation_generation, ?), "
                    "active_freeze_ref = ?, thaw_operation_id = NULL "
                    "WHERE data_ref = ?",
                    (selected.activation_generation, freeze_ref, selected.data_ref),
                )
                connection.execute(
                    "INSERT INTO versioned_plugin_data_freezes "
                    "(operation_id, namespace, data_ref, artifact_id, selection_digest, "
                    "activation_generation, freeze_purpose, freeze_ref, final_revision) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        operation_id,
                        namespace,
                        selected.data_ref,
                        selected.executable.artifact_id,
                        selection_digest,
                        selected.activation_generation,
                        _freeze_purpose(row),
                        freeze_ref,
                        row["dataset_revision"],
                    ),
                )
                connection.commit()
                return FrozenData(
                    freeze_ref=freeze_ref,
                    data_ref=selected.data_ref,
                    final_revision=row["dataset_revision"],
                )
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def stage(
        self,
        operation_id: str,
        candidate: ExecutableArtifact,
        frozen: FrozenData | None,
    ) -> StagedData:
        _validate_operation_id(operation_id)
        if type(candidate) is not ExecutableArtifact:
            raise PluginDataVersioningContractError()
        if frozen is not None and type(frozen) is not FrozenData:
            raise PluginDataVersioningContractError()
        migration = self._migrations.get(candidate.artifact_id)
        data_ref = _staged_data_ref(operation_id)
        source_data_ref = None if frozen is None else frozen.data_ref
        source_revision = None if frozen is None else frozen.final_revision

        with self._mutation_lock:
            connection = self._connect()
            try:
                self._ensure_schema(connection)
                connection.execute("BEGIN IMMEDIATE")
                existing = self._load_generation_by_stage_operation(connection, operation_id)
                if existing is not None and existing["state"] != "staging":
                    self._require_stage_identity(
                        existing,
                        candidate,
                        source_data_ref,
                        source_revision,
                    )
                    if (
                        existing["migration_receipt_ref"] is None
                        or existing["staged_revision"] is None
                    ):
                        raise LifecycleConflictError("sealed stage has no migration receipt")
                    connection.commit()
                    return StagedData(
                        data_ref=existing["data_ref"],
                        initial_revision=existing["staged_revision"],
                        migration_receipt_ref=existing["migration_receipt_ref"],
                    )
                if existing is not None:
                    self._require_stage_identity(
                        existing,
                        candidate,
                        source_data_ref,
                        source_revision,
                    )
                    connection.execute(
                        "DELETE FROM versioned_plugin_data_generations WHERE data_ref = ?",
                        (existing["data_ref"],),
                    )
                stage_incarnation = (
                    0 if existing is None else existing["stage_incarnation"] + 1
                )

                initial_revision = 0
                if frozen is not None:
                    self._require_frozen_source(
                        connection,
                        candidate.extension_id,
                        frozen,
                    )
                    initial_revision = frozen.final_revision
                connection.execute(
                    "INSERT INTO versioned_plugin_data_generations "
                    "(data_ref, namespace, artifact_id, activation_generation, dataset_revision, "
                    "state, writable, frozen, active_freeze_ref, binding_valid, thaw_operation_id, "
                    "stage_operation_id, "
                    "stage_incarnation, source_data_ref, source_revision, staged_revision, "
                    "migration_receipt_ref) "
                    "VALUES (?, ?, ?, NULL, ?, 'staging', 0, 0, NULL, 0, NULL, ?, ?, ?, ?, "
                    "NULL, NULL)",
                    (
                        data_ref,
                        candidate.extension_id,
                        candidate.artifact_id,
                        initial_revision,
                        operation_id,
                        stage_incarnation,
                        source_data_ref,
                        source_revision,
                    ),
                )
                if frozen is not None:
                    connection.execute(
                        "INSERT INTO versioned_plugin_data_entries "
                        "(data_ref, key, value_json, revision, deleted) "
                        "SELECT ?, key, value_json, revision, deleted "
                        "FROM versioned_plugin_data_entries WHERE data_ref = ?",
                        (data_ref, frozen.data_ref),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

        if migration is not None:
            staged_repository = _BoundPluginDataRepository(
                self,
                staging_operation_id=operation_id,
                staging_namespace=candidate.extension_id,
                staging_data_ref=data_ref,
                staging_incarnation=stage_incarnation,
            )
            migration(staged_repository)

        with self._mutation_lock:
            connection = self._connect()
            try:
                self._ensure_schema(connection)
                connection.execute("BEGIN IMMEDIATE")
                current = self._load_generation_by_stage_operation(connection, operation_id)
                if (
                    current is None
                    or current["state"] != "staging"
                    or current["stage_incarnation"] != stage_incarnation
                ):
                    raise LifecycleConflictError("staged generation changed during migration")
                self._require_stage_identity(
                    current,
                    candidate,
                    source_data_ref,
                    source_revision,
                )
                migration_receipt_ref = _migration_receipt_ref(operation_id)
                connection.execute(
                    "UPDATE versioned_plugin_data_generations "
                    "SET state = 'sealed', staged_revision = ?, migration_receipt_ref = ? "
                    "WHERE data_ref = ?",
                    (current["dataset_revision"], migration_receipt_ref, data_ref),
                )
                connection.commit()
                return StagedData(
                    data_ref=data_ref,
                    initial_revision=current["dataset_revision"],
                    migration_receipt_ref=migration_receipt_ref,
                )
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def thaw(self, operation_id: str, frozen: FrozenData) -> None:
        _validate_operation_id(operation_id)
        if type(frozen) is not FrozenData:
            raise PluginDataVersioningContractError()
        with self._mutation_lock:
            connection = self._connect()
            try:
                self._ensure_schema(connection)
                connection.execute("BEGIN IMMEDIATE")
                receipt = connection.execute(
                    "SELECT freeze_ref, final_revision FROM versioned_plugin_data_freezes "
                    "WHERE operation_id = ? AND data_ref = ?",
                    (operation_id, frozen.data_ref),
                ).fetchone()
                if receipt is None or tuple(receipt) != (
                    frozen.freeze_ref,
                    frozen.final_revision,
                ):
                    raise LifecycleConflictError("frozen-data evidence does not match")
                row = self._load_generation(connection, frozen.data_ref)
                if row is None or row["dataset_revision"] != frozen.final_revision:
                    raise LifecycleConflictError("frozen generation revision changed")
                if row["frozen"] == 0:
                    if row["thaw_operation_id"] == operation_id and row["writable"] == 0:
                        connection.commit()
                        return
                    raise LifecycleConflictError("thaw receipt is no longer current")
                if row["active_freeze_ref"] != frozen.freeze_ref:
                    raise LifecycleConflictError("data is frozen by another operation")
                connection.execute(
                    "UPDATE versioned_plugin_data_generations "
                    "SET frozen = 0, active_freeze_ref = NULL, writable = 0, "
                    "thaw_operation_id = ? "
                    "WHERE data_ref = ?",
                    (operation_id, frozen.data_ref),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def activate_selected(
        self,
        operation_id: str,
        selected: SelectedInstallation,
        *,
        expected_data_revision: int,
        enabled: bool,
    ) -> None:
        _validate_operation_id(operation_id)
        if type(selected) is not SelectedInstallation:
            raise PluginDataVersioningContractError()
        if type(expected_data_revision) is not int or expected_data_revision < 0:
            raise PluginDataVersioningContractError()
        if type(enabled) is not bool:
            raise PluginDataVersioningContractError()
        namespace = selected.executable.extension_id
        selection_digest = _selection_digest(selected)
        with self._mutation_lock:
            connection = self._connect()
            try:
                self._ensure_schema(connection)
                connection.execute("BEGIN IMMEDIATE")
                activation_receipt = connection.execute(
                    "SELECT artifact_id, selection_digest, expected_data_revision, enabled "
                    "FROM versioned_plugin_data_activations "
                    "WHERE operation_id = ? AND data_ref = ? AND activation_generation = ?",
                    (operation_id, selected.data_ref, selected.activation_generation),
                ).fetchone()
                requested_receipt = (
                    selected.executable.artifact_id,
                    selection_digest,
                    expected_data_revision,
                    1 if enabled else 0,
                )
                if activation_receipt is not None:
                    if tuple(activation_receipt) != requested_receipt:
                        raise LifecycleConflictError("activation operation binding changed")
                    connection.commit()
                    return
                row = self._load_generation(connection, selected.data_ref)
                if row is None or row["namespace"] != namespace:
                    raise LifecycleConflictError("selected data generation was not found")
                if row["artifact_id"] != selected.executable.artifact_id:
                    raise LifecycleConflictError("selected artifact does not own data generation")
                if row["state"] == "staging":
                    raise LifecycleConflictError("partial stage cannot be selected")
                if row["dataset_revision"] != expected_data_revision:
                    raise LifecycleConflictError("selected dataset revision is stale")
                if enabled and row["frozen"] == 1:
                    raise LifecycleConflictError("selected generation is still frozen")
                highest_other_generation = connection.execute(
                    "SELECT MAX(activation_generation) "
                    "FROM versioned_plugin_data_generations "
                    "WHERE namespace = ? AND data_ref <> ?",
                    (namespace, selected.data_ref),
                ).fetchone()[0]
                if (
                    highest_other_generation is not None
                    and selected.activation_generation <= highest_other_generation
                ):
                    raise LifecycleConflictError("selected activation generation is stale")
                if (
                    row["activation_generation"] is not None
                    and selected.activation_generation < row["activation_generation"]
                ):
                    raise LifecycleConflictError("selected activation generation moved backward")
                state_changes = (
                    row["state"] != "selected"
                    or row["binding_valid"] != 1
                    or row["writable"] != (1 if enabled else 0)
                    or row["activation_generation"] != selected.activation_generation
                )
                if (
                    state_changes
                    and row["activation_generation"] == selected.activation_generation
                    and not self._is_same_operation_thawed_restore(
                        connection,
                        operation_id,
                        selected,
                        enabled=enabled,
                        frozen=row["frozen"],
                        thaw_operation_id=row["thaw_operation_id"],
                    )
                ):
                    raise LifecycleConflictError(
                        "state change requires a newer activation generation"
                    )
                connection.execute(
                    "UPDATE versioned_plugin_data_generations SET "
                    "state = CASE WHEN state = 'selected' THEN 'retained' ELSE state END, "
                    "writable = 0, binding_valid = 0 "
                    "WHERE namespace = ? AND data_ref <> ?",
                    (namespace, selected.data_ref),
                )
                connection.execute(
                    "UPDATE versioned_plugin_data_generations SET state = 'selected', "
                    "activation_generation = ?, writable = ?, binding_valid = 1, "
                    "thaw_operation_id = NULL "
                    "WHERE data_ref = ?",
                    (
                        selected.activation_generation,
                        1 if enabled else 0,
                        selected.data_ref,
                    ),
                )
                connection.execute(
                    "INSERT INTO versioned_plugin_data_activations "
                    "(operation_id, data_ref, activation_generation, artifact_id, "
                    "selection_digest, expected_data_revision, enabled) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        operation_id,
                        selected.data_ref,
                        selected.activation_generation,
                        selected.executable.artifact_id,
                        selection_digest,
                        expected_data_revision,
                        1 if enabled else 0,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self._db_path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(_SCHEMA_SQL)

    @staticmethod
    def _load_generation(
        connection: sqlite3.Connection,
        data_ref: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM versioned_plugin_data_generations WHERE data_ref = ?",
            (data_ref,),
        ).fetchone()

    @staticmethod
    def _load_generation_by_stage_operation(
        connection: sqlite3.Connection,
        operation_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM versioned_plugin_data_generations WHERE stage_operation_id = ?",
            (operation_id,),
        ).fetchone()

    @staticmethod
    def _require_stage_identity(
        row: sqlite3.Row,
        candidate: ExecutableArtifact,
        source_data_ref: str | None,
        source_revision: int | None,
    ) -> None:
        if (
            row["namespace"] != candidate.extension_id
            or row["artifact_id"] != candidate.artifact_id
            or row["source_data_ref"] != source_data_ref
            or row["source_revision"] != source_revision
        ):
            raise LifecycleConflictError("stage operation binding changed")

    @staticmethod
    def _require_frozen_source(
        connection: sqlite3.Connection,
        namespace: str,
        frozen: FrozenData,
    ) -> None:
        generation = SQLiteVersionedPluginDataStore._load_generation(
            connection,
            frozen.data_ref,
        )
        if (
            generation is None
            or generation["namespace"] != namespace
            or generation["frozen"] != 1
            or generation["active_freeze_ref"] != frozen.freeze_ref
            or generation["dataset_revision"] != frozen.final_revision
        ):
            raise LifecycleConflictError("frozen source does not match staged namespace")
        receipt = connection.execute(
            "SELECT 1 FROM versioned_plugin_data_freezes "
            "WHERE data_ref = ? AND freeze_ref = ? AND final_revision = ?",
            (frozen.data_ref, frozen.freeze_ref, frozen.final_revision),
        ).fetchone()
        if receipt is None:
            raise LifecycleConflictError("frozen source evidence was not found")

    @staticmethod
    def _is_same_operation_thawed_restore(
        connection: sqlite3.Connection,
        operation_id: str,
        selected: SelectedInstallation,
        *,
        enabled: bool,
        frozen: int,
        thaw_operation_id: str | None,
    ) -> bool:
        if not enabled or frozen != 0 or thaw_operation_id != operation_id:
            return False
        receipt = connection.execute(
            "SELECT selection_digest, activation_generation "
            "FROM versioned_plugin_data_freezes "
            "WHERE operation_id = ? AND data_ref = ?",
            (operation_id, selected.data_ref),
        ).fetchone()
        return receipt is not None and tuple(receipt) == (
            _selection_digest(selected),
            selected.activation_generation,
        )


class _BoundPluginDataRepository:
    """CRUD view that resolves and checks its immutable generation every call."""

    def __init__(
        self,
        store: SQLiteVersionedPluginDataStore,
        *,
        binding: PluginDataBinding | None = None,
        staging_operation_id: str | None = None,
        staging_namespace: str | None = None,
        staging_data_ref: str | None = None,
        staging_incarnation: int | None = None,
    ) -> None:
        self._store = store
        self._binding = binding
        self._staging_operation_id = staging_operation_id
        self._namespace = binding.namespace if binding is not None else staging_namespace
        self._data_ref = binding.data_ref if binding is not None else staging_data_ref
        self._staging_incarnation = staging_incarnation
        if self._namespace is None or self._data_ref is None:
            raise PluginDataVersioningContractError()

    def get(self, namespace: str, key: str) -> PluginDataEntry:
        self._validate_namespace(namespace)
        validate_plugin_data_key(key)
        with self._store._mutation_lock:
            connection = self._store._connect()
            try:
                self._store._ensure_schema(connection)
                connection.execute("BEGIN")
                self._require_generation(connection, mutation=False)
                row = connection.execute(
                    "SELECT value_json, revision, deleted "
                    "FROM versioned_plugin_data_entries WHERE data_ref = ? AND key = ?",
                    (self._data_ref, key),
                ).fetchone()
                connection.commit()
                if row is None or row["deleted"] == 1:
                    raise PluginDataNotFoundError(key)
                return PluginDataEntry(
                    namespace=namespace,
                    key=key,
                    value=json.loads(row["value_json"]),
                    revision=row["revision"],
                )
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def list(
        self,
        namespace: str,
        prefix: str = "",
        limit: int = 200,
    ) -> list[PluginDataListItem]:
        self._validate_namespace(namespace)
        validate_plugin_data_prefix(prefix)
        validate_plugin_data_limit(limit)
        with self._store._mutation_lock:
            connection = self._store._connect()
            try:
                self._store._ensure_schema(connection)
                connection.execute("BEGIN")
                self._require_generation(connection, mutation=False)
                parameters: tuple[Any, ...]
                if not prefix:
                    query = (
                        "SELECT key, revision FROM versioned_plugin_data_entries "
                        "WHERE data_ref = ? AND deleted = 0 ORDER BY key LIMIT ?"
                    )
                    parameters = (self._data_ref, limit)
                else:
                    upper = plugin_data_prefix_upper(prefix)
                    if upper is None:
                        query = (
                            "SELECT key, revision FROM versioned_plugin_data_entries "
                            "WHERE data_ref = ? AND deleted = 0 AND key >= ? "
                            "ORDER BY key LIMIT ?"
                        )
                        parameters = (self._data_ref, prefix, limit)
                    else:
                        query = (
                            "SELECT key, revision FROM versioned_plugin_data_entries "
                            "WHERE data_ref = ? AND deleted = 0 AND key >= ? AND key < ? "
                            "ORDER BY key LIMIT ?"
                        )
                        parameters = (self._data_ref, prefix, upper, limit)
                rows = connection.execute(query, parameters).fetchall()
                connection.commit()
                return [
                    PluginDataListItem(key=row["key"], revision=row["revision"])
                    for row in rows
                    if row["key"].startswith(prefix)
                ]
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def put(
        self,
        namespace: str,
        key: str,
        value: Any,
        expected_revision: int | None = None,
    ) -> PluginDataEntry:
        self._validate_namespace(namespace)
        validate_plugin_data_key(key)
        validate_plugin_data_expected_revision(expected_revision)
        canonical = canonicalize_plugin_data_value(value)
        validate_plugin_data_value_size(canonical, self._store._quota)
        with self._store._mutation_lock:
            connection = self._store._connect()
            try:
                self._store._ensure_schema(connection)
                connection.execute("BEGIN IMMEDIATE")
                self._require_generation(connection, mutation=True)
                row = connection.execute(
                    "SELECT value_json, revision, deleted "
                    "FROM versioned_plugin_data_entries WHERE data_ref = ? AND key = ?",
                    (self._data_ref, key),
                ).fetchone()
                if row is None:
                    if expected_revision not in (None, 0):
                        raise PluginDataRevisionConflictError("stale revision")
                    revision = 1
                else:
                    if expected_revision is not None and expected_revision != row["revision"]:
                        raise PluginDataRevisionConflictError("stale revision")
                    revision = row["revision"] + 1
                live_rows = connection.execute(
                    "SELECT key, value_json FROM versioned_plugin_data_entries "
                    "WHERE data_ref = ? AND deleted = 0",
                    (self._data_ref,),
                ).fetchall()
                enforce_plugin_data_quota(
                    namespace,
                    key,
                    canonical,
                    [(item["key"], item["value_json"]) for item in live_rows],
                    self._store._quota,
                )
                connection.execute(
                    "INSERT INTO versioned_plugin_data_entries "
                    "(data_ref, key, value_json, revision, deleted) VALUES (?, ?, ?, ?, 0) "
                    "ON CONFLICT(data_ref, key) DO UPDATE SET "
                    "value_json = excluded.value_json, revision = excluded.revision, deleted = 0",
                    (self._data_ref, key, canonical, revision),
                )
                self._advance_dataset_revision(connection)
                connection.commit()
                return PluginDataEntry(
                    namespace=namespace,
                    key=key,
                    value=json.loads(canonical),
                    revision=revision,
                )
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def delete(
        self,
        namespace: str,
        key: str,
        expected_revision: int | None = None,
    ) -> bool:
        self._validate_namespace(namespace)
        validate_plugin_data_key(key)
        validate_plugin_data_expected_revision(expected_revision)
        with self._store._mutation_lock:
            connection = self._store._connect()
            try:
                self._store._ensure_schema(connection)
                connection.execute("BEGIN IMMEDIATE")
                self._require_generation(connection, mutation=True)
                row = connection.execute(
                    "SELECT revision, deleted FROM versioned_plugin_data_entries "
                    "WHERE data_ref = ? AND key = ?",
                    (self._data_ref, key),
                ).fetchone()
                if row is None:
                    if expected_revision not in (None, 0):
                        raise PluginDataRevisionConflictError("stale revision")
                    connection.commit()
                    return False
                if row["deleted"] == 1:
                    if expected_revision is not None and expected_revision != row["revision"]:
                        raise PluginDataRevisionConflictError("stale revision")
                    connection.commit()
                    return False
                if expected_revision is not None and expected_revision != row["revision"]:
                    raise PluginDataRevisionConflictError("stale revision")
                connection.execute(
                    "UPDATE versioned_plugin_data_entries SET revision = ?, deleted = 1 "
                    "WHERE data_ref = ? AND key = ?",
                    (row["revision"] + 1, self._data_ref, key),
                )
                self._advance_dataset_revision(connection)
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _validate_namespace(self, namespace: str) -> None:
        validate_plugin_data_namespace(namespace)
        if namespace != self._namespace:
            raise PluginDataRevisionConflictError("plugin data namespace binding is stale")

    def _require_generation(
        self,
        connection: sqlite3.Connection,
        *,
        mutation: bool,
    ) -> sqlite3.Row:
        row = self._store._load_generation(connection, self._data_ref)
        if row is None or row["namespace"] != self._namespace:
            raise PluginDataRevisionConflictError("plugin data generation binding is stale")
        if self._binding is not None:
            if (
                row["binding_valid"] != 1
                or row["activation_generation"] != self._binding.activation_generation
            ):
                raise PluginDataRevisionConflictError("plugin data activation binding is stale")
            if mutation and (row["writable"] != 1 or row["frozen"] == 1):
                raise PluginDataRevisionConflictError("plugin data generation is not writable")
        else:
            if (
                row["state"] != "staging"
                or row["stage_operation_id"] != self._staging_operation_id
                or row["stage_incarnation"] != self._staging_incarnation
            ):
                raise PluginDataRevisionConflictError("staged plugin data binding is stale")
        return row

    def _advance_dataset_revision(self, connection: sqlite3.Connection) -> None:
        cursor = connection.execute(
            "UPDATE versioned_plugin_data_generations "
            "SET dataset_revision = dataset_revision + 1 WHERE data_ref = ?",
            (self._data_ref,),
        )
        if cursor.rowcount != 1:
            raise PluginDataRevisionConflictError("plugin data generation is missing")


def _validate_operation_id(operation_id: object) -> None:
    try:
        validate_schema_ref(
            "contracts/common/types.schema.json#/definitions/uuid",
            operation_id,
        )
    except SchemaValidationError:
        raise PluginDataVersioningContractError() from None


def _staged_data_ref(operation_id: str) -> str:
    return f"ref:plugin-data.{operation_id}"


def _freeze_ref(operation_id: str, data_ref: str) -> str:
    suffix = hashlib.sha256(data_ref.encode("utf-8")).hexdigest()[:16]
    return f"ref:plugin-data-freeze.{operation_id}.{suffix}"


def _migration_receipt_ref(operation_id: str) -> str:
    return f"ref:plugin-data-migration.{operation_id}"


def _freeze_purpose(row: sqlite3.Row) -> str:
    return "staged_candidate" if row["state"] == "sealed" else "selected"


def _selection_digest(selected: SelectedInstallation) -> str:
    payload = {
        "artifact_id": selected.executable.artifact_id,
        "extension_id": selected.executable.extension_id,
        "version": selected.executable.version,
        "requested_scopes": list(selected.executable.requested_scopes),
        "data_ref": selected.data_ref,
        "approved_scopes": list(selected.approved_scopes),
        "grant_generation": selected.grant_generation,
        "activation_generation": selected.activation_generation,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

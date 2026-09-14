"""SQLite durable plugin job STATE repository (B19/B22 slice)."""
from __future__ import annotations

import json
import math
import sqlite3
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from model_deck.engine.jobs.ports import (
    ACTIVE_JOB_STATES,
    CHECKPOINT_MAX_BYTES,
    JSON_VALUE_MAX_BYTES,
    TERMINAL_JOB_STATES,
    BoundedJsonValidationError,
    ClaimJobCommand,
    CompleteJobCommand,
    ConfirmCancelCommand,
    CreateJobCommand,
    FAILURE_CODES,
    FailJobCommand,
    GetJobCommand,
    GetPublicCommand,
    JobCheckpointConflictError,
    JobCheckpointValidationError,
    JobIdempotencyConflictError,
    JobNotFoundError,
    JobOriginMismatchError,
    JobOwner,
    JobOwnershipMismatchError,
    JobPublicView,
    JobRecord,
    JobState,
    JobStateConflictError,
    JobTerminalConflictError,
    ReportProgressCommand,
    RequestCancelCommand,
    RequestCancelPublicCommand,
    SaveCheckpointCommand,
    WorkerCrashResult,
    validate_bounded_json_value,
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS plugin_jobs (
    job_id TEXT PRIMARY KEY,
    plugin_id TEXT NOT NULL,
    activation_id TEXT NOT NULL,
    invocation_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    state TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    origin_principal_id TEXT NOT NULL DEFAULT '',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    checkpoint_revision INTEGER NOT NULL DEFAULT 0,
    checkpoint_schema_id TEXT,
    checkpoint_json TEXT,
    output_json TEXT,
    failure_code TEXT
);
CREATE INDEX IF NOT EXISTS idx_plugin_jobs_activation_state
    ON plugin_jobs(activation_id, state);
CREATE TABLE IF NOT EXISTS plugin_job_cancel_receipts (
    principal_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    job_id TEXT NOT NULL,
    accepted INTEGER NOT NULL,
    PRIMARY KEY (principal_id, idempotency_key)
);
"""

CheckpointValidator = Callable[[str | None, Any], None]


class SQLitePluginJobRepository:
    def __init__(
        self,
        db_path: Path,
        *,
        uuid_factory: Callable[[], str] | None = None,
        utc_clock: Callable[[], str] | None = None,
        checkpoint_validator: CheckpointValidator,
    ) -> None:
        self._db_path = db_path
        self._uuid_factory = uuid_factory or (lambda: str(uuid.uuid4()))
        self._utc_clock = utc_clock or (
            lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        if not callable(checkpoint_validator):
            raise TypeError("checkpoint_validator must be a callable")
        self._validator = checkpoint_validator

    def create(self, command: CreateJobCommand) -> JobRecord:
        self._require_owner(command.owner)
        self._require_text("invocation_id", command.invocation_id)
        self._require_text("operation_id", command.operation_id)
        self._require_text("origin_principal_id", command.origin_principal_id)
        if command.checkpoint_schema_id is not None:
            self._require_text("checkpoint_schema_id", command.checkpoint_schema_id)
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                record = JobRecord(
                    job_id=self._uuid_factory(),
                    plugin_id=command.owner.plugin_id,
                    activation_id=command.owner.activation_id,
                    invocation_id=command.invocation_id,
                    operation_id=command.operation_id,
                    state=JobState.QUEUED,
                    progress=0.0,
                    created_at=self._utc_clock(),
                    origin_principal_id=command.origin_principal_id,
                    checkpoint_schema_id=command.checkpoint_schema_id,
                )
                conn.execute(
                    "INSERT INTO plugin_jobs (job_id, plugin_id, activation_id, invocation_id,"
                    " operation_id, state, progress, created_at, origin_principal_id,"
                    " cancel_requested, checkpoint_revision, checkpoint_schema_id,"
                    " checkpoint_json, output_json, failure_code)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, NULL, NULL, NULL)",
                    (
                        record.job_id,
                        record.plugin_id,
                        record.activation_id,
                        record.invocation_id,
                        record.operation_id,
                        JobState.QUEUED.value,
                        0.0,
                        record.created_at,
                        record.origin_principal_id,
                        record.checkpoint_schema_id,
                    ),
                )
                conn.commit()
                return record
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def claim(self, command: ClaimJobCommand) -> JobRecord:
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._load_row(conn, command.job_id)
                self._check_owner(row, command.owner)
                if row["state"] in {s.value for s in TERMINAL_JOB_STATES}:
                    raise JobTerminalConflictError("job is terminal")
                if row["state"] != JobState.QUEUED.value:
                    raise JobStateConflictError("only queued jobs can be claimed")
                updated = conn.execute(
                    "UPDATE plugin_jobs SET state = ? WHERE job_id = ? AND state = ?",
                    (JobState.RUNNING.value, command.job_id, JobState.QUEUED.value),
                )
                if updated.rowcount != 1:
                    raise JobStateConflictError("claim lost race")
                conn.commit()
                return self._get_owned(conn, command.job_id, command.owner)
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def report_progress(self, command: ReportProgressCommand) -> JobRecord:
        self._require_progress(command.progress)
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._load_row(conn, command.job_id)
                self._check_owner(row, command.owner)
                if row["state"] in {s.value for s in TERMINAL_JOB_STATES}:
                    raise JobTerminalConflictError("job already terminal")
                self._require_active(row)
                if command.progress < float(row["progress"]) - 1e-12:
                    raise JobStateConflictError("progress must be monotonic")
                updated = conn.execute(
                    "UPDATE plugin_jobs SET progress = ? WHERE job_id = ? AND progress <= ?",
                    (command.progress, command.job_id, command.progress),
                )
                if updated.rowcount != 1:
                    raise JobStateConflictError("progress race")
                conn.commit()
                return self._get_owned(conn, command.job_id, command.owner)
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def save_checkpoint(self, command: SaveCheckpointCommand) -> JobRecord:
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._load_row(conn, command.job_id)
                self._check_owner(row, command.owner)
                self._require_active(row)
                self._require_revision(command.expected_revision)
                if int(row["checkpoint_revision"]) != command.expected_revision:
                    raise JobCheckpointConflictError("stale checkpoint revision")
                declared_schema_id = row["checkpoint_schema_id"]
                if (
                    command.schema_id is not None
                    and command.schema_id != declared_schema_id
                ):
                    raise JobCheckpointValidationError(
                        "checkpoint schema_id cannot replace the declared schema"
                    )
                schema_id = declared_schema_id
                self._require_strict_json(command.checkpoint)
                try:
                    self._validator(schema_id, command.checkpoint)
                except JobCheckpointValidationError:
                    raise
                except Exception as exc:
                    raise JobCheckpointValidationError(str(exc)) from exc
                payload = self._encode_checkpoint(command.checkpoint)
                conn.execute(
                    "UPDATE plugin_jobs SET checkpoint_json = ?, checkpoint_revision = ?,"
                    " checkpoint_schema_id = ? WHERE job_id = ? AND checkpoint_revision = ?",
                    (
                        payload,
                        command.expected_revision + 1,
                        schema_id,
                        command.job_id,
                        command.expected_revision,
                    ),
                )
                if conn.total_changes == 0:
                    raise JobCheckpointConflictError("checkpoint race")
                conn.commit()
                return self._get_owned(conn, command.job_id, command.owner)
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def request_cancel(self, command: RequestCancelCommand) -> JobRecord:
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._load_row(conn, command.job_id)
                self._check_owner(row, command.owner)
                self._require_active(row)
                conn.execute(
                    "UPDATE plugin_jobs SET cancel_requested = 1 WHERE job_id = ?",
                    (command.job_id,),
                )
                conn.commit()
                return self._get_owned(conn, command.job_id, command.owner)
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def confirm_cancel(self, command: ConfirmCancelCommand) -> JobRecord:
        return self._terminalize(
            command.job_id, command.owner, JobState.CANCELLED, None, None, False
        )

    def complete(self, command: CompleteJobCommand) -> JobRecord:
        # Encode the output JSON up front so the storage transaction either
        # commits both state and output atomically or rolls them back together.
        # ``output_present`` distinguishes "worker did not supply output"
        # from "worker supplied explicit JSON null"; only the latter is
        # persisted, matching the public schema's optional output slot.
        if command.output_present:
            encoded = self._encode_output(command.output)
        else:
            encoded = None
        return self._terminalize(
            command.job_id,
            command.owner,
            JobState.COMPLETED,
            None,
            encoded,
            command.output_present,
        )

    def fail(self, command: FailJobCommand) -> JobRecord:
        self._require_text("failure_code", command.failure_code)
        if command.failure_code not in FAILURE_CODES:
            raise ValueError(
                f"failure_code must be one of {sorted(FAILURE_CODES)}"
            )
        return self._terminalize(
            command.job_id,
            command.owner,
            JobState.FAILED,
            command.failure_code,
            None,
            False,
        )

    def get(self, command: GetJobCommand) -> JobRecord:
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            return self._get(conn, command.job_id)
        finally:
            conn.close()

    def get_public(self, command: GetPublicCommand) -> JobPublicView:
        self._require_text("job_id", command.job_id)
        self._require_text("caller_principal_id", command.caller_principal_id)
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            row = self._load_row(conn, command.job_id)
            stored_origin = row["origin_principal_id"]
            if not stored_origin:
                # Backstop: rows created before origin was captured must not
                # authorize any public reader.
                raise JobOriginMismatchError("job has no recorded origin")
            if stored_origin != command.caller_principal_id:
                raise JobOriginMismatchError(
                    "caller is not the originating principal"
                )
            return self._to_public_view(row)
        finally:
            conn.close()

    def request_cancel_public(
        self, command: RequestCancelPublicCommand
    ) -> bool:
        """Record a public cancel intent.

        The first request durably binds ``(caller, idempotency_key)`` to the
        job and result in the same transaction as cancel intent. Exact replays
        return that stored result even after the job becomes terminal. Reusing
        the key for another job raises ``JobIdempotencyConflictError``.
        """
        self._require_text("job_id", command.job_id)
        self._require_text("caller_principal_id", command.caller_principal_id)
        self._require_text("idempotency_key", command.idempotency_key)
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._load_row(conn, command.job_id)
                stored_origin = row["origin_principal_id"]
                if not stored_origin:
                    raise JobOriginMismatchError("job has no recorded origin")
                if stored_origin != command.caller_principal_id:
                    raise JobOriginMismatchError(
                        "caller is not the originating principal"
                    )
                receipt = conn.execute(
                    "SELECT job_id, accepted FROM plugin_job_cancel_receipts"
                    " WHERE principal_id = ? AND idempotency_key = ?",
                    (command.caller_principal_id, command.idempotency_key),
                ).fetchone()
                if receipt is not None:
                    if receipt["job_id"] != command.job_id:
                        raise JobIdempotencyConflictError(
                            "idempotency key already used for another job"
                        )
                    conn.commit()
                    return bool(receipt["accepted"])
                accepted = row["state"] not in {
                    state.value for state in TERMINAL_JOB_STATES
                }
                if accepted:
                    conn.execute(
                        "UPDATE plugin_jobs SET cancel_requested = 1 WHERE job_id = ?"
                        " AND state IN (?, ?)",
                        (
                            command.job_id,
                            JobState.QUEUED.value,
                            JobState.RUNNING.value,
                        ),
                    )
                conn.execute(
                    "INSERT INTO plugin_job_cancel_receipts"
                    " (principal_id, idempotency_key, job_id, accepted)"
                    " VALUES (?, ?, ?, ?)",
                    (
                        command.caller_principal_id,
                        command.idempotency_key,
                        command.job_id,
                        int(accepted),
                    ),
                )
                conn.commit()
                return accepted
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def list_active_for_activation(self, owner: JobOwner) -> list[JobRecord]:
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            rows = conn.execute(
                "SELECT * FROM plugin_jobs WHERE plugin_id = ? AND activation_id = ?"
                " AND state IN (?, ?) ORDER BY created_at, job_id",
                (
                    owner.plugin_id,
                    owner.activation_id,
                    JobState.QUEUED.value,
                    JobState.RUNNING.value,
                ),
            ).fetchall()
            return [self._to_record(r) for r in rows]
        finally:
            conn.close()

    def mark_worker_crashed(self, owner: JobOwner) -> WorkerCrashResult:
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    "SELECT job_id FROM plugin_jobs WHERE plugin_id = ?"
                    " AND activation_id = ? AND state IN (?, ?)",
                    (
                        owner.plugin_id,
                        owner.activation_id,
                        JobState.QUEUED.value,
                        JobState.RUNNING.value,
                    ),
                ).fetchall()
                ids = tuple(r[0] for r in rows)
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    conn.execute(
                        f"UPDATE plugin_jobs SET state = ? WHERE job_id IN ({placeholders})"
                        f" AND state IN (?, ?)",
                        (JobState.INTERRUPTED.value, *ids, JobState.QUEUED.value, JobState.RUNNING.value),
                    )
                conn.commit()
                return WorkerCrashResult(interrupted_job_ids=ids)
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def _terminalize(
        self,
        job_id: str,
        owner: JobOwner,
        outcome: JobState,
        failure_code: str | None,
        output_json: str | None,
        output_present: bool,
    ) -> JobRecord:
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._load_row(conn, job_id)
                self._check_owner(row, owner)
                if row["state"] in {s.value for s in TERMINAL_JOB_STATES}:
                    raise JobTerminalConflictError("job already terminal")
                if row["state"] not in {s.value for s in ACTIVE_JOB_STATES}:
                    raise JobStateConflictError("job is not active")
                if outcome is JobState.COMPLETED and output_present:
                    updated = conn.execute(
                        "UPDATE plugin_jobs SET state = ?, failure_code = ?,"
                        " output_json = ? WHERE job_id = ? AND state = ?",
                        (
                            outcome.value,
                            failure_code,
                            output_json,
                            job_id,
                            row["state"],
                        ),
                    )
                else:
                    # Only COMPLETED may persist output; never accept output on
                    # FAILED / CANCELLED / INTERRUPTED.
                    updated = conn.execute(
                        "UPDATE plugin_jobs SET state = ?, failure_code = ?"
                        " WHERE job_id = ? AND state = ?",
                        (outcome.value, failure_code, job_id, row["state"]),
                    )
                if updated.rowcount != 1:
                    raise JobTerminalConflictError("terminal race")
                conn.commit()
                return self._get_owned(conn, job_id, owner)
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(_SCHEMA_SQL)
        self._migrate_add_origin_principal_id(conn)
        self._migrate_add_output_json(conn)

    def _migrate_add_origin_principal_id(self, conn: sqlite3.Connection) -> None:
        """Add the origin_principal_id column if it is missing.

        Older rows predate origin capture; they stay readable but cannot
        satisfy a public reader, so the column is backfilled with the empty
        string and the public read path treats empty as missing.
        """
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(plugin_jobs)").fetchall()}
        if "origin_principal_id" in existing:
            return
        conn.execute(
            "ALTER TABLE plugin_jobs ADD COLUMN origin_principal_id TEXT NOT NULL DEFAULT ''"
        )

    def _migrate_add_output_json(self, conn: sqlite3.Connection) -> None:
        """Add the bounded output_json column if missing.

        Output is only ever written together with a successful COMPLETED
        transition, so no backfill is needed for legacy rows.
        """
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(plugin_jobs)").fetchall()}
        if "output_json" in existing:
            return
        conn.execute("ALTER TABLE plugin_jobs ADD COLUMN output_json TEXT")

    def _load_row(self, conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM plugin_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise JobNotFoundError(job_id)
        return row

    def _check_owner(self, row: sqlite3.Row, owner: JobOwner) -> None:
        if row["plugin_id"] != owner.plugin_id or row["activation_id"] != owner.activation_id:
            raise JobOwnershipMismatchError("plugin/activation mismatch")

    def _get(self, conn: sqlite3.Connection, job_id: str) -> JobRecord:
        return self._to_record(self._load_row(conn, job_id))

    def _get_owned(
        self, conn: sqlite3.Connection, job_id: str, owner: JobOwner
    ) -> JobRecord:
        row = self._load_row(conn, job_id)
        self._check_owner(row, owner)
        return self._to_record(row)

    @staticmethod
    def _to_record(row: sqlite3.Row) -> JobRecord:
        # Decode stored output_json if present; absent column means no
        # completed output was ever written.
        output_present = row["output_json"] is not None
        if output_present:
            try:
                decoded = json.loads(row["output_json"])
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"stored output_json is not valid JSON: {exc}"
                ) from exc
        else:
            decoded = None
        return JobRecord(
            job_id=row["job_id"],
            plugin_id=row["plugin_id"],
            activation_id=row["activation_id"],
            invocation_id=row["invocation_id"],
            operation_id=row["operation_id"],
            state=JobState(row["state"]),
            progress=float(row["progress"]),
            created_at=row["created_at"],
            origin_principal_id=row["origin_principal_id"],
            cancel_requested=bool(row["cancel_requested"]),
            checkpoint_revision=int(row["checkpoint_revision"]),
            checkpoint_schema_id=row["checkpoint_schema_id"],
            checkpoint_json=row["checkpoint_json"],
            output=decoded,
            output_present=output_present,
            failure_code=row["failure_code"],
        )

    @staticmethod
    def _to_public_view(row: sqlite3.Row) -> JobPublicView:
        # Only COMPLETED rows are eligible to expose a public output value.
        state = JobState(row["state"])
        if state is JobState.COMPLETED and row["output_json"] is not None:
            try:
                decoded = json.loads(row["output_json"])
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"stored output_json is not valid JSON: {exc}"
                ) from exc
            return JobPublicView(
                job_id=row["job_id"],
                state=state,
                progress=float(row["progress"]),
                output_present=True,
                output=decoded,
            )
        return JobPublicView(
            job_id=row["job_id"],
            state=state,
            progress=float(row["progress"]),
            output_present=False,
            output=None,
        )

    @staticmethod
    def _require_owner(owner: JobOwner) -> None:
        SQLitePluginJobRepository._require_text("plugin_id", owner.plugin_id)
        SQLitePluginJobRepository._require_text("activation_id", owner.activation_id)

    @staticmethod
    def _require_text(name: str, value: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")

    @staticmethod
    def _require_progress(value: float) -> None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("progress must be a number")
        if math.isnan(value) or math.isinf(value):
            raise ValueError("progress must be finite")
        if not 0.0 <= float(value) <= 1.0:
            raise ValueError("progress must be within [0, 1]")

    @staticmethod
    def _require_revision(value: int) -> None:
        if type(value) is not int:
            raise ValueError("expected_revision must be an int")
        if value < 0:
            raise ValueError("expected_revision must be >= 0")

    @staticmethod
    def _require_active(row: sqlite3.Row) -> None:
        if row["state"] not in {s.value for s in ACTIVE_JOB_STATES}:
            raise JobStateConflictError("job is not active")

    @staticmethod
    def _require_strict_json(value: Any) -> None:
        if value is None or isinstance(value, (str, bool)):
            return
        if isinstance(value, int):
            return
        if isinstance(value, float):
            if math.isnan(value) or math.isinf(value):
                raise JobCheckpointValidationError(
                    "checkpoint must not contain NaN or infinity"
                )
            return
        if isinstance(value, list):
            for item in value:
                SQLitePluginJobRepository._require_strict_json(item)
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if type(key) is not str:
                    raise JobCheckpointValidationError(
                        "checkpoint object keys must be strings"
                    )
                SQLitePluginJobRepository._require_strict_json(item)
            return
        raise JobCheckpointValidationError(
            f"checkpoint type is not JSON: {type(value).__name__}"
        )

    @staticmethod
    def _encode_checkpoint(value: Any) -> str:
        try:
            payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise JobCheckpointValidationError(f"checkpoint is not JSON: {exc}") from exc
        if len(payload.encode("utf-8")) > CHECKPOINT_MAX_BYTES:
            raise JobCheckpointValidationError("checkpoint exceeds 1 MiB")
        return payload

    @classmethod
    def _encode_output(cls, value: Any) -> str:
        """Encode a bounded output value with the application-owned validator.

        Validation caps, depth and node limits live in
        ``validate_bounded_json_value``; the storage adapter only enforces the
        encoded-payload size cap (1 MiB) so the durable row stays bounded.
        """
        try:
            validate_bounded_json_value(value)
        except BoundedJsonValidationError as exc:
            raise JobCheckpointValidationError(str(exc)) from exc
        try:
            payload = json.dumps(value, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise JobCheckpointValidationError("output is not JSON") from exc
        if len(payload.encode("utf-8")) > JSON_VALUE_MAX_BYTES:
            raise JobCheckpointValidationError("output exceeds 1 MiB")
        return payload

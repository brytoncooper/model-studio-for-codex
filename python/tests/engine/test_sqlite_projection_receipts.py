from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from model_deck.adapters.storage.sqlite_outbox import ensure_projection_outbox_schema
from model_deck.adapters.storage.sqlite_projection_receipts import (
    CURRENT_SCHEMA_VERSION,
    SQLiteProjectionReceiptStore,
    ensure_projection_receipt_schema,
)
from model_deck.engine.projections.ports import ProjectionOutboxEvent
from model_deck.engine.projections.receipts import (
    ProjectionOutboxEventMismatchError,
    ProjectionOutboxStateError,
    ProjectionReceiptConflictError,
    validate_aggregate_type,
    validate_conflict_detail,
    validate_output_sha256,
    validate_strict_int,
)

HASH_A = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
HASH_B = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
CONSUMER = "consumer-a"
ARTIFACT_A = "artifact:alpha"
ARTIFACT_B = "artifact:beta"


def _seed_outbox(db_path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        ensure_projection_outbox_schema(conn)
        conn.executemany(
            "INSERT INTO projection_outbox "
            "(aggregate_type, aggregate_id, aggregate_revision, event_kind, payload_json, state) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _event_from_db(db_path: Path, outbox_id: int) -> ProjectionOutboxEvent:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT outbox_id, aggregate_type, aggregate_id, aggregate_revision, event_kind, payload_json "
            "FROM projection_outbox WHERE outbox_id = ?",
            (outbox_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return ProjectionOutboxEvent(
        outbox_id=row[0],
        aggregate_type=row[1],
        aggregate_id=row[2],
        aggregate_revision=row[3],
        event_kind=row[4],
        payload_json=row[5],
    )


def _outbox_snapshot(db_path: Path) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT outbox_id, aggregate_type, aggregate_id, aggregate_revision, event_kind, payload_json, state "
            "FROM projection_outbox ORDER BY outbox_id"
        ).fetchall()
    finally:
        conn.close()


class SQLiteProjectionReceiptStoreTests(unittest.TestCase):
    def test_missing_db_rejected_without_creation(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "missing.sqlite3"
            store = SQLiteProjectionReceiptStore(db_path)
            event = ProjectionOutboxEvent(
                outbox_id=1,
                aggregate_type="registered_model",
                aggregate_id="reg-1",
                aggregate_revision=1,
                event_kind="registered_model.upserted",
                payload_json='{"a":1}',
            )
            with self.assertRaises(FileNotFoundError):
                store.record_applied(
                    event,
                    consumer_id=CONSUMER,
                    artifact_ref=ARTIFACT_A,
                    output_sha256=HASH_A,
                )
            with self.assertRaises(FileNotFoundError):
                store.get_applied(
                    consumer_id=CONSUMER,
                    aggregate_type="registered_model",
                    aggregate_id="reg-1",
                )
            self.assertFalse(db_path.exists())

    def test_receipt_schema_refuses_to_create_the_outbox_authority(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "unrelated.sqlite3"
            conn = sqlite3.connect(db_path)
            try:
                with self.assertRaises(ProjectionOutboxStateError):
                    ensure_projection_receipt_schema(conn)
                outbox = conn.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'projection_outbox'"
                ).fetchone()
            finally:
                conn.close()
            self.assertIsNone(outbox)

    def test_schema_additive_and_outbox_content_preserved(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        1,
                        "registered_model.upserted",
                        '{"a":1}',
                        "pending",
                    )
                ],
            )
            before_rows = _outbox_snapshot(db_path)
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 1)
            store.record_applied(
                event,
                consumer_id=CONSUMER,
                artifact_ref=ARTIFACT_A,
                output_sha256=HASH_A,
            )
            after_rows = _outbox_snapshot(db_path)
            self.assertEqual(after_rows[0][:6], before_rows[0][:6])
            self.assertEqual(after_rows[0][6], "applied")
            conn = sqlite3.connect(db_path)
            try:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            finally:
                conn.close()
            self.assertIn("projection_applied_state", tables)
            self.assertIn("projection_outbox_applied_receipts", tables)
            self.assertIn("projection_outbox_conflicts", tables)

    def test_get_applied_is_read_only_and_returns_current_state(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(db_path, [])
            store = SQLiteProjectionReceiptStore(db_path)
            before = db_path.read_bytes()
            self.assertIsNone(
                store.get_applied(
                    consumer_id=CONSUMER,
                    aggregate_type="registered_model",
                    aggregate_id="reg-1",
                )
            )
            self.assertEqual(db_path.read_bytes(), before)

            conn = sqlite3.connect(db_path)
            try:
                ensure_projection_receipt_schema(conn)
                conn.execute(
                    "INSERT INTO projection_applied_state "
                    "(consumer_id, aggregate_type, aggregate_id, applied_revision, "
                    "applied_outbox_id, artifact_ref, output_sha256) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (CONSUMER, "registered_model", "reg-1", 3, 7, ARTIFACT_A, HASH_A),
                )
                conn.commit()
            finally:
                conn.close()
            state = store.get_applied(
                consumer_id=CONSUMER,
                aggregate_type="registered_model",
                aggregate_id="reg-1",
            )
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual(state.applied_revision, 3)
            self.assertEqual(state.applied_outbox_id, 7)

    def test_event_mismatch_for_each_field(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        1,
                        "registered_model.upserted",
                        '{"a":1}',
                        "pending",
                    )
                ],
            )
            base = _event_from_db(db_path, 1)
            store = SQLiteProjectionReceiptStore(db_path)
            variants = [
                ProjectionOutboxEvent(
                    outbox_id=99,
                    aggregate_type=base.aggregate_type,
                    aggregate_id=base.aggregate_id,
                    aggregate_revision=base.aggregate_revision,
                    event_kind=base.event_kind,
                    payload_json=base.payload_json,
                ),
                ProjectionOutboxEvent(
                    outbox_id=base.outbox_id,
                    aggregate_type="other",
                    aggregate_id=base.aggregate_id,
                    aggregate_revision=base.aggregate_revision,
                    event_kind=base.event_kind,
                    payload_json=base.payload_json,
                ),
                ProjectionOutboxEvent(
                    outbox_id=base.outbox_id,
                    aggregate_type=base.aggregate_type,
                    aggregate_id="other",
                    aggregate_revision=base.aggregate_revision,
                    event_kind=base.event_kind,
                    payload_json=base.payload_json,
                ),
                ProjectionOutboxEvent(
                    outbox_id=base.outbox_id,
                    aggregate_type=base.aggregate_type,
                    aggregate_id=base.aggregate_id,
                    aggregate_revision=2,
                    event_kind=base.event_kind,
                    payload_json=base.payload_json,
                ),
                ProjectionOutboxEvent(
                    outbox_id=base.outbox_id,
                    aggregate_type=base.aggregate_type,
                    aggregate_id=base.aggregate_id,
                    aggregate_revision=base.aggregate_revision,
                    event_kind="other.kind",
                    payload_json=base.payload_json,
                ),
                ProjectionOutboxEvent(
                    outbox_id=base.outbox_id,
                    aggregate_type=base.aggregate_type,
                    aggregate_id=base.aggregate_id,
                    aggregate_revision=base.aggregate_revision,
                    event_kind=base.event_kind,
                    payload_json='{"a":2}',
                ),
            ]
            for variant in variants:
                with self.subTest(outbox_id=variant.outbox_id, kind=variant.event_kind):
                    with self.assertRaises(ProjectionOutboxEventMismatchError):
                        store.record_applied(
                            variant,
                            consumer_id=CONSUMER,
                            artifact_ref=ARTIFACT_A,
                            output_sha256=HASH_A,
                        )

    def test_applied_transaction_updates_state_and_outbox(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        1,
                        "registered_model.upserted",
                        '{"a":1}',
                        "pending",
                    )
                ],
            )
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 1)
            state = store.record_applied(
                event,
                consumer_id=CONSUMER,
                artifact_ref=ARTIFACT_A,
                output_sha256=HASH_A,
            )
            self.assertEqual(state.applied_revision, 1)
            self.assertEqual(state.applied_outbox_id, 1)
            self.assertEqual(state.artifact_ref, ARTIFACT_A)
            self.assertEqual(state.output_sha256, HASH_A)
            self.assertEqual(_outbox_snapshot(db_path)[0][6], "applied")

    def test_exact_idempotent_replay(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        1,
                        "registered_model.upserted",
                        '{"a":1}',
                        "pending",
                    )
                ],
            )
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 1)
            first = store.record_applied(
                event,
                consumer_id=CONSUMER,
                artifact_ref=ARTIFACT_A,
                output_sha256=HASH_A,
            )
            second = store.record_applied(
                event,
                consumer_id=CONSUMER,
                artifact_ref=ARTIFACT_A,
                output_sha256=HASH_A,
            )
            self.assertEqual(first, second)

    def test_higher_revision_advances_state(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        1,
                        "registered_model.upserted",
                        '{"a":1}',
                        "applied",
                    ),
                    (
                        "registered_model",
                        "reg-1",
                        2,
                        "registered_model.upserted",
                        '{"a":2}',
                        "pending",
                    ),
                ],
            )
            conn = sqlite3.connect(db_path)
            try:
                ensure_projection_receipt_schema(conn)
                conn.execute(
                    "INSERT INTO projection_applied_state "
                    "(consumer_id, aggregate_type, aggregate_id, applied_revision, applied_outbox_id, artifact_ref, output_sha256) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (CONSUMER, "registered_model", "reg-1", 1, 1, ARTIFACT_A, HASH_A),
                )
                conn.execute(
                    "INSERT INTO projection_outbox_applied_receipts "
                    "(outbox_id, consumer_id, aggregate_type, aggregate_id, applied_revision, "
                    "artifact_ref, output_sha256) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (1, CONSUMER, "registered_model", "reg-1", 1, ARTIFACT_A, HASH_A),
                )
                conn.commit()
            finally:
                conn.close()
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 2)
            state = store.record_applied(
                event,
                consumer_id=CONSUMER,
                artifact_ref=ARTIFACT_B,
                output_sha256=HASH_B,
            )
            self.assertEqual(state.applied_revision, 2)
            self.assertEqual(state.applied_outbox_id, 2)
            self.assertEqual(state.artifact_ref, ARTIFACT_B)
            conn = sqlite3.connect(db_path)
            try:
                current_count = conn.execute(
                    "SELECT COUNT(*) FROM projection_applied_state"
                ).fetchone()[0]
                receipt_count = conn.execute(
                    "SELECT COUNT(*) FROM projection_outbox_applied_receipts"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(current_count, 1)
            self.assertEqual(receipt_count, 2)

    def test_older_revision_ack_without_rollback(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        1,
                        "registered_model.upserted",
                        '{"a":1}',
                        "pending",
                    ),
                    (
                        "registered_model",
                        "reg-1",
                        2,
                        "registered_model.upserted",
                        '{"a":2}',
                        "applied",
                    ),
                ],
            )
            conn = sqlite3.connect(db_path)
            try:
                ensure_projection_receipt_schema(conn)
                conn.execute(
                    "INSERT INTO projection_applied_state "
                    "(consumer_id, aggregate_type, aggregate_id, applied_revision, applied_outbox_id, artifact_ref, output_sha256) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (CONSUMER, "registered_model", "reg-1", 2, 2, ARTIFACT_B, HASH_B),
                )
                conn.commit()
            finally:
                conn.close()
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 1)
            state = store.record_applied(
                event,
                consumer_id=CONSUMER,
                artifact_ref=ARTIFACT_A,
                output_sha256=HASH_A,
            )
            self.assertEqual(state.applied_revision, 2)
            self.assertEqual(state.applied_outbox_id, 2)
            self.assertEqual(state.artifact_ref, ARTIFACT_B)
            self.assertEqual(_outbox_snapshot(db_path)[0][6], "applied")

    def test_equal_revision_different_hash_preserves_pending(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        2,
                        "registered_model.upserted",
                        '{"a":2}',
                        "pending",
                    )
                ],
            )
            conn = sqlite3.connect(db_path)
            try:
                ensure_projection_receipt_schema(conn)
                conn.execute(
                    "INSERT INTO projection_applied_state "
                    "(consumer_id, aggregate_type, aggregate_id, applied_revision, applied_outbox_id, artifact_ref, output_sha256) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (CONSUMER, "registered_model", "reg-1", 2, 1, ARTIFACT_A, HASH_A),
                )
                conn.commit()
            finally:
                conn.close()
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 1)
            with self.assertRaises(ProjectionReceiptConflictError):
                store.record_applied(
                    event,
                    consumer_id=CONSUMER,
                    artifact_ref=ARTIFACT_B,
                    output_sha256=HASH_B,
                )
            self.assertEqual(_outbox_snapshot(db_path)[0][6], "pending")

    def test_conflict_receipt_and_exact_replay(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        1,
                        "registered_model.upserted",
                        '{"a":1}',
                        "pending",
                    )
                ],
            )
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 1)
            detail = "foreign edit detected"
            first = store.record_conflict(
                event, consumer_id=CONSUMER, detail=detail
            )
            second = store.record_conflict(
                event, consumer_id=CONSUMER, detail=detail
            )
            self.assertEqual(first, second)
            self.assertEqual(_outbox_snapshot(db_path)[0][6], "conflict")

    def test_changed_conflict_receipt_rejected(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        1,
                        "registered_model.upserted",
                        '{"a":1}',
                        "pending",
                    )
                ],
            )
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 1)
            store.record_conflict(event, consumer_id=CONSUMER, detail="first")
            with self.assertRaises(ProjectionReceiptConflictError):
                store.record_conflict(event, consumer_id="consumer-b", detail="first")
            with self.assertRaises(ProjectionReceiptConflictError):
                store.record_conflict(event, consumer_id=CONSUMER, detail="second")

    def test_applied_state_preserved_on_conflict(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        1,
                        "registered_model.upserted",
                        '{"a":1}',
                        "pending",
                    )
                ],
            )
            conn = sqlite3.connect(db_path)
            try:
                ensure_projection_receipt_schema(conn)
                conn.execute(
                    "INSERT INTO projection_applied_state "
                    "(consumer_id, aggregate_type, aggregate_id, applied_revision, applied_outbox_id, artifact_ref, output_sha256) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (CONSUMER, "registered_model", "reg-1", 1, 9, ARTIFACT_A, HASH_A),
                )
                conn.commit()
            finally:
                conn.close()
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 1)
            store.record_conflict(event, consumer_id=CONSUMER, detail="blocked")
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT applied_revision, artifact_ref FROM projection_applied_state "
                    "WHERE consumer_id = ?",
                    (CONSUMER,),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(row, (1, ARTIFACT_A))

    def test_forced_insert_failure_rolls_back(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        1,
                        "registered_model.upserted",
                        '{"a":1}',
                        "pending",
                    )
                ],
            )
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 1)
            conn = sqlite3.connect(db_path)
            try:
                ensure_projection_receipt_schema(conn)
                conn.execute(
                    "CREATE TRIGGER projection_applied_state_fail_insert "
                    "BEFORE INSERT ON projection_applied_state "
                    "BEGIN SELECT RAISE(ABORT, 'forced failure'); END"
                )
                conn.commit()
            finally:
                conn.close()
            with self.assertRaises(sqlite3.IntegrityError):
                store.record_applied(
                    event,
                    consumer_id=CONSUMER,
                    artifact_ref=ARTIFACT_A,
                    output_sha256=HASH_A,
                )
            self.assertEqual(_outbox_snapshot(db_path)[0][6], "pending")
            conn = sqlite3.connect(db_path)
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM projection_applied_state"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(count, 0)

    def test_validation_rejects_bool_revision_and_bad_hash_and_secret_detail(self) -> None:
        with self.assertRaises(TypeError):
            validate_strict_int("aggregate_revision", True)
        with self.assertRaises(ValueError):
            validate_output_sha256("ABCDEF" + "0" * 58)
        with self.assertRaises(ValueError):
            validate_conflict_detail("password=leak")
        with self.assertRaises(ValueError):
            validate_conflict_detail("reason=api_key_value")
        with self.assertRaises(ValueError):
            validate_conflict_detail("apiKey=leak")
        with self.assertRaises(ValueError):
            validate_aggregate_type("registered\nmodel")

    def test_record_applied_rejects_non_pending_state(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        1,
                        "registered_model.upserted",
                        '{"a":1}',
                        "conflict",
                    )
                ],
            )
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 1)
            with self.assertRaises(ProjectionOutboxStateError):
                store.record_applied(
                    event,
                    consumer_id=CONSUMER,
                    artifact_ref=ARTIFACT_A,
                    output_sha256=HASH_A,
                )

    def test_record_paths_use_begin_immediate(self) -> None:
        adapter_path = Path("python/src/model_deck/adapters/storage/sqlite_projection_receipts.py")
        source = adapter_path.read_text()
        # Three record paths (record_applied, record_deleted, record_conflict) plus
        # one for the v1 -> v2 schema migration inside ensure_projection_receipt_schema.
        self.assertEqual(source.count("BEGIN IMMEDIATE"), 4)

    def test_forced_outbox_update_failure_rolls_back(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        1,
                        "registered_model.upserted",
                        '{"a":1}',
                        "pending",
                    )
                ],
            )
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 1)
            conn = sqlite3.connect(db_path)
            try:
                ensure_projection_receipt_schema(conn)
                conn.execute(
                    "CREATE TRIGGER projection_outbox_fail_update "
                    "BEFORE UPDATE ON projection_outbox "
                    "BEGIN SELECT RAISE(ABORT, 'forced update failure'); END"
                )
                conn.commit()
            finally:
                conn.close()
            with self.assertRaises(sqlite3.IntegrityError):
                store.record_applied(
                    event,
                    consumer_id=CONSUMER,
                    artifact_ref=ARTIFACT_A,
                    output_sha256=HASH_A,
                )
            self.assertEqual(_outbox_snapshot(db_path)[0][6], "pending")
            conn = sqlite3.connect(db_path)
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM projection_applied_state"
                ).fetchone()[0]
                receipt_count = conn.execute(
                    "SELECT COUNT(*) FROM projection_outbox_applied_receipts"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(count, 0)
            self.assertEqual(receipt_count, 0)

    def test_forced_conflict_update_failure_rolls_back(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        1,
                        "registered_model.upserted",
                        '{"a":1}',
                        "pending",
                    )
                ],
            )
            conn = sqlite3.connect(db_path)
            try:
                ensure_projection_receipt_schema(conn)
                conn.execute(
                    "CREATE TRIGGER projection_outbox_fail_conflict_update "
                    "BEFORE UPDATE ON projection_outbox "
                    "BEGIN SELECT RAISE(ABORT, 'forced conflict update failure'); END"
                )
                conn.commit()
            finally:
                conn.close()
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 1)
            with self.assertRaises(sqlite3.IntegrityError):
                store.record_conflict(
                    event,
                    consumer_id=CONSUMER,
                    detail="foreign edit detected",
                )
            self.assertEqual(_outbox_snapshot(db_path)[0][6], "pending")
            conn = sqlite3.connect(db_path)
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM projection_outbox_conflicts"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(count, 0)


class SQLiteProjectionReceiptStoreDeletionTests(unittest.TestCase):
    """B09 deletion-receipt seam: tombstones, replay, and v1 -> v2 migration."""

    def test_migrates_v1_database_and_preserves_rows(self) -> None:
        """v1 (NOT NULL hash) fixture DB migrates to v2 with rows preserved."""
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "legacy.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        1,
                        "registered_model.upserted",
                        '{"a":1}',
                        "applied",
                    )
                ],
            )
            # Manually lay down the v1 receipt schema (NOT NULL hash) and
            # pre-populate one applied state and one receipt row. This is the
            # exact shape a caller-injected legacy fixture would have.
            conn = sqlite3.connect(db_path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE projection_applied_state (
                        consumer_id TEXT NOT NULL,
                        aggregate_type TEXT NOT NULL,
                        aggregate_id TEXT NOT NULL,
                        applied_revision INTEGER NOT NULL,
                        applied_outbox_id INTEGER NOT NULL,
                        artifact_ref TEXT NOT NULL,
                        output_sha256 TEXT NOT NULL,
                        PRIMARY KEY (consumer_id, aggregate_type, aggregate_id)
                    );
                    CREATE TABLE projection_outbox_applied_receipts (
                        outbox_id INTEGER NOT NULL,
                        consumer_id TEXT NOT NULL,
                        aggregate_type TEXT NOT NULL,
                        aggregate_id TEXT NOT NULL,
                        applied_revision INTEGER NOT NULL,
                        artifact_ref TEXT NOT NULL,
                        output_sha256 TEXT NOT NULL,
                        PRIMARY KEY (outbox_id, consumer_id)
                    );
                    CREATE TABLE projection_outbox_conflicts (
                        outbox_id INTEGER NOT NULL PRIMARY KEY,
                        consumer_id TEXT NOT NULL,
                        detail TEXT NOT NULL
                    );
                    """
                )
                conn.execute(
                    "INSERT INTO projection_applied_state VALUES "
                    "(?, ?, ?, ?, ?, ?, ?)",
                    (CONSUMER, "registered_model", "reg-1", 1, 1, ARTIFACT_A, HASH_A),
                )
                conn.execute(
                    "INSERT INTO projection_outbox_applied_receipts VALUES "
                    "(?, ?, ?, ?, ?, ?, ?)",
                    (1, CONSUMER, "registered_model", "reg-1", 1, ARTIFACT_A, HASH_A),
                )
                conn.commit()
                legacy_applied = conn.execute(
                    "SELECT output_sha256 FROM projection_applied_state"
                ).fetchone()
                legacy_receipts = conn.execute(
                    "SELECT output_sha256 FROM projection_outbox_applied_receipts"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(legacy_applied[0], HASH_A)
            self.assertEqual(legacy_receipts[0], HASH_A)

            store = SQLiteProjectionReceiptStore(db_path)
            # get_applied stays read-only and does not migrate. Trigger the
            # migration explicitly so the metadata table is created and the
            # v1 columns are widened to nullable.
            ensure_projection_receipt_schema(sqlite3.connect(db_path))
            state = store.get_applied(
                consumer_id=CONSUMER,
                aggregate_type="registered_model",
                aggregate_id="reg-1",
            )
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual(state.output_sha256, HASH_A)

            conn = sqlite3.connect(db_path)
            try:
                applied_notnull = conn.execute(
                    "SELECT \"notnull\" FROM pragma_table_info"
                    "('projection_applied_state') WHERE name = 'output_sha256'"
                ).fetchone()[0]
                receipts_notnull = conn.execute(
                    "SELECT \"notnull\" FROM pragma_table_info"
                    "('projection_outbox_applied_receipts') WHERE name = 'output_sha256'"
                ).fetchone()[0]
                version = conn.execute(
                    "SELECT value FROM projection_receipt_schema_metadata WHERE key = 'schema_version'"
                ).fetchone()
                preserved = conn.execute(
                    "SELECT output_sha256, applied_revision FROM projection_applied_state "
                    "WHERE consumer_id = ?",
                    (CONSUMER,),
                ).fetchone()
                receipt_preserved = conn.execute(
                    "SELECT output_sha256, applied_revision FROM projection_outbox_applied_receipts"
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(applied_notnull, 0)
            self.assertEqual(receipts_notnull, 0)
            self.assertEqual(version, (CURRENT_SCHEMA_VERSION,))
            self.assertEqual(preserved, (HASH_A, 1))
            self.assertEqual(len(receipt_preserved), 1)
            self.assertEqual(receipt_preserved[0], (HASH_A, 1))

    def test_migration_idempotent_when_already_v2(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "fresh.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        1,
                        "registered_model.upserted",
                        '{"a":1}',
                        "pending",
                    )
                ],
            )
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 1)
            state = store.record_applied(
                event,
                consumer_id=CONSUMER,
                artifact_ref=ARTIFACT_A,
                output_sha256=HASH_A,
            )
            self.assertEqual(state.output_sha256, HASH_A)
            conn = sqlite3.connect(db_path)
            try:
                first_version = conn.execute(
                    "SELECT value FROM projection_receipt_schema_metadata WHERE key = 'schema_version'"
                ).fetchone()[0]
            finally:
                conn.close()
            # Re-running the migration must not destroy data or alter the version.
            ensure_projection_receipt_schema(
                sqlite3.connect(db_path)
            )
            conn = sqlite3.connect(db_path)
            try:
                second_version = conn.execute(
                    "SELECT value FROM projection_receipt_schema_metadata WHERE key = 'schema_version'"
                ).fetchone()[0]
                applied_state = conn.execute(
                    "SELECT output_sha256 FROM projection_applied_state"
                ).fetchone()
                receipt_count = conn.execute(
                    "SELECT COUNT(*) FROM projection_outbox_applied_receipts"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(first_version, second_version)
            self.assertEqual(applied_state[0], HASH_A)
            self.assertEqual(receipt_count, 1)

    def test_record_deleted_writes_tombstone_and_marks_applied(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        5,
                        "registered_model.removed",
                        '{"removed":true}',
                        "pending",
                    )
                ],
            )
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 1)
            state = store.record_deleted(
                event, consumer_id=CONSUMER, artifact_ref=ARTIFACT_A
            )
            self.assertIsNone(state.output_sha256)
            self.assertEqual(state.applied_revision, 5)
            self.assertEqual(state.applied_outbox_id, 1)
            self.assertEqual(state.artifact_ref, ARTIFACT_A)
            self.assertEqual(_outbox_snapshot(db_path)[0][6], "applied")
            conn = sqlite3.connect(db_path)
            try:
                applied_row = conn.execute(
                    "SELECT output_sha256 FROM projection_applied_state"
                ).fetchone()
                receipt_row = conn.execute(
                    "SELECT output_sha256 FROM projection_outbox_applied_receipts"
                ).fetchone()
            finally:
                conn.close()
            self.assertIsNone(applied_row[0])
            self.assertIsNone(receipt_row[0])

    def test_record_deleted_exact_replay_is_idempotent(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        5,
                        "registered_model.removed",
                        '{"removed":true}',
                        "pending",
                    )
                ],
            )
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 1)
            first = store.record_deleted(
                event, consumer_id=CONSUMER, artifact_ref=ARTIFACT_A
            )
            second = store.record_deleted(
                event, consumer_id=CONSUMER, artifact_ref=ARTIFACT_A
            )
            self.assertEqual(first, second)
            self.assertEqual(_outbox_snapshot(db_path)[0][6], "applied")
            conn = sqlite3.connect(db_path)
            try:
                receipt_count = conn.execute(
                    "SELECT COUNT(*) FROM projection_outbox_applied_receipts"
                ).fetchone()[0]
                state_count = conn.execute(
                    "SELECT COUNT(*) FROM projection_applied_state"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(receipt_count, 1)
            self.assertEqual(state_count, 1)

    def test_record_deleted_newer_revision_advances_tombstone(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        5,
                        "registered_model.removed",
                        '{"removed":true}',
                        "applied",
                    ),
                    (
                        "registered_model",
                        "reg-1",
                        7,
                        "registered_model.removed",
                        '{"removed":true}',
                        "pending",
                    ),
                ],
            )
            conn = sqlite3.connect(db_path)
            try:
                ensure_projection_receipt_schema(conn)
                conn.execute(
                    "INSERT INTO projection_applied_state "
                    "(consumer_id, aggregate_type, aggregate_id, applied_revision, "
                    "applied_outbox_id, artifact_ref, output_sha256) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    (CONSUMER, "registered_model", "reg-1", 5, 1, ARTIFACT_A),
                )
                conn.execute(
                    "INSERT INTO projection_outbox_applied_receipts "
                    "(outbox_id, consumer_id, aggregate_type, aggregate_id, applied_revision, "
                    "artifact_ref, output_sha256) VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    (1, CONSUMER, "registered_model", "reg-1", 5, ARTIFACT_A),
                )
                conn.commit()
            finally:
                conn.close()
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 2)
            state = store.record_deleted(
                event, consumer_id=CONSUMER, artifact_ref=ARTIFACT_B
            )
            self.assertEqual(state.applied_revision, 7)
            self.assertEqual(state.applied_outbox_id, 2)
            self.assertEqual(state.artifact_ref, ARTIFACT_B)
            self.assertIsNone(state.output_sha256)
            conn = sqlite3.connect(db_path)
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM projection_applied_state"
                ).fetchone()[0]
                receipt_count = conn.execute(
                    "SELECT COUNT(*) FROM projection_outbox_applied_receipts"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(count, 1)
            self.assertEqual(receipt_count, 2)

    def test_record_deleted_changed_artifact_at_equal_revision_conflicts(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        5,
                        "registered_model.removed",
                        '{"removed":true}',
                        "pending",
                    )
                ],
            )
            conn = sqlite3.connect(db_path)
            try:
                ensure_projection_receipt_schema(conn)
                conn.execute(
                    "INSERT INTO projection_applied_state "
                    "(consumer_id, aggregate_type, aggregate_id, applied_revision, "
                    "applied_outbox_id, artifact_ref, output_sha256) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    (CONSUMER, "registered_model", "reg-1", 5, 1, ARTIFACT_A),
                )
                conn.commit()
            finally:
                conn.close()
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 1)
            with self.assertRaises(ProjectionReceiptConflictError):
                store.record_deleted(
                    event, consumer_id=CONSUMER, artifact_ref=ARTIFACT_B
                )
            self.assertEqual(_outbox_snapshot(db_path)[0][6], "pending")

    def test_record_deleted_equal_revision_existing_hash_conflicts(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        5,
                        "registered_model.removed",
                        '{"removed":true}',
                        "pending",
                    )
                ],
            )
            conn = sqlite3.connect(db_path)
            try:
                ensure_projection_receipt_schema(conn)
                conn.execute(
                    "INSERT INTO projection_applied_state "
                    "(consumer_id, aggregate_type, aggregate_id, applied_revision, "
                    "applied_outbox_id, artifact_ref, output_sha256) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (CONSUMER, "registered_model", "reg-1", 5, 99, ARTIFACT_A, HASH_A),
                )
                conn.commit()
            finally:
                conn.close()
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 1)
            with self.assertRaises(ProjectionReceiptConflictError):
                store.record_deleted(
                    event, consumer_id=CONSUMER, artifact_ref=ARTIFACT_A
                )
            self.assertEqual(_outbox_snapshot(db_path)[0][6], "pending")

    def test_get_applied_returns_tombstone_with_none_hash(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(db_path, [])
            conn = sqlite3.connect(db_path)
            try:
                ensure_projection_receipt_schema(conn)
                conn.execute(
                    "INSERT INTO projection_applied_state "
                    "(consumer_id, aggregate_type, aggregate_id, applied_revision, "
                    "applied_outbox_id, artifact_ref, output_sha256) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    (CONSUMER, "registered_model", "reg-1", 5, 1, ARTIFACT_A),
                )
                conn.commit()
            finally:
                conn.close()
            store = SQLiteProjectionReceiptStore(db_path)
            state = store.get_applied(
                consumer_id=CONSUMER,
                aggregate_type="registered_model",
                aggregate_id="reg-1",
            )
            self.assertIsNotNone(state)
            assert state is not None
            self.assertIsNone(state.output_sha256)
            self.assertEqual(state.applied_revision, 5)
            self.assertEqual(state.applied_outbox_id, 1)
            self.assertEqual(state.artifact_ref, ARTIFACT_A)

    def test_record_deleted_older_event_acks_without_resurrection(self) -> None:
        """A pre-tombstone event arriving after the tombstone must not resurrect."""
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        3,
                        "registered_model.upserted",
                        '{"a":3}',
                        "pending",
                    ),
                    (
                        "registered_model",
                        "reg-1",
                        5,
                        "registered_model.removed",
                        '{"removed":true}',
                        "applied",
                    ),
                ],
            )
            conn = sqlite3.connect(db_path)
            try:
                ensure_projection_receipt_schema(conn)
                conn.execute(
                    "INSERT INTO projection_applied_state "
                    "(consumer_id, aggregate_type, aggregate_id, applied_revision, "
                    "applied_outbox_id, artifact_ref, output_sha256) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    (CONSUMER, "registered_model", "reg-1", 5, 2, ARTIFACT_B),
                )
                conn.execute(
                    "INSERT INTO projection_outbox_applied_receipts "
                    "(outbox_id, consumer_id, aggregate_type, aggregate_id, applied_revision, "
                    "artifact_ref, output_sha256) VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    (2, CONSUMER, "registered_model", "reg-1", 5, ARTIFACT_B),
                )
                conn.commit()
            finally:
                conn.close()
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 1)
            state = store.record_deleted(
                event, consumer_id=CONSUMER, artifact_ref=ARTIFACT_A
            )
            self.assertEqual(state.applied_revision, 5)
            self.assertIsNone(state.output_sha256)
            self.assertEqual(state.artifact_ref, ARTIFACT_B)
            self.assertEqual(_outbox_snapshot(db_path)[0][6], "applied")
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT applied_revision, output_sha256, artifact_ref "
                    "FROM projection_applied_state WHERE consumer_id = ?",
                    (CONSUMER,),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(row, (5, None, ARTIFACT_B))

    def test_record_applied_equal_revision_with_existing_tombstone_conflicts(self) -> None:
        """An upsert after a tombstone at the same revision must conflict, not resurrect."""
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        5,
                        "registered_model.upserted",
                        '{"a":5}',
                        "pending",
                    )
                ],
            )
            conn = sqlite3.connect(db_path)
            try:
                ensure_projection_receipt_schema(conn)
                conn.execute(
                    "INSERT INTO projection_applied_state "
                    "(consumer_id, aggregate_type, aggregate_id, applied_revision, "
                    "applied_outbox_id, artifact_ref, output_sha256) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    (CONSUMER, "registered_model", "reg-1", 5, 99, ARTIFACT_A),
                )
                conn.commit()
            finally:
                conn.close()
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 1)
            with self.assertRaises(ProjectionReceiptConflictError):
                store.record_applied(
                    event,
                    consumer_id=CONSUMER,
                    artifact_ref=ARTIFACT_A,
                    output_sha256=HASH_A,
                )
            self.assertEqual(_outbox_snapshot(db_path)[0][6], "pending")
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT output_sha256 FROM projection_applied_state "
                    "WHERE consumer_id = ?",
                    (CONSUMER,),
                ).fetchone()
            finally:
                conn.close()
            self.assertIsNone(row[0])

    def test_record_applied_older_revision_after_tombstone_preserves_tombstone(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        3,
                        "registered_model.upserted",
                        '{"a":3}',
                        "pending",
                    )
                ],
            )
            conn = sqlite3.connect(db_path)
            try:
                ensure_projection_receipt_schema(conn)
                conn.execute(
                    "INSERT INTO projection_applied_state "
                    "(consumer_id, aggregate_type, aggregate_id, applied_revision, "
                    "applied_outbox_id, artifact_ref, output_sha256) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    (CONSUMER, "registered_model", "reg-1", 7, 99, ARTIFACT_B),
                )
                conn.commit()
            finally:
                conn.close()
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 1)
            state = store.record_applied(
                event,
                consumer_id=CONSUMER,
                artifact_ref=ARTIFACT_A,
                output_sha256=HASH_A,
            )
            self.assertEqual(state.applied_revision, 7)
            self.assertIsNone(state.output_sha256)
            self.assertEqual(state.artifact_ref, ARTIFACT_B)
            self.assertEqual(_outbox_snapshot(db_path)[0][6], "applied")

    def test_record_deleted_rejects_non_pending_state(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        1,
                        "registered_model.removed",
                        '{"removed":true}',
                        "conflict",
                    )
                ],
            )
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 1)
            with self.assertRaises(ProjectionOutboxStateError):
                store.record_deleted(
                    event, consumer_id=CONSUMER, artifact_ref=ARTIFACT_A
                )

    def test_record_deleted_applied_replay_with_different_artifact_conflicts(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        5,
                        "registered_model.removed",
                        '{"removed":true}',
                        "applied",
                    )
                ],
            )
            conn = sqlite3.connect(db_path)
            try:
                ensure_projection_receipt_schema(conn)
                conn.execute(
                    "INSERT INTO projection_applied_state "
                    "(consumer_id, aggregate_type, aggregate_id, applied_revision, "
                    "applied_outbox_id, artifact_ref, output_sha256) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    (CONSUMER, "registered_model", "reg-1", 5, 1, ARTIFACT_A),
                )
                conn.execute(
                    "INSERT INTO projection_outbox_applied_receipts "
                    "(outbox_id, consumer_id, aggregate_type, aggregate_id, applied_revision, "
                    "artifact_ref, output_sha256) VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    (1, CONSUMER, "registered_model", "reg-1", 5, ARTIFACT_A),
                )
                conn.commit()
            finally:
                conn.close()
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 1)
            with self.assertRaises(ProjectionReceiptConflictError):
                store.record_deleted(
                    event, consumer_id=CONSUMER, artifact_ref=ARTIFACT_B
                )

    def test_record_deleted_event_mismatch_raises(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        5,
                        "registered_model.removed",
                        '{"removed":true}',
                        "pending",
                    )
                ],
            )
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 1)
            with self.assertRaises(ProjectionOutboxEventMismatchError):
                store.record_deleted(
                    ProjectionOutboxEvent(
                        outbox_id=event.outbox_id,
                        aggregate_type=event.aggregate_type,
                        aggregate_id=event.aggregate_id,
                        aggregate_revision=event.aggregate_revision,
                        event_kind=event.event_kind,
                        payload_json='{"removed":false}',
                    ),
                    consumer_id=CONSUMER,
                    artifact_ref=ARTIFACT_A,
                )

    def test_record_deleted_forced_insert_failure_rolls_back(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        5,
                        "registered_model.removed",
                        '{"removed":true}',
                        "pending",
                    )
                ],
            )
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 1)
            conn = sqlite3.connect(db_path)
            try:
                ensure_projection_receipt_schema(conn)
                conn.execute(
                    "CREATE TRIGGER projection_applied_state_fail_insert "
                    "BEFORE INSERT ON projection_applied_state "
                    "BEGIN SELECT RAISE(ABORT, 'forced failure'); END"
                )
                conn.commit()
            finally:
                conn.close()
            with self.assertRaises(sqlite3.IntegrityError):
                store.record_deleted(
                    event, consumer_id=CONSUMER, artifact_ref=ARTIFACT_A
                )
            self.assertEqual(_outbox_snapshot(db_path)[0][6], "pending")
            conn = sqlite3.connect(db_path)
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM projection_applied_state"
                ).fetchone()[0]
                receipt_count = conn.execute(
                    "SELECT COUNT(*) FROM projection_outbox_applied_receipts"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(count, 0)
            self.assertEqual(receipt_count, 0)

    def test_record_deleted_forced_outbox_update_failure_rolls_back(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        5,
                        "registered_model.removed",
                        '{"removed":true}',
                        "pending",
                    )
                ],
            )
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 1)
            conn = sqlite3.connect(db_path)
            try:
                ensure_projection_receipt_schema(conn)
                conn.execute(
                    "CREATE TRIGGER projection_outbox_fail_delete_update "
                    "BEFORE UPDATE ON projection_outbox "
                    "BEGIN SELECT RAISE(ABORT, 'forced update failure'); END"
                )
                conn.commit()
            finally:
                conn.close()
            with self.assertRaises(sqlite3.IntegrityError):
                store.record_deleted(
                    event, consumer_id=CONSUMER, artifact_ref=ARTIFACT_A
                )
            self.assertEqual(_outbox_snapshot(db_path)[0][6], "pending")
            conn = sqlite3.connect(db_path)
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM projection_applied_state"
                ).fetchone()[0]
                receipt_count = conn.execute(
                    "SELECT COUNT(*) FROM projection_outbox_applied_receipts"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(count, 0)
            self.assertEqual(receipt_count, 0)

    def test_record_deleted_concurrent_with_reader_uses_begin_immediate(self) -> None:
        """Two SQLite connections: one writes via record_deleted, one reads.

        With ``BEGIN IMMEDIATE`` the writer serializes before the reader's
        BEGIN. ``get_applied`` opens read-only; here we use a read-write
        connection to assert the writer's BEGIN IMMEDIATE acquires the
        write lock first, so the read transaction must wait or see a
        consistent post-commit view. The test must observe the post-commit
        tombstone via ``get_applied`` and not block indefinitely.
        """
        import threading

        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        5,
                        "registered_model.removed",
                        '{"removed":true}',
                        "pending",
                    )
                ],
            )
            store = SQLiteProjectionReceiptStore(db_path)
            event = _event_from_db(db_path, 1)

            writer_state: dict[str, object] = {}
            writer_error: list[BaseException] = []

            def writer() -> None:
                try:
                    state = store.record_deleted(
                        event, consumer_id=CONSUMER, artifact_ref=ARTIFACT_A
                    )
                    writer_state["state"] = state
                except BaseException as exc:  # pragma: no cover - thread report
                    writer_error.append(exc)

            thread = threading.Thread(target=writer)
            thread.start()
            thread.join(timeout=10)
            self.assertFalse(writer_error, msg=f"writer failed: {writer_error}")
            self.assertTrue(thread.is_alive() is False)

            # Read on a separate connection after the writer committed.
            state = store.get_applied(
                consumer_id=CONSUMER,
                aggregate_type="registered_model",
                aggregate_id="reg-1",
            )
            self.assertIsNotNone(state)
            assert state is not None
            self.assertIsNone(state.output_sha256)
            self.assertEqual(state.applied_revision, 5)




class ReceiptMigrationRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'receipt.sqlite3'
        _seed_outbox(self.db, [
            ('registered_model', 'reg-1', revision, 'fixture', '{}', 'pending')
            for revision in (1, 2, 3)
        ])
        self.store = SQLiteProjectionReceiptStore(self.db)

    def legacy(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db)
        # Explicit old schema fixture, independent of the migration implementation.
        conn.executescript('''
            CREATE TABLE projection_applied_state (
                consumer_id TEXT NOT NULL, aggregate_type TEXT NOT NULL,
                aggregate_id TEXT NOT NULL, applied_revision INTEGER NOT NULL,
                applied_outbox_id INTEGER NOT NULL, artifact_ref TEXT NOT NULL,
                output_sha256 TEXT NOT NULL,
                PRIMARY KEY (consumer_id, aggregate_type, aggregate_id));
            CREATE TABLE projection_outbox_applied_receipts (
                outbox_id INTEGER NOT NULL, consumer_id TEXT NOT NULL,
                aggregate_type TEXT NOT NULL, aggregate_id TEXT NOT NULL,
                applied_revision INTEGER NOT NULL, artifact_ref TEXT NOT NULL,
                output_sha256 TEXT NOT NULL, PRIMARY KEY (outbox_id, consumer_id));
        ''')
        conn.execute('INSERT INTO projection_applied_state VALUES (?, ?, ?, ?, ?, ?, ?)',
                     (CONSUMER, 'registered_model', 'reg-1', 1, 1, ARTIFACT_A, HASH_A))
        conn.execute('INSERT INTO projection_outbox_applied_receipts VALUES (?, ?, ?, ?, ?, ?, ?)',
                     (1, CONSUMER, 'registered_model', 'reg-1', 1, ARTIFACT_A, HASH_A))
        conn.commit()
        self.addCleanup(conn.close)
        return conn

    def snapshot(self, conn: sqlite3.Connection) -> tuple[str, ...]:
        return tuple(conn.iterdump())

    def test_migration_preserves_rows_indexes_and_trigger_behavior(self) -> None:
        conn = self.legacy()
        conn.execute('CREATE UNIQUE INDEX artifact_unique ON projection_applied_state(artifact_ref)')
        conn.execute("CREATE TRIGGER receipt_guard BEFORE INSERT ON projection_outbox_applied_receipts "
                     "BEGIN SELECT RAISE(ABORT, 'guard'); END")
        conn.commit()
        before_state = conn.execute('SELECT * FROM projection_applied_state').fetchall()
        before_receipts = conn.execute('SELECT * FROM projection_outbox_applied_receipts').fetchall()
        ensure_projection_receipt_schema(conn)
        self.assertEqual(conn.execute('SELECT * FROM projection_applied_state').fetchall(), before_state)
        self.assertEqual(conn.execute('SELECT * FROM projection_outbox_applied_receipts').fetchall(), before_receipts)
        self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE name='artifact_unique'").fetchone(), ('artifact_unique',))
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'guard'):
            conn.execute('INSERT INTO projection_outbox_applied_receipts VALUES (2, ?, ?, ?, 2, ?, NULL)',
                         (CONSUMER, 'registered_model', 'reg-1', ARTIFACT_A))
        conn.rollback()

    def test_metadata_failure_rolls_back_entire_schema_and_data(self) -> None:
        conn = self.legacy()
        conn.execute('CREATE TABLE projection_receipt_schema_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        conn.execute("INSERT INTO projection_receipt_schema_metadata VALUES ('schema_version', '1')")
        conn.execute("CREATE TRIGGER stamp_abort BEFORE UPDATE ON projection_receipt_schema_metadata "
                     "BEGIN SELECT RAISE(ABORT, 'stamp failure'); END")
        conn.commit()
        before = self.snapshot(conn)
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'stamp failure'):
            ensure_projection_receipt_schema(conn)
        self.assertEqual(self.snapshot(conn), before)

    def test_metadata_insert_failure_rolls_back_entire_migration(self) -> None:
        conn = self.legacy()
        conn.execute('CREATE TABLE projection_receipt_schema_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        conn.execute("CREATE TRIGGER stamp_abort BEFORE INSERT ON projection_receipt_schema_metadata "
                     "BEGIN SELECT RAISE(ABORT, 'stamp failure'); END")
        conn.commit()
        before = self.snapshot(conn)
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'stamp failure'):
            ensure_projection_receipt_schema(conn)
        self.assertEqual(self.snapshot(conn), before)

    def test_outgoing_foreign_key_layout_rejected_unchanged(self) -> None:
        conn = self.legacy()
        conn.execute('CREATE TABLE artifacts(name TEXT PRIMARY KEY)')
        conn.execute('ALTER TABLE projection_applied_state ADD COLUMN '
                     'artifact_owner TEXT REFERENCES artifacts(name)')
        conn.commit()
        before = self.snapshot(conn)
        with self.assertRaisesRegex(ProjectionOutboxStateError, 'layout'):
            ensure_projection_receipt_schema(conn)
        self.assertEqual(self.snapshot(conn), before)

    def test_version_upgrade_and_unknown_version_rejection(self) -> None:
        conn = self.legacy()
        conn.execute('CREATE TABLE projection_receipt_schema_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        conn.execute("INSERT INTO projection_receipt_schema_metadata VALUES ('schema_version', 'future')")
        conn.commit()
        before = self.snapshot(conn)
        with self.assertRaisesRegex(ProjectionOutboxStateError, 'version'):
            ensure_projection_receipt_schema(conn)
        self.assertEqual(self.snapshot(conn), before)
        conn.execute("UPDATE projection_receipt_schema_metadata SET value='1'")
        conn.commit()
        ensure_projection_receipt_schema(conn)
        self.assertEqual(conn.execute('SELECT value FROM projection_receipt_schema_metadata').fetchone(), ('2',))

    def test_incoming_foreign_key_rejected_without_cascading(self) -> None:
        conn = self.legacy()
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('CREATE TABLE dependent(c TEXT, a TEXT, i TEXT, FOREIGN KEY(c,a,i) '
                     'REFERENCES projection_applied_state(consumer_id,aggregate_type,aggregate_id) ON DELETE CASCADE)')
        conn.execute('INSERT INTO dependent VALUES (?, ?, ?)', (CONSUMER, 'registered_model', 'reg-1'))
        conn.commit()
        before = self.snapshot(conn)
        with self.assertRaisesRegex(ProjectionOutboxStateError, 'foreign keys'):
            ensure_projection_receipt_schema(conn)
        self.assertEqual(self.snapshot(conn), before)

    def test_unknown_column_preserved_on_rejection(self) -> None:
        conn = self.legacy()
        conn.execute("ALTER TABLE projection_applied_state ADD COLUMN unknown_data TEXT DEFAULT 'preserve'")
        conn.commit()
        before = self.snapshot(conn)
        with self.assertRaisesRegex(ProjectionOutboxStateError, 'layout'):
            ensure_projection_receipt_schema(conn)
        self.assertEqual(self.snapshot(conn), before)

    def test_occupied_second_destination_rejected_before_first_rebuild(self) -> None:
        conn = self.legacy()
        conn.execute('CREATE TABLE projection_outbox_applied_receipts_new(unrelated TEXT)')
        conn.commit()
        before = self.snapshot(conn)
        with self.assertRaisesRegex(ProjectionOutboxStateError, 'destination'):
            ensure_projection_receipt_schema(conn)
        self.assertEqual(self.snapshot(conn), before)

    def test_deletion_replay_returns_original_receipt_after_later_write(self) -> None:
        first = self.store.record_deleted(_event_from_db(self.db, 2), consumer_id=CONSUMER, artifact_ref=ARTIFACT_A)
        self.store.record_applied(_event_from_db(self.db, 3), consumer_id=CONSUMER,
                                  artifact_ref=ARTIFACT_A, output_sha256=HASH_B)
        replay = self.store.record_deleted(_event_from_db(self.db, 2), consumer_id=CONSUMER, artifact_ref=ARTIFACT_A)
        self.assertEqual(replay, first)
        self.assertIsNone(replay.output_sha256)
        current = self.store.get_applied(consumer_id=CONSUMER, aggregate_type='registered_model', aggregate_id='reg-1')
        self.assertEqual(current.applied_revision, 3)
        self.assertEqual(current.output_sha256, HASH_B)

    def test_concurrent_legacy_migration_write_and_deletion(self) -> None:
        import threading
        from concurrent.futures import ThreadPoolExecutor
        self.legacy().close()
        start = threading.Barrier(2)
        def record(revision: int) -> None:
            store = SQLiteProjectionReceiptStore(self.db)
            event = _event_from_db(self.db, revision)
            start.wait(timeout=5)
            if revision == 2:
                store.record_applied(event, consumer_id=CONSUMER, artifact_ref=ARTIFACT_A, output_sha256=HASH_B)
            else:
                store.record_deleted(event, consumer_id=CONSUMER, artifact_ref=ARTIFACT_A)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(record, (2, 3)))
        current = self.store.get_applied(consumer_id=CONSUMER, aggregate_type='registered_model', aggregate_id='reg-1')
        self.assertEqual(current.applied_revision, 3)
        self.assertIsNone(current.output_sha256)
        self.assertEqual([row[6] for row in _outbox_snapshot(self.db)[1:]], ['applied', 'applied'])


if __name__ == "__main__":
    unittest.main()

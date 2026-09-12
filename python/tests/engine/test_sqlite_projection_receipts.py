from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from model_deck.adapters.storage.sqlite_outbox import ensure_projection_outbox_schema
from model_deck.adapters.storage.sqlite_projection_receipts import (
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
        self.assertEqual(source.count("BEGIN IMMEDIATE"), 2)

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


if __name__ == "__main__":
    unittest.main()

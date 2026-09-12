from __future__ import annotations

import sqlite3
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from model_deck.adapters.storage.sqlite_outbox import ensure_projection_outbox_schema
from model_deck.adapters.storage.sqlite_projection_intents import (
    SQLiteProjectionMutationIntentJournal,
)
from model_deck.engine.projections.intents import (
    OPERATION_DELETE,
    OPERATION_WRITE,
    ProjectionMutationIntent,
    compute_payload_sha256,
    validate_operation,
)
from model_deck.engine.projections.ports import ProjectionOutboxEvent
from model_deck.engine.projections.receipts import (
    ProjectionOutboxEventMismatchError,
    ProjectionOutboxStateError,
    ProjectionReceiptConflictError,
    validate_strict_int,
)

HASH_A = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
HASH_B = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
CONSUMER = "consumer-a"
ARTIFACT_A = "artifact:alpha"
ARTIFACT_B = "artifact:beta"
PAYLOAD_A = '{"a":1}'

OUTBOX_PENDING = (
    "registered_model",
    "reg-1",
    1,
    "registered_model.upserted",
    PAYLOAD_A,
    "pending",
)


def _seed_outbox(db_path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        ensure_projection_outbox_schema(conn)
        # NOTE: deliberately do not pre-create the intent schema here. record_intent
        # ensures it on first use; get_intent must handle a missing schema gracefully.
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


def _intent_snapshot(db_path: Path) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT outbox_id, consumer_id, aggregate_type, aggregate_id, "
            "aggregate_revision, event_kind, payload_sha256, operation, artifact_ref, "
            "expected_sha256, desired_sha256 "
            "FROM projection_mutation_intents ORDER BY outbox_id, consumer_id"
        ).fetchall()
    finally:
        conn.close()


def _update_outbox(db_path: Path, outbox_id: int, **fields: object) -> None:
    if not fields:
        return
    cols = ", ".join(f"{column} = ?" for column in fields)
    params: list[object] = list(fields.values()) + [outbox_id]
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"UPDATE projection_outbox SET {cols} WHERE outbox_id = ?",
            params,
        )
        conn.commit()
    finally:
        conn.close()


def _delete_outbox_row(db_path: Path, outbox_id: int) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "DELETE FROM projection_outbox WHERE outbox_id = ?", (outbox_id,)
        )
        conn.commit()
    finally:
        conn.close()


class SQLiteProjectionMutationIntentJournalTests(unittest.TestCase):
    # ===== read-only / missing database =====

    def test_missing_db_read_only_get_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "missing.sqlite3"
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            with self.assertRaises(FileNotFoundError):
                journal.get_intent(outbox_id=1, consumer_id=CONSUMER)
            self.assertFalse(db_path.exists())

    def test_missing_db_record_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "missing.sqlite3"
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            event = ProjectionOutboxEvent(
                outbox_id=1,
                aggregate_type="registered_model",
                aggregate_id="reg-1",
                aggregate_revision=1,
                event_kind="registered_model.upserted",
                payload_json=PAYLOAD_A,
            )
            with self.assertRaises(FileNotFoundError):
                journal.record_intent(
                    event,
                    consumer_id=CONSUMER,
                    operation=OPERATION_WRITE,
                    artifact_ref=ARTIFACT_A,
                    expected_sha256=None,
                    desired_sha256=HASH_A,
                )

    # ===== schema handling =====

    def test_schema_additive_does_not_mutate_outbox(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])
            before_outbox = _outbox_snapshot(db_path)
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            event = _event_from_db(db_path, 1)
            journal.record_intent(
                event,
                consumer_id=CONSUMER,
                operation=OPERATION_WRITE,
                artifact_ref=ARTIFACT_A,
                expected_sha256=None,
                desired_sha256=HASH_A,
            )
            self.assertEqual(_outbox_snapshot(db_path), before_outbox)
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
            self.assertIn("projection_outbox", tables)
            self.assertIn("projection_mutation_intents", tables)

    def test_get_intent_handles_missing_schema(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            self.assertIsNone(journal.get_intent(outbox_id=1, consumer_id=CONSUMER))

    # ===== basic record / replay =====

    def test_insert_stores_correct_row(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            event = _event_from_db(db_path, 1)
            result = journal.record_intent(
                event,
                consumer_id=CONSUMER,
                operation=OPERATION_WRITE,
                artifact_ref=ARTIFACT_A,
                expected_sha256=None,
                desired_sha256=HASH_A,
            )
            self.assertIsInstance(result, ProjectionMutationIntent)
            self.assertEqual(result.outbox_id, 1)
            self.assertEqual(result.consumer_id, CONSUMER)
            self.assertEqual(result.operation, OPERATION_WRITE)
            self.assertEqual(result.artifact_ref, ARTIFACT_A)
            self.assertIsNone(result.expected_sha256)
            self.assertEqual(result.desired_sha256, HASH_A)
            self.assertEqual(result.payload_sha256, compute_payload_sha256(event))
            intents = _intent_snapshot(db_path)
            self.assertEqual(len(intents), 1)
            row = intents[0]
            self.assertEqual(row[0], 1)
            self.assertEqual(row[1], CONSUMER)
            self.assertEqual(row[7], OPERATION_WRITE)
            self.assertEqual(row[8], ARTIFACT_A)
            self.assertIsNone(row[9])
            self.assertEqual(row[10], HASH_A)

    def test_exact_replay_is_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            event = _event_from_db(db_path, 1)
            first = journal.record_intent(
                event,
                consumer_id=CONSUMER,
                operation=OPERATION_WRITE,
                artifact_ref=ARTIFACT_A,
                expected_sha256=None,
                desired_sha256=HASH_A,
            )
            second = journal.record_intent(
                event,
                consumer_id=CONSUMER,
                operation=OPERATION_WRITE,
                artifact_ref=ARTIFACT_A,
                expected_sha256=None,
                desired_sha256=HASH_A,
            )
            self.assertEqual(first, second)
            self.assertEqual(len(_intent_snapshot(db_path)), 1)

    # ===== outbox precedes intent replay =====

    def test_exact_replay_succeeds_when_outbox_state_moved_to_applied(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            event = _event_from_db(db_path, 1)
            first = journal.record_intent(
                event,
                consumer_id=CONSUMER,
                operation=OPERATION_WRITE,
                artifact_ref=ARTIFACT_A,
                expected_sha256=None,
                desired_sha256=HASH_A,
            )

            # Producer progresses the outbox without touching any event field.
            _update_outbox(db_path, 1, state="applied")
            replayed = _event_from_db(db_path, 1)
            # Outbox content is unchanged; only the state column differs.
            self.assertEqual(replayed.payload_json, event.payload_json)
            self.assertEqual(replayed.aggregate_id, event.aggregate_id)

            second = journal.record_intent(
                replayed,
                consumer_id=CONSUMER,
                operation=OPERATION_WRITE,
                artifact_ref=ARTIFACT_A,
                expected_sha256=None,
                desired_sha256=HASH_A,
            )
            self.assertEqual(first, second)
            self.assertEqual(len(_intent_snapshot(db_path)), 1)

    def test_record_intent_raises_mismatch_when_outbox_content_tampered(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            event = _event_from_db(db_path, 1)
            journal.record_intent(
                event,
                consumer_id=CONSUMER,
                operation=OPERATION_WRITE,
                artifact_ref=ARTIFACT_A,
                expected_sha256=None,
                desired_sha256=HASH_A,
            )

            _update_outbox(db_path, 1, aggregate_id="reg-2")

            # Replay the original event; its aggregate_id no longer matches the
            # persisted row, so the journal must refuse with the mismatch error.
            with self.assertRaises(ProjectionOutboxEventMismatchError):
                journal.record_intent(
                    event,
                    consumer_id=CONSUMER,
                    operation=OPERATION_WRITE,
                    artifact_ref=ARTIFACT_A,
                    expected_sha256=None,
                    desired_sha256=HASH_A,
                )

    def test_record_intent_raises_mismatch_when_outbox_row_removed(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            event = _event_from_db(db_path, 1)
            journal.record_intent(
                event,
                consumer_id=CONSUMER,
                operation=OPERATION_WRITE,
                artifact_ref=ARTIFACT_A,
                expected_sha256=None,
                desired_sha256=HASH_A,
            )

            _delete_outbox_row(db_path, 1)
            with self.assertRaises(ProjectionOutboxEventMismatchError):
                journal.record_intent(
                    event,
                    consumer_id=CONSUMER,
                    operation=OPERATION_WRITE,
                    artifact_ref=ARTIFACT_A,
                    expected_sha256=None,
                    desired_sha256=HASH_A,
                )

    # ===== conflict variants =====

    def test_changed_operation_raises_conflict(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            event = _event_from_db(db_path, 1)
            journal.record_intent(
                event,
                consumer_id=CONSUMER,
                operation=OPERATION_WRITE,
                artifact_ref=ARTIFACT_A,
                expected_sha256=None,
                desired_sha256=HASH_A,
            )
            with self.assertRaises(ProjectionReceiptConflictError):
                journal.record_intent(
                    event,
                    consumer_id=CONSUMER,
                    operation=OPERATION_DELETE,
                    artifact_ref=ARTIFACT_A,
                    expected_sha256=None,
                    desired_sha256=None,
                )

    def test_changed_desired_sha256_raises_conflict(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            event = _event_from_db(db_path, 1)
            journal.record_intent(
                event,
                consumer_id=CONSUMER,
                operation=OPERATION_WRITE,
                artifact_ref=ARTIFACT_A,
                expected_sha256=None,
                desired_sha256=HASH_A,
            )
            with self.assertRaises(ProjectionReceiptConflictError):
                journal.record_intent(
                    event,
                    consumer_id=CONSUMER,
                    operation=OPERATION_WRITE,
                    artifact_ref=ARTIFACT_A,
                    expected_sha256=None,
                    desired_sha256=HASH_B,
                )

    def test_changed_artifact_ref_raises_conflict(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            event = _event_from_db(db_path, 1)
            journal.record_intent(
                event,
                consumer_id=CONSUMER,
                operation=OPERATION_WRITE,
                artifact_ref=ARTIFACT_A,
                expected_sha256=None,
                desired_sha256=HASH_A,
            )
            with self.assertRaises(ProjectionReceiptConflictError):
                journal.record_intent(
                    event,
                    consumer_id=CONSUMER,
                    operation=OPERATION_WRITE,
                    artifact_ref=ARTIFACT_B,
                    expected_sha256=None,
                    desired_sha256=HASH_A,
                )

    def test_changed_expected_sha256_raises_conflict(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            event = _event_from_db(db_path, 1)
            journal.record_intent(
                event,
                consumer_id=CONSUMER,
                operation=OPERATION_WRITE,
                artifact_ref=ARTIFACT_A,
                expected_sha256=HASH_A,
                desired_sha256=HASH_A,
            )
            with self.assertRaises(ProjectionReceiptConflictError):
                journal.record_intent(
                    event,
                    consumer_id=CONSUMER,
                    operation=OPERATION_WRITE,
                    artifact_ref=ARTIFACT_A,
                    expected_sha256=HASH_B,
                    desired_sha256=HASH_A,
                )

    def test_changed_event_kind_raises_conflict(self) -> None:
        # Two-stage seed: outbox row with kind A; record intent; manually flip
        # the outbox row to kind B (same payload); replay with the new event.
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [("registered_model", "reg-1", 1, "kind-A", PAYLOAD_A, "pending")],
            )
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            event_a = _event_from_db(db_path, 1)
            journal.record_intent(
                event_a,
                consumer_id=CONSUMER,
                operation=OPERATION_WRITE,
                artifact_ref=ARTIFACT_A,
                expected_sha256=None,
                desired_sha256=HASH_A,
            )

            _update_outbox(db_path, 1, event_kind="kind-B")

            event_b = _event_from_db(db_path, 1)
            with self.assertRaises(ProjectionReceiptConflictError):
                journal.record_intent(
                    event_b,
                    consumer_id=CONSUMER,
                    operation=OPERATION_WRITE,
                    artifact_ref=ARTIFACT_A,
                    expected_sha256=None,
                    desired_sha256=HASH_A,
                )

    def test_changed_event_same_payload_conflicts(self) -> None:
        # Regression: an outbox row whose aggregate_id changed but whose
        # payload bytes (and therefore payload_sha256) are unchanged must not
        # be accepted as an idempotent replay.
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            event_a = _event_from_db(db_path, 1)
            journal.record_intent(
                event_a,
                consumer_id=CONSUMER,
                operation=OPERATION_WRITE,
                artifact_ref=ARTIFACT_A,
                expected_sha256=None,
                desired_sha256=HASH_A,
            )

            _update_outbox(db_path, 1, aggregate_id="reg-2")

            event_b = _event_from_db(db_path, 1)
            self.assertEqual(
                event_a.payload_json, event_b.payload_json
            )  # payload unchanged => same hash
            self.assertNotEqual(event_a.aggregate_id, event_b.aggregate_id)
            with self.assertRaises(ProjectionReceiptConflictError):
                journal.record_intent(
                    event_b,
                    consumer_id=CONSUMER,
                    operation=OPERATION_WRITE,
                    artifact_ref=ARTIFACT_A,
                    expected_sha256=None,
                    desired_sha256=HASH_A,
                )

    # ===== outbox state =====

    def test_outbox_not_pending_raises_state_error(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        1,
                        "registered_model.upserted",
                        PAYLOAD_A,
                        "applied",
                    )
                ],
            )
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            event = _event_from_db(db_path, 1)
            with self.assertRaises(ProjectionOutboxStateError):
                journal.record_intent(
                    event,
                    consumer_id=CONSUMER,
                    operation=OPERATION_WRITE,
                    artifact_ref=ARTIFACT_A,
                    expected_sha256=None,
                    desired_sha256=HASH_A,
                )

    def test_insert_leaves_outbox_pending(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            event = _event_from_db(db_path, 1)
            journal.record_intent(
                event,
                consumer_id=CONSUMER,
                operation=OPERATION_WRITE,
                artifact_ref=ARTIFACT_A,
                expected_sha256=None,
                desired_sha256=HASH_A,
            )
            self.assertEqual(_outbox_snapshot(db_path)[0][6], "pending")

    # ===== write/delete invariants =====

    def test_write_requires_desired_sha256(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            event = _event_from_db(db_path, 1)
            with self.assertRaises(ValueError):
                journal.record_intent(
                    event,
                    consumer_id=CONSUMER,
                    operation=OPERATION_WRITE,
                    artifact_ref=ARTIFACT_A,
                    expected_sha256=None,
                    desired_sha256=None,
                )

    def test_delete_requires_desired_sha256_none(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            event = _event_from_db(db_path, 1)
            with self.assertRaises(ValueError):
                journal.record_intent(
                    event,
                    consumer_id=CONSUMER,
                    operation=OPERATION_DELETE,
                    artifact_ref=ARTIFACT_A,
                    expected_sha256=None,
                    desired_sha256=HASH_A,
                )

    def test_delete_operation_records_correctly(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(
                db_path,
                [
                    (
                        "registered_model",
                        "reg-1",
                        1,
                        "registered_model.removed",
                        PAYLOAD_A,
                        "pending",
                    )
                ],
            )
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            event = _event_from_db(db_path, 1)
            result = journal.record_intent(
                event,
                consumer_id=CONSUMER,
                operation=OPERATION_DELETE,
                artifact_ref=ARTIFACT_A,
                expected_sha256=HASH_A,
                desired_sha256=None,
            )
            self.assertEqual(result.operation, OPERATION_DELETE)
            self.assertEqual(result.expected_sha256, HASH_A)
            self.assertIsNone(result.desired_sha256)
            self.assertEqual(result.artifact_ref, ARTIFACT_A)

    def test_expected_sha256_is_optional_write(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            event = _event_from_db(db_path, 1)
            result = journal.record_intent(
                event,
                consumer_id=CONSUMER,
                operation=OPERATION_WRITE,
                artifact_ref=ARTIFACT_A,
                expected_sha256=HASH_A,
                desired_sha256=HASH_B,
            )
            self.assertEqual(result.expected_sha256, HASH_A)
            self.assertEqual(result.desired_sha256, HASH_B)

    # ===== payload never stored =====

    def test_payload_not_stored(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            event = _event_from_db(db_path, 1)
            journal.record_intent(
                event,
                consumer_id=CONSUMER,
                operation=OPERATION_WRITE,
                artifact_ref=ARTIFACT_A,
                expected_sha256=None,
                desired_sha256=HASH_A,
            )
            conn = sqlite3.connect(db_path)
            try:
                for row in conn.execute("SELECT * FROM projection_mutation_intents"):
                    self.assertNotIn(PAYLOAD_A, row)
                    self.assertNotIn("payload_json", str(row))
            finally:
                conn.close()

    # ===== read-only get =====

    def test_get_intent_returns_recorded_intent(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            event = _event_from_db(db_path, 1)
            recorded = journal.record_intent(
                event,
                consumer_id=CONSUMER,
                operation=OPERATION_WRITE,
                artifact_ref=ARTIFACT_A,
                expected_sha256=None,
                desired_sha256=HASH_A,
            )
            retrieved = journal.get_intent(outbox_id=1, consumer_id=CONSUMER)
            self.assertEqual(retrieved, recorded)

    def test_get_intent_returns_none_when_not_recorded(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            self.assertIsNone(
                journal.get_intent(outbox_id=1, consumer_id="unknown-consumer")
            )

    def test_get_intent_is_read_only(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])
            before_bytes = db_path.read_bytes()
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            result = journal.get_intent(outbox_id=1, consumer_id=CONSUMER)
            self.assertIsNone(result)
            self.assertEqual(db_path.read_bytes(), before_bytes)

    # ===== input validation: record_intent =====

    def test_record_intent_rejects_bool_outbox_id_via_event(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            event = ProjectionOutboxEvent(
                outbox_id=True,  # type: ignore[arg-type]
                aggregate_type="registered_model",
                aggregate_id="reg-1",
                aggregate_revision=1,
                event_kind="registered_model.upserted",
                payload_json=PAYLOAD_A,
            )
            with self.assertRaises(TypeError):
                journal.record_intent(
                    event,
                    consumer_id=CONSUMER,
                    operation=OPERATION_WRITE,
                    artifact_ref=ARTIFACT_A,
                    expected_sha256=None,
                    desired_sha256=HASH_A,
                )

    def test_record_intent_rejects_zero_outbox_id_via_event(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            event = ProjectionOutboxEvent(
                outbox_id=0,
                aggregate_type="registered_model",
                aggregate_id="reg-1",
                aggregate_revision=1,
                event_kind="registered_model.upserted",
                payload_json=PAYLOAD_A,
            )
            with self.assertRaises(ValueError):
                journal.record_intent(
                    event,
                    consumer_id=CONSUMER,
                    operation=OPERATION_WRITE,
                    artifact_ref=ARTIFACT_A,
                    expected_sha256=None,
                    desired_sha256=HASH_A,
                )

    def test_record_intent_rejects_empty_consumer_id(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            event = _event_from_db(db_path, 1)
            with self.assertRaises(ValueError):
                journal.record_intent(
                    event,
                    consumer_id="",
                    operation=OPERATION_WRITE,
                    artifact_ref=ARTIFACT_A,
                    expected_sha256=None,
                    desired_sha256=HASH_A,
                )

    def test_record_intent_rejects_artifact_ref_too_long(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            event = _event_from_db(db_path, 1)
            long_ref = "x" * 300
            with self.assertRaises(ValueError):
                journal.record_intent(
                    event,
                    consumer_id=CONSUMER,
                    operation=OPERATION_WRITE,
                    artifact_ref=long_ref,
                    expected_sha256=None,
                    desired_sha256=HASH_A,
                )

    # ===== input validation: get_intent =====

    def test_get_intent_rejects_bool_outbox_id(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            with self.assertRaises(TypeError):
                journal.get_intent(outbox_id=True, consumer_id=CONSUMER)  # type: ignore[arg-type]

    def test_get_intent_rejects_zero_outbox_id(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            with self.assertRaises(ValueError):
                journal.get_intent(outbox_id=0, consumer_id=CONSUMER)

    def test_get_intent_rejects_empty_consumer_id(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])
            journal = SQLiteProjectionMutationIntentJournal(db_path)
            with self.assertRaises(ValueError):
                journal.get_intent(outbox_id=1, consumer_id="")

    # ===== intent-specific operation validation =====

    def test_validation_rejects_unknown_operation(self) -> None:
        with self.assertRaises(ValueError):
            validate_operation("unknown")

    def test_validation_strict_int_rejects_bool(self) -> None:
        with self.assertRaises(TypeError):
            validate_strict_int("outbox_id", True)

    # ===== concurrency =====

    def test_concurrent_identical_contenders_yield_one_row(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])

            results: list[object] = [None, None]
            errors: list[object] = [None, None]
            barrier = threading.Barrier(2)

            def worker(idx: int) -> None:
                journal = SQLiteProjectionMutationIntentJournal(db_path)
                event = _event_from_db(db_path, 1)
                barrier.wait()
                try:
                    results[idx] = journal.record_intent(
                        event,
                        consumer_id=CONSUMER,
                        operation=OPERATION_WRITE,
                        artifact_ref=ARTIFACT_A,
                        expected_sha256=None,
                        desired_sha256=HASH_A,
                    )
                except BaseException as e:  # pragma: no cover - exercised
                    errors[idx] = e

            t1 = threading.Thread(target=worker, args=(0,))
            t2 = threading.Thread(target=worker, args=(1,))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            self.assertIsNone(errors[0])
            self.assertIsNone(errors[1])
            self.assertIsNotNone(results[0])
            self.assertIsNotNone(results[1])
            self.assertEqual(results[0], results[1])
            intents = _intent_snapshot(db_path)
            self.assertEqual(len(intents), 1)
            self.assertEqual(intents[0][10], HASH_A)

    def test_concurrent_differing_contenders_yield_one_success_and_conflict(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outbox.sqlite3"
            _seed_outbox(db_path, [OUTBOX_PENDING])

            results: list[object] = [None, None]
            errors: list[object] = [None, None]
            barrier = threading.Barrier(2)

            def worker(idx: int, desired: str) -> None:
                journal = SQLiteProjectionMutationIntentJournal(db_path)
                event = _event_from_db(db_path, 1)
                barrier.wait()
                try:
                    results[idx] = journal.record_intent(
                        event,
                        consumer_id=CONSUMER,
                        operation=OPERATION_WRITE,
                        artifact_ref=ARTIFACT_A,
                        expected_sha256=None,
                        desired_sha256=desired,
                    )
                except BaseException as e:
                    errors[idx] = e

            t1 = threading.Thread(target=worker, args=(0, HASH_A))
            t2 = threading.Thread(target=worker, args=(1, HASH_B))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            successes = sum(1 for r in results if r is not None)
            failures = sum(1 for e in errors if e is not None)
            self.assertEqual(successes, 1)
            self.assertEqual(failures, 1)
            self.assertIsInstance(
                errors[0] if errors[0] is not None else errors[1],
                ProjectionReceiptConflictError,
            )

            intents = _intent_snapshot(db_path)
            self.assertEqual(len(intents), 1)
            winning_desired = HASH_A if results[0] is not None else HASH_B
            self.assertEqual(intents[0][10], winning_desired)


if __name__ == "__main__":
    unittest.main()

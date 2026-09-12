from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from model_deck.adapters.storage.sqlite_outbox import ensure_projection_outbox_schema
from model_deck.adapters.storage.sqlite_model_repository import SQLiteModelRepository
from model_deck.adapters.storage.sqlite_projection_outbox import SQLiteProjectionOutboxReader
from model_deck.engine.model_library.ports import RegisterModelCommand
from model_deck.engine.projections.ports import ProjectionOutboxEvent


def _seed_db(db_path: Path, rows: list[tuple[str, str, int, str, str, str]]) -> None:
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


def _snapshot(db_path: Path) -> tuple[bytes, list[tuple]]:
    raw = db_path.read_bytes()
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT outbox_id, aggregate_type, aggregate_id, aggregate_revision, event_kind, payload_json, state "
            "FROM projection_outbox ORDER BY outbox_id ASC"
        ).fetchall()
    finally:
        conn.close()
    return raw, [tuple(row) for row in rows]


class SQLiteProjectionOutboxReaderTests(unittest.TestCase):
    def test_empty_db_returns_empty_tuple(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_db(db_path, [])
            reader = SQLiteProjectionOutboxReader(db_path)
            self.assertEqual(reader.list_pending(), ())

    def test_pending_order_and_bounded_limit(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_db(db_path, [
                ("registered_model", "reg-1", 1, "registered_model.upserted", '{"a":1}', "pending"),
                ("registered_model", "reg-2", 1, "registered_model.upserted", '{"a":2}', "pending"),
                ("connection", "conn-1", 1, "connection.saved", '{"a":3}', "pending"),
            ])
            reader = SQLiteProjectionOutboxReader(db_path)
            events = reader.list_pending()
            self.assertEqual([event.outbox_id for event in events], [1, 2, 3])
            self.assertTrue(all(isinstance(event, ProjectionOutboxEvent) for event in events))
            bounded = reader.list_pending(limit=2)
            self.assertEqual([event.outbox_id for event in bounded], [1, 2])

    def test_excludes_applied_and_conflict(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_db(db_path, [
                ("registered_model", "reg-keep", 1, "registered_model.upserted", '{"a":1}', "pending"),
                ("registered_model", "reg-done", 1, "registered_model.upserted", '{"a":2}', "applied"),
                ("registered_model", "reg-clash", 1, "registered_model.upserted", '{"a":3}', "conflict"),
            ])
            reader = SQLiteProjectionOutboxReader(db_path)
            events = reader.list_pending()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].aggregate_id, "reg-keep")

    def test_payload_json_stays_immutable_string(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_db(db_path, [
                ("registered_model", "reg-1", 1, "registered_model.upserted", '{"a":1}', "pending"),
            ])
            reader = SQLiteProjectionOutboxReader(db_path)
            (event,) = reader.list_pending()
            self.assertIsInstance(event.payload_json, str)
            self.assertEqual(event.payload_json, '{"a":1}')
            with self.assertRaises(AttributeError):
                event.outbox_id = 99  # type: ignore[misc]

    def test_special_char_filename_reads_correct_db(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "we?ird#name.sqlite3"
            _seed_db(db_path, [
                ("registered_model", "reg-1", 1, "registered_model.upserted", '{"a":1}', "pending"),
            ])
            reader = SQLiteProjectionOutboxReader(db_path)
            events = reader.list_pending()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].aggregate_id, "reg-1")
            self.assertEqual(events[0].payload_json, '{"a":1}')

    def test_invalid_limits_rejected(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_db(db_path, [])
            reader = SQLiteProjectionOutboxReader(db_path)
            for bad in (0, -1, 1001, True, False, "10", 1.5, None):
                with self.subTest(limit=bad):
                    with self.assertRaises((TypeError, ValueError)):
                        reader.list_pending(limit=bad)  # type: ignore[arg-type]

    def test_missing_db_rejected_without_creating_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "absent.sqlite3"
            reader = SQLiteProjectionOutboxReader(db_path)
            with self.assertRaises(FileNotFoundError):
                reader.list_pending()
            self.assertFalse(db_path.exists())

    def test_failed_execute_closes_connection(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_db(db_path, [
                ("registered_model", "reg-1", 1, "registered_model.upserted", '{"a":1}', "pending"),
            ])
            reader = SQLiteProjectionOutboxReader(db_path)
            conn = MagicMock()
            conn.execute.side_effect = sqlite3.OperationalError("boom")
            with patch("sqlite3.connect", return_value=conn) as mock_connect:
                with self.assertRaises(sqlite3.OperationalError):
                    reader.list_pending()
                mock_connect.assert_called_once()
            conn.close.assert_called_once_with()

    def test_connect_failure_propagates(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "outbox.sqlite3"
            _seed_db(db_path, [
                ("registered_model", "reg-1", 1, "registered_model.upserted", '{"a":1}', "pending"),
            ])
            reader = SQLiteProjectionOutboxReader(db_path)
            with patch("sqlite3.connect", side_effect=sqlite3.OperationalError("boom")) as mock_connect:
                with self.assertRaises(sqlite3.OperationalError):
                    reader.list_pending()
                mock_connect.assert_called_once()

    def test_producer_integration_leaves_rows_and_bytes_unchanged(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "models.sqlite3"
            repo = SQLiteModelRepository(db_path)
            repo.register(
                RegisterModelCommand(
                    connection_id="conn-a",
                    provider_model_id="openrouter/alpha",
                    display_name="Alpha",
                    expected_revision=0,
                    idempotency_key="reg-a",
                )
            )
            repo.register(
                RegisterModelCommand(
                    connection_id="conn-b",
                    provider_model_id="openrouter/beta",
                    display_name="Beta",
                    expected_revision=0,
                    idempotency_key="reg-b",
                )
            )
            before_raw, before_rows = _snapshot(db_path)
            reader = SQLiteProjectionOutboxReader(db_path)
            events = reader.list_pending()
            self.assertEqual(len(events), 2)
            self.assertEqual([event.outbox_id for event in events], sorted(event.outbox_id for event in events))
            self.assertTrue(all(isinstance(event.payload_json, str) for event in events))
            after_raw, after_rows = _snapshot(db_path)
            self.assertEqual(before_rows, after_rows)
            self.assertEqual(before_raw, after_raw)


if __name__ == "__main__":
    unittest.main()

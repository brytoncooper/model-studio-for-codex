from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from uuid import uuid4

from model_deck.adapters.storage.sqlite_connection_repository import SQLiteConnectionRepository
from model_deck.adapters.storage.sqlite_model_repository import SQLiteModelRepository
from model_deck.adapters.storage.sqlite_outbox import enqueue_connection_saved
from model_deck.adapters.storage.sqlite_projection_dependency_recovery import recover_connection_dependencies, DependencyRecoveryError
from model_deck.engine.connections.ports import SaveConnectionCommand
from model_deck.engine.model_library.ports import RegisterModelCommand, RemoveModelCommand


class ProjectionDependencyRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state.sqlite3"
        self.connections = SQLiteConnectionRepository(self.path)
        self.models = SQLiteModelRepository(self.path)
        self.identifier = str(uuid4())
        self.save(self.identifier, 0)
        self.model = self.models.register(RegisterModelCommand(self.identifier, "model", "Model", 0, str(uuid4())))
        self.save(self.identifier, 1)
        self.save(self.identifier, 2)
        with sqlite3.connect(self.path) as connection:
            self.historical = tuple(row[0] for row in connection.execute("SELECT outbox_id FROM projection_outbox WHERE event_kind = 'connection.saved' AND aggregate_revision <= 2 ORDER BY outbox_id"))
            connection.execute("DELETE FROM projection_dependency_expansions WHERE connection_revision <= 2")

    def save(self, identifier, expected):
        return self.connections.save(SaveConnectionCommand(identifier, "org.example.provider", expected, str(uuid4()), "ref:endpoint" + str(expected)))

    def snapshot(self):
        with sqlite3.connect(self.path) as connection:
            return list(connection.iterdump())

    def model_revision(self):
        return self.models.list_registered()[0].revision

    def test_latest_committed_connection_supersedes_history_once_per_group(self):
        before = self.connections.list_connections()
        report = recover_connection_dependencies(self.path)
        self.assertEqual(report.expanded_outbox_ids, self.historical)
        self.assertEqual(report.conflicts, ())
        self.assertEqual(self.model_revision(), 4)
        self.assertEqual(self.connections.list_connections(), before)
        with sqlite3.connect(self.path) as connection:
            receipts = connection.execute("SELECT outbox_id, expanded_models_json FROM projection_dependency_expansions WHERE connection_revision <= 2 ORDER BY outbox_id").fetchall()
        expected = [{"registration_id": self.model.registration_id, "revision": 4}]
        self.assertEqual([json.loads(row[1]) for row in receipts], [expected, expected])
        stable = self.snapshot()
        self.assertEqual(recover_connection_dependencies(self.path).expanded_outbox_ids, ())
        self.assertEqual(self.snapshot(), stable)

    def test_tombstones_never_resurrect_even_for_older_connection_events(self):
        self.models.remove(RemoveModelCommand(self.model.registration_id, 3, str(uuid4())))
        report = recover_connection_dependencies(self.path)
        self.assertEqual(report.expanded_outbox_ids, self.historical)
        self.assertEqual(self.models.list_registered(), [])
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT revision, active FROM registered_models").fetchone(), (4, 0))
            for identifier in self.historical:
                self.assertEqual(json.loads(connection.execute("SELECT expanded_models_json FROM projection_dependency_expansions WHERE outbox_id=?", (identifier,)).fetchone()[0]), [])
            self.assertEqual(connection.execute("SELECT event_kind FROM projection_outbox WHERE aggregate_type='registered_model' ORDER BY outbox_id DESC LIMIT 1").fetchone()[0], "registered_model.removed")

    def test_missing_future_malformed_and_unknown_rows_remain_pending(self):
        with sqlite3.connect(self.path) as connection:
            missing = enqueue_connection_saved(connection, connection_id=str(uuid4()), revision=1,
                provider_id="org.example.provider", endpoint_config_ref=None, credential_ref=None)
            future = enqueue_connection_saved(connection, connection_id=self.identifier, revision=99,
                provider_id="org.example.provider", endpoint_config_ref=None, credential_ref=None)
            malformed = enqueue_connection_saved(connection, connection_id=self.identifier, revision=100,
                provider_id="org.example.provider", endpoint_config_ref=None, credential_ref=None)
            connection.execute("UPDATE projection_outbox SET payload_json='SECRET_BAD_JSON' WHERE outbox_id=?", (malformed,))
            unknown = connection.execute("INSERT INTO projection_outbox (aggregate_type,aggregate_id,aggregate_revision,event_kind,payload_json) VALUES ('other','opaque',1,'other.saved','{}')").lastrowid
        report = recover_connection_dependencies(self.path)
        self.assertEqual(report.expanded_outbox_ids, self.historical)
        self.assertEqual({c.outbox_id: c.reason for c in report.conflicts},
            {missing: "connection_missing", future: "connection_revision_future", malformed: "event_invalid"})
        self.assertNotIn("SECRET_BAD_JSON", repr(report))
        with sqlite3.connect(self.path) as connection:
            for identifier in (missing, future, malformed, unknown):
                self.assertEqual(connection.execute("SELECT state FROM projection_outbox WHERE outbox_id=?", (identifier,)).fetchone()[0], "pending")
                self.assertIsNone(connection.execute("SELECT 1 FROM projection_dependency_expansions WHERE outbox_id=?", (identifier,)).fetchone())

    def test_mismatched_existing_proof_is_reported_without_replacement(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE projection_dependency_expansions SET connection_id='other'")
            identifier = connection.execute("SELECT outbox_id FROM projection_dependency_expansions").fetchone()[0]
        report = recover_connection_dependencies(self.path)
        self.assertIn((identifier, "proof_conflict"), [(item.outbox_id, item.reason) for item in report.conflicts])
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT connection_id FROM projection_dependency_expansions WHERE outbox_id=?", (identifier,)).fetchone()[0], "other")

    def test_receipt_failure_rolls_back_all_groups_and_new_upserts(self):
        other = str(uuid4())
        self.save(other, 0)
        self.models.register(RegisterModelCommand(other, "other", "Other", 0, str(uuid4())))
        with sqlite3.connect(self.path) as connection:
            connection.execute("DELETE FROM projection_dependency_expansions WHERE connection_id=?", (other,))
            connection.execute("CREATE TRIGGER fail_recovery BEFORE INSERT ON projection_dependency_expansions WHEN NEW.connection_id='" + other + "' BEGIN SELECT RAISE(ABORT,'fixture'); END")
        before = self.snapshot()
        with self.assertRaises(DependencyRecoveryError):
            recover_connection_dependencies(self.path)
        self.assertEqual(self.snapshot(), before)

    def test_two_concurrent_recoverers_expand_each_source_once(self):
        barrier = threading.Barrier(2, timeout=3)
        def recover():
            barrier.wait()
            return recover_connection_dependencies(self.path)
        with ThreadPoolExecutor(2) as workers:
            futures = [workers.submit(recover) for _ in range(2)]
            reports = [future.result(timeout=5) for future in futures]
        self.assertEqual(sorted(len(result.expanded_outbox_ids) for result in reports), [0, 2])
        self.assertEqual(self.model_revision(), 4)

    def test_limit_is_bounded_and_each_selected_batch_is_independent(self):
        first = recover_connection_dependencies(self.path, limit=1)
        self.assertEqual(first.expanded_outbox_ids, self.historical[:1])
        second = recover_connection_dependencies(self.path, limit=1)
        self.assertEqual(second.expanded_outbox_ids, self.historical[1:])
        self.assertEqual(self.model_revision(), 5)
        for invalid in (0, 1001, True):
            with self.assertRaises((ValueError, TypeError)):
                recover_connection_dependencies(self.path, limit=invalid)

    def test_legacy_database_without_receipt_table_is_recovered_explicitly(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TABLE projection_dependency_expansions")
        result = recover_connection_dependencies(self.path)
        self.assertEqual(len(result.expanded_outbox_ids), 3)
        self.assertEqual(self.model_revision(), 4)

    def test_missing_database_is_not_created(self):
        path = self.path.parent / "missing.sqlite3"
        with self.assertRaises(DependencyRecoveryError):
            recover_connection_dependencies(path)
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()

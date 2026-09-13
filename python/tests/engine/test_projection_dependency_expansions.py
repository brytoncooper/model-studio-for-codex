import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from model_deck.adapters.storage.sqlite_connection_repository import SQLiteConnectionRepository
from model_deck.adapters.storage.sqlite_model_repository import SQLiteModelRepository
from model_deck.adapters.storage.sqlite_projection_outbox import SQLiteProjectionOutboxReader
from model_deck.adapters.storage.sqlite_outbox import enqueue_connection_saved
from model_deck.adapters.storage.sqlite_projection_dependency_expansions import record_connection_expansion
from model_deck.engine.connections.ports import SaveConnectionCommand
from model_deck.engine.model_library.ports import RegisterModelCommand, RemoveModelCommand


class ProjectionDependencyExpansionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state.sqlite3"
        self.connections = SQLiteConnectionRepository(self.path)
        self.models = SQLiteModelRepository(self.path)
        self.reader = SQLiteProjectionOutboxReader(self.path)
        self.identifier = str(uuid4())
        self.connections.save(self.command(0))

    def command(self, expected):
        return SaveConnectionCommand(self.identifier, "org.example.provider", expected, str(uuid4()), "ref:endpoint")

    def register(self, name, owner=None):
        return self.models.register(RegisterModelCommand(owner or self.identifier, name, name, 0, str(uuid4())))

    def snapshot(self):
        with sqlite3.connect(self.path) as connection:
            return list(connection.iterdump())

    def test_many_proven_connection_saves_do_not_starve_model_event(self):
        for revision in range(1, 106):
            self.connections.save(self.command(revision))
        model = self.register("last")
        pending = self.reader.list_pending(limit=1)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].aggregate_id, model.registration_id)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM projection_outbox WHERE event_kind = 'connection.saved' AND state = 'pending'").fetchone()[0], 106)
            self.assertEqual(connection.execute("SELECT count(*) FROM projection_dependency_expansions").fetchone()[0], 106)

    def test_receipt_contains_exact_active_expansion_and_canonical_source(self):
        first, second = self.register("one"), self.register("two")
        self.register("foreign", str(uuid4()))
        removed = self.register("removed")
        self.models.remove(RemoveModelCommand(removed.registration_id, 1, str(uuid4())))
        self.connections.save(self.command(1))
        with sqlite3.connect(self.path) as connection:
            row = connection.execute("SELECT e.connection_id, e.connection_revision, e.payload_json, e.expanded_models_json, o.payload_json FROM projection_dependency_expansions e JOIN projection_outbox o USING(outbox_id) ORDER BY e.outbox_id DESC LIMIT 1").fetchone()
        self.assertEqual(row[:2], (self.identifier, 2))
        self.assertEqual(row[2], row[4])
        self.assertEqual(json.loads(row[3]), sorted([
            {"registration_id": first.registration_id, "revision": 2},
            {"registration_id": second.registration_id, "revision": 2}], key=lambda item: item["registration_id"]))

    def test_expansion_receipt_failure_rolls_back_every_mutation(self):
        self.register("one")
        self.register("two")
        with sqlite3.connect(self.path) as connection:
            connection.execute("CREATE TRIGGER fail_expansion BEFORE INSERT ON projection_dependency_expansions BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        before = self.snapshot()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connections.save(self.command(1))
        self.assertEqual(self.snapshot(), before)

    def test_unexpanded_connection_and_unknown_events_remain_pending(self):
        with sqlite3.connect(self.path) as connection:
            unexpanded = enqueue_connection_saved(connection, connection_id=str(uuid4()), revision=1,
                provider_id="org.example.provider", endpoint_config_ref=None, credential_ref=None)
            cursor = connection.execute("INSERT INTO projection_outbox (aggregate_type,aggregate_id,aggregate_revision,event_kind,payload_json) VALUES ('other','opaque',1,'other.changed','{}')")
            unknown = cursor.lastrowid
        self.assertEqual([event.outbox_id for event in self.reader.list_pending()], [unexpanded, unknown])

    def test_absent_receipt_table_preserves_original_query_without_ddl(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TABLE projection_dependency_expansions")
        before = self.path.read_bytes()
        pending = self.reader.list_pending()
        self.assertEqual([event.event_kind for event in pending], ["connection.saved"])
        self.assertEqual(self.path.read_bytes(), before)
        with sqlite3.connect(self.path) as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'projection_dependency_expansions'").fetchone())

    def test_mismatched_proof_does_not_hide_source_event(self):
        for column, replacement in (("connection_id", "other"), ("connection_revision", 9), ("payload_json", "{}")):
            with self.subTest(column=column), sqlite3.connect(self.path) as connection:
                original = connection.execute("SELECT " + column + " FROM projection_dependency_expansions").fetchone()[0]
                connection.execute("UPDATE projection_dependency_expansions SET " + column + " = ?", (replacement,))
                connection.commit()
                self.assertEqual([e.event_kind for e in self.reader.list_pending()], ["connection.saved"])
                connection.execute("UPDATE projection_dependency_expansions SET " + column + " = ?", (original,))
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE projection_outbox SET event_kind = 'other.changed'")
        self.assertEqual([e.event_kind for e in self.reader.list_pending()], ["other.changed"])

    def test_replay_retains_one_expansion_and_exact_receipt(self):
        self.register("one")
        command = self.command(1)
        result = self.connections.save(command)
        before = self.snapshot()
        self.assertEqual(SQLiteConnectionRepository(self.path).save(command), result)
        self.assertEqual(self.snapshot(), before)

    def test_helper_rejects_noncanonical_source_and_missing_model_upsert(self):
        model = self.register("one")
        connection = sqlite3.connect(self.path)
        try:
            with self.assertRaises(ValueError):
                record_connection_expansion(connection, outbox_id=1, expanded_models=())
            connection.execute("BEGIN IMMEDIATE")
            source = enqueue_connection_saved(connection, connection_id=self.identifier, revision=2,
                provider_id="org.example.provider", endpoint_config_ref=None, credential_ref=None)
            with self.assertRaisesRegex(ValueError, "expanded model event missing"):
                record_connection_expansion(connection, outbox_id=source, expanded_models=(model,))
            row = connection.execute("SELECT payload_json FROM projection_outbox WHERE outbox_id = ?", (source,)).fetchone()[0]
            connection.execute("UPDATE projection_outbox SET payload_json = ? WHERE outbox_id = ?", (json.dumps(json.loads(row)), source))
            with self.assertRaisesRegex(ValueError, "source binding rejected"):
                record_connection_expansion(connection, outbox_id=source, expanded_models=())
            connection.rollback()
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from model_deck.adapters.storage.sqlite_connection_repository import SQLiteConnectionRepository
from model_deck.adapters.storage.sqlite_model_repository import SQLiteModelRepository
from model_deck.adapters.storage.sqlite_model_projection_invalidation import invalidate_connection_models
from model_deck.adapters.storage.sqlite_model_schema import ensure_model_schema
from model_deck.adapters.storage import sqlite_model_projection_invalidation as invalidation
from model_deck.engine.connections.ports import ConnectionIdempotencyConflictError, ConnectionRevisionConflictError, SaveConnectionCommand
from model_deck.engine.model_library.ports import (
    ModelRevisionConflictError, RegisterModelCommand, RemoveModelCommand, RenameModelCommand,
)


class ModelProjectionInvalidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state.sqlite3"
        self.models = SQLiteModelRepository(self.path)
        self.connections = SQLiteConnectionRepository(self.path)
        self.connection = str(uuid4())
        self.other_connection = str(uuid4())
        self.connections.save(self.command(expected=0))

    def command(self, expected=1):
        return SaveConnectionCommand(self.connection, "org.example.provider", expected, str(uuid4()), "ref:updated.endpoint")

    def register(self, name, connection=None):
        return self.models.register(RegisterModelCommand(connection or self.connection, name, name, 0, str(uuid4())))

    def rows(self):
        with sqlite3.connect(self.path) as connection:
            return connection.execute("SELECT registration_id, revision, active FROM registered_models ORDER BY registration_id").fetchall()

    def outbox(self, registration=None):
        with sqlite3.connect(self.path) as connection:
            if registration is None:
                return connection.execute("SELECT * FROM projection_outbox ORDER BY outbox_id").fetchall()
            return connection.execute("SELECT aggregate_revision, event_kind, payload_json FROM projection_outbox WHERE aggregate_id = ? ORDER BY outbox_id", (registration,)).fetchall()

    def snapshot(self):
        with sqlite3.connect(self.path) as connection:
            return list(connection.iterdump())

    def test_save_advances_only_active_models_on_exact_connection(self):
        one, two = self.register("one"), self.register("two")
        foreign = self.register("foreign", self.other_connection)
        removed = self.register("removed")
        self.models.remove(RemoveModelCommand(removed.registration_id, 1, str(uuid4())))
        before_removed = self.outbox(removed.registration_id)
        result = self.connections.save(self.command())
        self.assertEqual(result.revision, 2)
        rows = {row[0]: row[1:] for row in self.rows()}
        for record in (one, two):
            self.assertEqual(rows[record.registration_id], (2, 1))
            revision, kind, raw = self.outbox(record.registration_id)[-1]
            self.assertEqual((revision, kind), (2, "registered_model.upserted"))
            self.assertEqual(json.loads(raw), {"registration_id": record.registration_id, "revision": 2,
                "connection_id": self.connection, "provider_model_id": record.provider_model_id, "display_name": record.display_name})
        self.assertEqual(rows[foreign.registration_id], (1, 1))
        self.assertEqual(rows[removed.registration_id], (2, 0))
        self.assertEqual(self.outbox(removed.registration_id), before_removed)

    def test_exact_replay_and_conflicting_replay_do_not_invalidate_again(self):
        self.register("one")
        command = self.command()
        result = self.connections.save(command)
        before = self.snapshot()
        reopened = SQLiteConnectionRepository(self.path)
        self.assertEqual(reopened.save(command), result)
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(ConnectionIdempotencyConflictError):
            reopened.save(replace(command, endpoint_config_ref="ref:other"))
        self.assertEqual(self.snapshot(), before)

    def test_connection_creation_invalidates_preexisting_active_registration(self):
        registration = self.register("orphan", self.other_connection)
        self.connections.save(replace(self.command(0), connection_id=self.other_connection))
        self.assertEqual(self.outbox(registration.registration_id)[-1][0], 2)

    def test_enqueuing_failure_rolls_back_connection_models_outbox_and_receipt(self):
        self.register("one")
        self.register("two")
        command = self.command()
        before = self.snapshot()
        original = invalidation.enqueue_registered_model_upserted
        calls = 0
        def fail(*args, **kwargs):
            nonlocal calls
            original(*args, **kwargs)
            calls += 1
            if calls == 2:
                raise OSError("fixture enqueue failure")
        with patch.object(invalidation, "enqueue_registered_model_upserted", side_effect=fail):
            with self.assertRaises(OSError):
                self.connections.save(command)
        self.assertEqual(calls, 2)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.connections.save(command).revision, 2)

    def test_receipt_insert_failure_rolls_back_complete_mutation_unit(self):
        self.register("one")
        with sqlite3.connect(self.path) as connection:
            connection.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON connection_idempotency BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        before = self.snapshot()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connections.save(self.command())
        self.assertEqual(self.snapshot(), before)

    def test_remove_before_save_does_not_resurrect_and_save_before_remove_is_newer(self):
        prior = self.register("prior")
        later = self.register("later")
        self.models.remove(RemoveModelCommand(prior.registration_id, 1, str(uuid4())))
        self.connections.save(self.command())
        with self.assertRaises(ModelRevisionConflictError):
            self.models.remove(RemoveModelCommand(later.registration_id, 1, str(uuid4())))
        self.models.remove(RemoveModelCommand(later.registration_id, 2, str(uuid4())))
        self.assertEqual({row[0]: row[1:] for row in self.rows()}, {prior.registration_id: (2, 0), later.registration_id: (3, 0)})
        self.assertEqual([row[:2] for row in self.outbox(later.registration_id)],
                         [(1, "registered_model.upserted"), (2, "registered_model.upserted"), (3, "registered_model.removed")])
        self.connections.save(self.command(2))
        self.assertEqual(self.outbox(later.registration_id)[-1][0], 3)

    def test_rename_order_and_historical_model_receipt(self):
        record = self.register("one")
        rename = RenameModelCommand(record.registration_id, "renamed", 1, str(uuid4()))
        renamed = self.models.rename(rename)
        self.connections.save(self.command())
        self.assertEqual(self.models.list_registered()[0].revision, 3)
        self.assertEqual(json.loads(self.outbox(record.registration_id)[-1][2])["display_name"], "renamed")
        before = self.snapshot()
        self.assertEqual(self.models.rename(rename), renamed)
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(ModelRevisionConflictError):
            self.models.rename(RenameModelCommand(record.registration_id, "stale", 2, str(uuid4())))

    def test_concurrent_identical_save_invalidates_exactly_once(self):
        model = self.register("one")
        command = self.command()
        barrier = threading.Barrier(2, timeout=3)
        def save():
            barrier.wait()
            return SQLiteConnectionRepository(self.path).save(command)
        with ThreadPoolExecutor(2) as workers:
            futures = [workers.submit(save) for _ in range(2)]
            outcomes = [future.result(timeout=5) for future in futures]
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(self.outbox(model.registration_id)[-1][0], 2)
        self.assertEqual(len(self.outbox(model.registration_id)), 2)

    def test_connection_save_serializes_against_rename_and_remove(self):
        for operation in ("rename", "remove"):
            with self.subTest(operation=operation):
                model = self.register(operation)
                expected_connection = self.connections.list_connections()[0].revision
                barrier = threading.Barrier(2, timeout=3)
                def save():
                    barrier.wait()
                    return SQLiteConnectionRepository(self.path).save(self.command(expected_connection))
                def mutate():
                    barrier.wait()
                    repository = SQLiteModelRepository(self.path)
                    try:
                        if operation == "rename":
                            return repository.rename(RenameModelCommand(model.registration_id, "changed", 1, str(uuid4())))
                        return repository.remove(RemoveModelCommand(model.registration_id, 1, str(uuid4())))
                    except ModelRevisionConflictError as error:
                        return error
                with ThreadPoolExecutor(2) as workers:
                    saving, changing = workers.submit(save), workers.submit(mutate)
                    saving.result(timeout=5)
                    changed = changing.result(timeout=5)
                row = next(row for row in self.rows() if row[0] == model.registration_id)
                if isinstance(changed, ModelRevisionConflictError):
                    self.assertEqual(row[1:], (2, 1))
                elif operation == "remove":
                    self.assertEqual(row[1:], (2, 0))
                    self.assertEqual(self.outbox(model.registration_id)[-1][1], "registered_model.removed")
                else:
                    self.assertEqual(row[1:], (3, 1))
                    self.assertEqual(json.loads(self.outbox(model.registration_id)[-1][2])["display_name"], "changed")

    def test_competing_connection_cas_only_winner_invalidates(self):
        model = self.register("one")
        barrier = threading.Barrier(2, timeout=3)
        def save():
            barrier.wait()
            try:
                return SQLiteConnectionRepository(self.path).save(self.command())
            except ConnectionRevisionConflictError as error:
                return error
        with ThreadPoolExecutor(2) as workers:
            futures = [workers.submit(save) for _ in range(2)]
            outcomes = [future.result(timeout=5) for future in futures]
        self.assertEqual(sum(isinstance(value, ConnectionRevisionConflictError) for value in outcomes), 1)
        self.assertEqual([row[0] for row in self.outbox(model.registration_id)], [1, 2])

    def test_replaying_connection_save_after_removal_cannot_recreate_upsert(self):
        model = self.register("one")
        command = self.command()
        result = self.connections.save(command)
        self.models.remove(RemoveModelCommand(model.registration_id, 2, str(uuid4())))
        before = self.snapshot()
        self.assertEqual(SQLiteConnectionRepository(self.path).save(command), result)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.models.list_registered(), [])

    def test_helper_requires_transaction_and_schema_cannot_commit_it(self):
        record = self.register("one")
        before = self.snapshot()
        connection = sqlite3.connect(self.path)
        try:
            with self.assertRaises(ValueError):
                invalidate_connection_models(connection, connection_id=self.connection)
            connection.execute("BEGIN IMMEDIATE")
            changed = invalidate_connection_models(connection, connection_id=self.connection)
            self.assertEqual(changed[0].registration_id, record.registration_id)
            self.assertTrue(connection.in_transaction)
            with self.assertRaises(ValueError):
                ensure_model_schema(connection)
            self.assertTrue(connection.in_transaction)
            connection.rollback()
        finally:
            connection.close()
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()

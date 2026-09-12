import sqlite3
import threading
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from tempfile import TemporaryDirectory
from typing import Any

from model_deck.adapters.storage.sqlite_model_repository import SQLiteModelRepository
from model_deck.engine.model_library.ports import (
    ModelIdempotencyConflictError,
    ModelRegistrationNotFoundError,
    ModelRevisionConflictError,
    RegisterModelCommand,
    RegisteredModelRecord,
    RemoveModelCommand,
    RenameModelCommand,
)

CONNECTION_A = "550e8400-e29b-41d4-a716-446655440002"
CONNECTION_B = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
REGISTRATION_ID_1 = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
REGISTRATION_ID_2 = "8d3e3f4a-5b6c-7d8e-9f0a-1b2c3d4e5f60"


class IdSequence:
    def __init__(self, values: list[str]) -> None:
        self._values = list(values)
        self._index = 0

    def __call__(self) -> str:
        if self._index >= len(self._values):
            raise AssertionError("id factory exhausted")
        value = self._values[self._index]
        self._index += 1
        return value


def _repo(temp_dir: str, ids: list[str] | None = None) -> SQLiteModelRepository:
    db_path = Path(temp_dir) / "models.sqlite3"
    factory = IdSequence(ids or [REGISTRATION_ID_1, REGISTRATION_ID_2])
    return SQLiteModelRepository(db_path, id_factory=factory)



class SQLiteModelRepositoryTests(unittest.TestCase):
    def test_fresh_register_list_and_filter(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo = _repo(temp_dir, [REGISTRATION_ID_1, REGISTRATION_ID_2])
            first = repo.register(
                RegisterModelCommand(
                    connection_id=CONNECTION_A,
                    provider_model_id="openrouter/alpha",
                    display_name="Alpha",
                    expected_revision=0,
                    idempotency_key="reg-a",
                )
            )
            second = repo.register(
                RegisterModelCommand(
                    connection_id=CONNECTION_B,
                    provider_model_id="openrouter/beta",
                    display_name="Beta",
                    expected_revision=0,
                    idempotency_key="reg-b",
                )
            )
            self.assertEqual(first.registration_id, REGISTRATION_ID_1)
            self.assertEqual(first.revision, 1)
            all_rows = repo.list_registered()
            self.assertEqual(len(all_rows), 2)
            filtered = repo.list_registered(connection_id=CONNECTION_A)
            self.assertEqual(len(filtered), 1)
            self.assertEqual(filtered[0].provider_model_id, "openrouter/alpha")
            self.assertEqual(second.connection_id, CONNECTION_B)

    def test_rename_increments_revision(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo = _repo(temp_dir)
            created = repo.register(
                RegisterModelCommand(
                    connection_id=CONNECTION_A,
                    provider_model_id="openrouter/alpha",
                    display_name="Alpha",
                    expected_revision=0,
                    idempotency_key="reg-a",
                )
            )
            renamed = repo.rename(
                RenameModelCommand(
                    registration_id=created.registration_id,
                    display_name="Alpha Renamed",
                    expected_revision=1,
                    idempotency_key="ren-a",
                )
            )
            self.assertEqual(renamed.display_name, "Alpha Renamed")
            self.assertEqual(renamed.revision, 2)
            listed = repo.list_registered(connection_id=CONNECTION_A)[0]
            self.assertEqual(listed.display_name, "Alpha Renamed")
            self.assertEqual(listed.revision, 2)

    def test_remove_hides_and_reregister_creates_new_identity(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo = _repo(temp_dir, [REGISTRATION_ID_1, REGISTRATION_ID_2])
            created = repo.register(
                RegisterModelCommand(
                    connection_id=CONNECTION_A,
                    provider_model_id="openrouter/alpha",
                    display_name="Alpha",
                    expected_revision=0,
                    idempotency_key="reg-a",
                )
            )
            removed = repo.remove(
                RemoveModelCommand(
                    registration_id=created.registration_id,
                    expected_revision=1,
                    idempotency_key="rm-a",
                )
            )
            self.assertTrue(removed)
            self.assertEqual(repo.list_registered(), [])
            reregistered = repo.register(
                RegisterModelCommand(
                    connection_id=CONNECTION_A,
                    provider_model_id="openrouter/alpha",
                    display_name="Alpha Again",
                    expected_revision=0,
                    idempotency_key="reg-a-2",
                )
            )
            self.assertEqual(reregistered.registration_id, REGISTRATION_ID_2)
            self.assertNotEqual(reregistered.registration_id, created.registration_id)
            self.assertEqual(reregistered.revision, 1)

    def test_stale_revision_and_missing_errors(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo = _repo(temp_dir)
            created = repo.register(
                RegisterModelCommand(
                    connection_id=CONNECTION_A,
                    provider_model_id="openrouter/alpha",
                    display_name="Alpha",
                    expected_revision=0,
                    idempotency_key="reg-a",
                )
            )
            with self.assertRaises(ModelRevisionConflictError):
                repo.rename(
                    RenameModelCommand(
                        registration_id=created.registration_id,
                        display_name="Too Early",
                        expected_revision=0,
                        idempotency_key="ren-stale",
                    )
                )
            repo.remove(
                RemoveModelCommand(
                    registration_id=created.registration_id,
                    expected_revision=1,
                    idempotency_key="rm-a",
                )
            )
            with self.assertRaises(ModelRegistrationNotFoundError):
                repo.rename(
                    RenameModelCommand(
                        registration_id=created.registration_id,
                        display_name="Gone",
                        expected_revision=2,
                        idempotency_key="ren-missing",
                    )
                )

    def test_exact_idempotent_replay_across_new_repository_instance(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "models.sqlite3"
            register_cmd = RegisterModelCommand(
                connection_id=CONNECTION_A,
                provider_model_id="openrouter/alpha",
                display_name="Alpha",
                expected_revision=0,
                idempotency_key="reg-a",
            )
            rename_cmd = RenameModelCommand(
                registration_id=REGISTRATION_ID_1,
                display_name="Renamed",
                expected_revision=1,
                idempotency_key="ren-a",
            )
            remove_cmd = RemoveModelCommand(
                registration_id=REGISTRATION_ID_1,
                expected_revision=2,
                idempotency_key="rm-a",
            )
            repo1 = SQLiteModelRepository(db_path, id_factory=IdSequence([REGISTRATION_ID_1]))
            first = repo1.register(register_cmd)
            renamed = repo1.rename(rename_cmd)
            removed = repo1.remove(remove_cmd)
            self.assertTrue(removed)
            self.assertEqual(repo1.list_registered(), [])

            repo2 = SQLiteModelRepository(db_path, id_factory=IdSequence(["unused"]))
            replay_register = repo2.register(register_cmd)
            self.assertEqual(replay_register, first)
            replay_rename = repo2.rename(rename_cmd)
            self.assertEqual(replay_rename, renamed)
            remove_cmd_after = RemoveModelCommand(
                registration_id=first.registration_id,
                expected_revision=2,
                idempotency_key="rm-a",
            )
            replay_remove = repo2.remove(remove_cmd_after)
            self.assertTrue(replay_remove)
            self.assertEqual(repo2.list_registered(), [])

    def test_changed_payload_same_key_conflicts_without_mutation(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo = _repo(temp_dir)
            repo.register(
                RegisterModelCommand(
                    connection_id=CONNECTION_A,
                    provider_model_id="openrouter/alpha",
                    display_name="Alpha",
                    expected_revision=0,
                    idempotency_key="shared-key",
                )
            )
            with self.assertRaises(ModelIdempotencyConflictError):
                repo.register(
                    RegisterModelCommand(
                        connection_id=CONNECTION_A,
                        provider_model_id="openrouter/alpha",
                        display_name="Different",
                        expected_revision=0,
                        idempotency_key="shared-key",
                    )
                )
            listed = repo.list_registered()
            self.assertEqual(listed[0].display_name, "Alpha")
            self.assertEqual(listed[0].revision, 1)

    def test_same_key_allowed_across_different_operations(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo = _repo(temp_dir)
            created = repo.register(
                RegisterModelCommand(
                    connection_id=CONNECTION_A,
                    provider_model_id="openrouter/alpha",
                    display_name="Alpha",
                    expected_revision=0,
                    idempotency_key="shared-key",
                )
            )
            renamed = repo.rename(
                RenameModelCommand(
                    registration_id=created.registration_id,
                    display_name="Alpha",
                    expected_revision=1,
                    idempotency_key="shared-key",
                )
            )
            self.assertEqual(renamed.revision, 2)

    def test_concurrent_register_same_slot_one_success_one_conflict(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "models.sqlite3"
            lock = threading.Lock()
            issued: list[str] = []

            def id_factory() -> str:
                with lock:
                    if not issued:
                        issued.append(REGISTRATION_ID_1)
                        return REGISTRATION_ID_1
                    return REGISTRATION_ID_2

            def attempt(key: str) -> Any:
                repo = SQLiteModelRepository(db_path, id_factory=id_factory)
                return repo.register(
                    RegisterModelCommand(
                        connection_id=CONNECTION_A,
                        provider_model_id="openrouter/alpha",
                        display_name="Alpha",
                        expected_revision=0,
                        idempotency_key=key,
                    )
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(attempt, "k1"), pool.submit(attempt, "k2")]
                outcomes: list[Any] = []
                errors: list[BaseException] = []
                for future in as_completed(futures):
                    try:
                        outcomes.append(future.result())
                    except BaseException as exc:  # noqa: BLE001
                        errors.append(exc)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], ModelRevisionConflictError)
            self.assertEqual(len(SQLiteModelRepository(db_path).list_registered()), 1)


    def test_concurrent_rename_same_revision_one_success_one_conflict(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "models.sqlite3"
            repo = SQLiteModelRepository(db_path, id_factory=IdSequence([REGISTRATION_ID_1]))
            created = repo.register(
                RegisterModelCommand(
                    connection_id=CONNECTION_A,
                    provider_model_id="openrouter/alpha",
                    display_name="Alpha",
                    expected_revision=0,
                    idempotency_key="reg-setup",
                )
            )
            self.assertEqual(created.revision, 1)

            def attempt(display_name: str, key: str) -> RegisteredModelRecord:
                worker = SQLiteModelRepository(db_path, id_factory=IdSequence(["unused"]))
                return worker.rename(
                    RenameModelCommand(
                        registration_id=created.registration_id,
                        display_name=display_name,
                        expected_revision=1,
                        idempotency_key=key,
                    )
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(attempt, "Winner Name", "ren-k1"),
                    pool.submit(attempt, "Loser Name", "ren-k2"),
                ]
                outcomes: list[RegisteredModelRecord] = []
                errors: list[BaseException] = []
                for future in as_completed(futures):
                    try:
                        outcomes.append(future.result())
                    except BaseException as exc:  # noqa: BLE001
                        errors.append(exc)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], ModelRevisionConflictError)
            winner = outcomes[0]
            self.assertEqual(winner.revision, 2)
            stored = SQLiteModelRepository(db_path).list_registered(connection_id=CONNECTION_A)[0]
            self.assertEqual(stored.registration_id, winner.registration_id)
            self.assertEqual(stored.display_name, winner.display_name)
            self.assertEqual(stored.revision, 2)

    def test_schema_has_no_secret_columns(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo = _repo(temp_dir)
            repo.register(
                RegisterModelCommand(
                    connection_id=CONNECTION_A,
                    provider_model_id="openrouter/alpha",
                    display_name="Alpha",
                    expected_revision=0,
                    idempotency_key="reg-a",
                )
            )
            db_path = Path(temp_dir) / "models.sqlite3"
            conn = sqlite3.connect(db_path)
            try:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                forbidden = ("credential", "secret", "token")
                for table in tables:
                    columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
                    for column in columns:
                        name = str(column[1]).casefold()
                        for marker in forbidden:
                            self.assertNotIn(marker, name)
            finally:
                conn.close()



def _fetch_outbox_rows(db_path: Path) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT outbox_id, aggregate_type, aggregate_id, aggregate_revision, event_kind, payload_json, state "
            "FROM projection_outbox ORDER BY outbox_id"
        ).fetchall()
    finally:
        conn.close()


class SQLiteModelRepositoryOutboxTests(unittest.TestCase):
    def test_mutations_enqueue_pending_projection_events(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "models.sqlite3"
            repo = SQLiteModelRepository(db_path, id_factory=IdSequence([REGISTRATION_ID_1]))
            created = repo.register(
                RegisterModelCommand(
                    connection_id=CONNECTION_A,
                    provider_model_id="openrouter/alpha",
                    display_name="Alpha",
                    expected_revision=0,
                    idempotency_key="reg-a",
                )
            )
            renamed = repo.rename(
                RenameModelCommand(
                    registration_id=created.registration_id,
                    display_name="Alpha Renamed",
                    expected_revision=1,
                    idempotency_key="ren-a",
                )
            )
            removed = repo.remove(
                RemoveModelCommand(
                    registration_id=created.registration_id,
                    expected_revision=2,
                    idempotency_key="rm-a",
                )
            )
            self.assertTrue(removed)
            rows = _fetch_outbox_rows(db_path)
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0][1], "registered_model")
            self.assertEqual(rows[0][2], REGISTRATION_ID_1)
            self.assertEqual(rows[0][3], 1)
            self.assertEqual(rows[0][4], "registered_model.upserted")
            self.assertEqual(
                rows[0][5],
                '{"connection_id":"550e8400-e29b-41d4-a716-446655440002","display_name":"Alpha","provider_model_id":"openrouter/alpha","registration_id":"7c9e6679-7425-40de-944b-e07fc1f90ae7","revision":1}',
            )
            self.assertEqual(rows[0][6], "pending")
            self.assertEqual(rows[1][3], renamed.revision)
            self.assertEqual(rows[1][4], "registered_model.upserted")
            self.assertEqual(
                rows[1][5],
                '{"connection_id":"550e8400-e29b-41d4-a716-446655440002","display_name":"Alpha Renamed","provider_model_id":"openrouter/alpha","registration_id":"7c9e6679-7425-40de-944b-e07fc1f90ae7","revision":2}',
            )
            self.assertEqual(rows[2][3], 3)
            self.assertEqual(rows[2][4], "registered_model.removed")
            self.assertEqual(
                rows[2][5],
                '{"registration_id":"7c9e6679-7425-40de-944b-e07fc1f90ae7","removed":true,"revision":3}',
            )
            self.assertEqual([row[0] for row in rows], [1, 2, 3])

    def test_exact_idempotent_replay_does_not_enqueue_additional_events(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "models.sqlite3"
            register_cmd = RegisterModelCommand(
                connection_id=CONNECTION_A,
                provider_model_id="openrouter/alpha",
                display_name="Alpha",
                expected_revision=0,
                idempotency_key="reg-a",
            )
            repo1 = SQLiteModelRepository(db_path, id_factory=IdSequence([REGISTRATION_ID_1]))
            repo1.register(register_cmd)
            self.assertEqual(len(_fetch_outbox_rows(db_path)), 1)
            repo2 = SQLiteModelRepository(db_path, id_factory=IdSequence(["unused"]))
            repo2.register(register_cmd)
            self.assertEqual(len(_fetch_outbox_rows(db_path)), 1)

    def test_failed_mutations_do_not_enqueue_events(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "models.sqlite3"
            repo = SQLiteModelRepository(db_path, id_factory=IdSequence([REGISTRATION_ID_1]))
            created = repo.register(
                RegisterModelCommand(
                    connection_id=CONNECTION_A,
                    provider_model_id="openrouter/alpha",
                    display_name="Alpha",
                    expected_revision=0,
                    idempotency_key="reg-a",
                )
            )
            with self.assertRaises(ModelRevisionConflictError):
                repo.rename(
                    RenameModelCommand(
                        registration_id=created.registration_id,
                        display_name="Too Early",
                        expected_revision=0,
                        idempotency_key="ren-stale",
                    )
                )
            with self.assertRaises(ModelIdempotencyConflictError):
                repo.register(
                    RegisterModelCommand(
                        connection_id=CONNECTION_A,
                        provider_model_id="openrouter/alpha",
                        display_name="Different",
                        expected_revision=0,
                        idempotency_key="reg-a",
                    )
                )
            self.assertEqual(len(_fetch_outbox_rows(db_path)), 1)

    def test_outbox_insert_failure_rolls_back_state_and_receipt(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "models.sqlite3"
            repo = SQLiteModelRepository(db_path, id_factory=IdSequence([REGISTRATION_ID_1]))
            created = repo.register(
                RegisterModelCommand(
                    connection_id=CONNECTION_A,
                    provider_model_id="openrouter/alpha",
                    display_name="Alpha",
                    expected_revision=0,
                    idempotency_key="reg-a",
                )
            )
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "CREATE TRIGGER projection_outbox_fail_insert "
                    "BEFORE INSERT ON projection_outbox "
                    "BEGIN SELECT RAISE(ABORT, 'forced outbox failure'); END"
                )
                conn.commit()
            finally:
                conn.close()
            rename_cmd = RenameModelCommand(
                registration_id=created.registration_id,
                display_name="Should Not Stick",
                expected_revision=1,
                idempotency_key="ren-fail",
            )
            with self.assertRaises(sqlite3.IntegrityError):
                repo.rename(rename_cmd)
            stored = SQLiteModelRepository(db_path).list_registered(connection_id=CONNECTION_A)[0]
            self.assertEqual(stored.display_name, "Alpha")
            self.assertEqual(stored.revision, 1)
            self.assertEqual(len(_fetch_outbox_rows(db_path)), 1)
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("DROP TRIGGER projection_outbox_fail_insert")
                conn.commit()
            finally:
                conn.close()
            renamed = repo.rename(rename_cmd)
            self.assertEqual(renamed.display_name, "Should Not Stick")
            self.assertEqual(renamed.revision, 2)
            replay = repo.rename(rename_cmd)
            self.assertEqual(replay, renamed)
            self.assertEqual(len(_fetch_outbox_rows(db_path)), 2)


if __name__ == "__main__":
    unittest.main()

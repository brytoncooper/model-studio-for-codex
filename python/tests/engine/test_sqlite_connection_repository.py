import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from model_deck.adapters.storage.sqlite_connection_repository import SQLiteConnectionRepository
from model_deck.adapters.storage.sqlite_model_repository import SQLiteModelRepository
from model_deck.engine.connections.ports import (
    ConnectionIdempotencyConflictError,
    ConnectionRecord,
    ConnectionRevisionConflictError,
    SaveConnectionCommand,
)
from model_deck.engine.model_library.ports import RegisterModelCommand

CONNECTION_A = "550e8400-e29b-41d4-a716-446655440002"
CONNECTION_B = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
PROVIDER_A = "com.example.provider"
PROVIDER_B = "com.other.provider"
CREDENTIAL_REF = "550e8400-e29b-41d4-a716-446655440010"
ENDPOINT_REF = "ref:endpoint.config"
REGISTRATION_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


def _repo(temp_dir: str, name: str = "state.sqlite3") -> SQLiteConnectionRepository:
    return SQLiteConnectionRepository(Path(temp_dir) / name)


def _create_cmd(
    *,
    connection_id: str = CONNECTION_A,
    provider_id: str = PROVIDER_A,
    key: str = "create-1",
    endpoint_config_ref: str | None = None,
    credential_ref: str | None = None,
) -> SaveConnectionCommand:
    return SaveConnectionCommand(
        connection_id=connection_id,
        provider_id=provider_id,
        expected_revision=0,
        idempotency_key=key,
        endpoint_config_ref=endpoint_config_ref,
        credential_ref=credential_ref,
    )


def _update_cmd(
    *,
    expected_revision: int,
    key: str,
    connection_id: str = CONNECTION_A,
    provider_id: str = PROVIDER_A,
    endpoint_config_ref: str | None = ENDPOINT_REF,
    credential_ref: str | None = None,
) -> SaveConnectionCommand:
    return SaveConnectionCommand(
        connection_id=connection_id,
        provider_id=provider_id,
        expected_revision=expected_revision,
        idempotency_key=key,
        endpoint_config_ref=endpoint_config_ref,
        credential_ref=credential_ref,
    )


class SQLiteConnectionRepositoryTests(unittest.TestCase):
    def test_create_and_list_with_omitted_and_populated_refs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo = _repo(temp_dir)
            created = repo.save(_create_cmd(key="c1"))
            self.assertEqual(
                created,
                ConnectionRecord(
                    connection_id=CONNECTION_A,
                    provider_id=PROVIDER_A,
                    revision=1,
                ),
            )
            with_refs = repo.save(
                _create_cmd(
                    connection_id=CONNECTION_B,
                    provider_id=PROVIDER_B,
                    key="c2",
                    endpoint_config_ref=ENDPOINT_REF,
                    credential_ref=CREDENTIAL_REF,
                )
            )
            self.assertEqual(with_refs.endpoint_config_ref, ENDPOINT_REF)
            self.assertEqual(with_refs.credential_ref, CREDENTIAL_REF)
            listed = repo.list_connections()
            self.assertEqual(len(listed), 2)
            self.assertEqual(listed[0].connection_id, CONNECTION_A)
            self.assertEqual(listed[1].connection_id, CONNECTION_B)
            self.assertEqual(listed[0].provider_id, PROVIDER_A)
            self.assertEqual(listed[1].provider_id, PROVIDER_B)

    def test_update_increments_revision_and_replaces_or_clears_refs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo = _repo(temp_dir)
            repo.save(
                _create_cmd(
                    key="c1",
                    endpoint_config_ref=ENDPOINT_REF,
                    credential_ref=CREDENTIAL_REF,
                )
            )
            updated = repo.save(
                _update_cmd(
                    expected_revision=1,
                    key="u1",
                    endpoint_config_ref="ref:endpoint.other",
                    credential_ref=None,
                )
            )
            self.assertEqual(updated.revision, 2)
            self.assertEqual(updated.endpoint_config_ref, "ref:endpoint.other")
            self.assertIsNone(updated.credential_ref)
            cleared = repo.save(
                _update_cmd(
                    expected_revision=2,
                    key="u2",
                    endpoint_config_ref=None,
                    credential_ref=CREDENTIAL_REF,
                )
            )
            self.assertEqual(cleared.revision, 3)
            self.assertIsNone(cleared.endpoint_config_ref)
            self.assertEqual(cleared.credential_ref, CREDENTIAL_REF)

    def test_missing_nonzero_and_existing_stale_revision_conflicts(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo = _repo(temp_dir)
            with self.assertRaises(ConnectionRevisionConflictError):
                repo.save(_update_cmd(expected_revision=1, key="missing"))
            repo.save(_create_cmd(key="c1"))
            with self.assertRaises(ConnectionRevisionConflictError):
                repo.save(_create_cmd(key="dup-create"))
            with self.assertRaises(ConnectionRevisionConflictError):
                repo.save(_update_cmd(expected_revision=0, key="stale-zero"))
            with self.assertRaises(ConnectionRevisionConflictError):
                repo.save(_update_cmd(expected_revision=99, key="stale-high"))

    def test_exact_create_receipt_replay_after_later_update(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            create_cmd = _create_cmd(key="create-key")
            repo1 = SQLiteConnectionRepository(db_path)
            first = repo1.save(create_cmd)
            self.assertEqual(first.revision, 1)
            repo1.save(_update_cmd(expected_revision=1, key="update-key", endpoint_config_ref=ENDPOINT_REF))
            repo2 = SQLiteConnectionRepository(db_path)
            replay = repo2.save(create_cmd)
            self.assertEqual(replay, first)
            self.assertEqual(replay.revision, 1)
            self.assertIsNone(replay.endpoint_config_ref)
            current = repo2.list_connections()[0]
            self.assertEqual(current.revision, 2)
            self.assertEqual(current.endpoint_config_ref, ENDPOINT_REF)

    def test_changed_payload_same_key_conflicts_without_state_change(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo = _repo(temp_dir)
            repo.save(_create_cmd(key="shared"))
            with self.assertRaises(ConnectionIdempotencyConflictError):
                repo.save(
                    SaveConnectionCommand(
                        connection_id=CONNECTION_A,
                        provider_id=PROVIDER_A,
                        expected_revision=0,
                        idempotency_key="shared",
                        endpoint_config_ref=ENDPOINT_REF,
                    )
                )
            listed = repo.list_connections()[0]
            self.assertEqual(listed.revision, 1)
            self.assertIsNone(listed.endpoint_config_ref)

    def test_concurrent_updates_same_expected_revision_one_winner(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            setup = SQLiteConnectionRepository(db_path)
            setup.save(_create_cmd(key="setup"))

            def attempt(endpoint_ref: str, key: str) -> ConnectionRecord:
                worker = SQLiteConnectionRepository(db_path)
                return worker.save(
                    _update_cmd(
                        expected_revision=1,
                        key=key,
                        endpoint_config_ref=endpoint_ref,
                    )
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(attempt, "ref:winner", "k1"),
                    pool.submit(attempt, "ref:loser", "k2"),
                ]
                outcomes: list[ConnectionRecord] = []
                errors: list[BaseException] = []
                for future in as_completed(futures):
                    try:
                        outcomes.append(future.result())
                    except BaseException as exc:  # noqa: BLE001
                        errors.append(exc)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], ConnectionRevisionConflictError)
            winner = outcomes[0]
            self.assertEqual(winner.revision, 2)
            stored = SQLiteConnectionRepository(db_path).list_connections()[0]
            self.assertEqual(stored.revision, 2)
            self.assertEqual(stored.endpoint_config_ref, winner.endpoint_config_ref)

    def test_shared_db_path_after_model_repository_init(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "shared.sqlite3"
            model_repo = SQLiteModelRepository(db_path, id_factory=lambda: REGISTRATION_ID)
            registered = model_repo.register(
                RegisterModelCommand(
                    connection_id=CONNECTION_A,
                    provider_model_id="openrouter/alpha",
                    display_name="Alpha",
                    expected_revision=0,
                    idempotency_key="model-reg",
                )
            )
            conn_repo = SQLiteConnectionRepository(db_path)
            saved = conn_repo.save(_create_cmd(key="conn-create"))
            self.assertEqual(saved.revision, 1)
            models = model_repo.list_registered()
            self.assertEqual(len(models), 1)
            self.assertEqual(models[0].registration_id, registered.registration_id)
            self.assertEqual(models[0].display_name, "Alpha")

    def test_receipt_insert_failure_rolls_back_and_retry_succeeds(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            repo = SQLiteConnectionRepository(db_path)
            repo.save(_create_cmd(key="ok"))
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "CREATE TRIGGER connection_idempotency_fail_insert "
                    "BEFORE INSERT ON connection_idempotency "
                    "BEGIN SELECT RAISE(ABORT, 'forced receipt failure'); END"
                )
                conn.commit()
            finally:
                conn.close()
            failing_cmd = _update_cmd(expected_revision=1, key="fail-key", endpoint_config_ref=ENDPOINT_REF)
            with self.assertRaises(sqlite3.IntegrityError):
                repo.save(failing_cmd)
            before_retry = SQLiteConnectionRepository(db_path).list_connections()[0]
            self.assertEqual(before_retry.revision, 1)
            self.assertIsNone(before_retry.endpoint_config_ref)
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("DROP TRIGGER connection_idempotency_fail_insert")
                conn.commit()
            finally:
                conn.close()
            after_retry = repo.save(failing_cmd)
            self.assertEqual(after_retry.revision, 2)
            self.assertEqual(after_retry.endpoint_config_ref, ENDPOINT_REF)
            replay = repo.save(failing_cmd)
            self.assertEqual(replay, after_retry)

    def test_schema_allows_credential_ref_and_rejects_secret_columns(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo = _repo(temp_dir)
            repo.save(_create_cmd(key="schema", credential_ref=CREDENTIAL_REF))
            db_path = Path(temp_dir) / "state.sqlite3"
            conn = sqlite3.connect(db_path)
            try:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                for table in tables:
                    columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
                    for column in columns:
                        name = str(column[1])
                        lower = name.casefold()
                        if lower == "credential_ref":
                            continue
                        self.assertNotIn("secret", lower)
                        self.assertNotIn("token", lower)
                        self.assertNotIn("api_key", lower)
                        self.assertNotIn("api-key", lower)
                        if "credential" in lower:
                            self.fail(f"unexpected credential column: {name}")
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


class SQLiteConnectionRepositoryOutboxTests(unittest.TestCase):
    def test_save_operations_enqueue_pending_projection_events(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            repo = SQLiteConnectionRepository(db_path)
            created = repo.save(_create_cmd(key="c1"))
            updated = repo.save(
                _update_cmd(
                    expected_revision=1,
                    key="u1",
                    endpoint_config_ref=ENDPOINT_REF,
                    credential_ref=CREDENTIAL_REF,
                )
            )
            rows = _fetch_outbox_rows(db_path)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0][1], "connection")
            self.assertEqual(rows[0][2], CONNECTION_A)
            self.assertEqual(rows[0][3], 1)
            self.assertEqual(rows[0][4], "connection.saved")
            self.assertEqual(
                rows[0][5],
                '{"connection_id":"550e8400-e29b-41d4-a716-446655440002","credential_ref":null,"endpoint_config_ref":null,"provider_id":"com.example.provider","revision":1}',
            )
            self.assertEqual(rows[0][6], "pending")
            self.assertEqual(rows[1][3], updated.revision)
            self.assertEqual(rows[1][4], "connection.saved")
            self.assertEqual(
                rows[1][5],
                '{"connection_id":"550e8400-e29b-41d4-a716-446655440002","credential_ref":"550e8400-e29b-41d4-a716-446655440010","endpoint_config_ref":"ref:endpoint.config","provider_id":"com.example.provider","revision":2}',
            )
            self.assertEqual([row[0] for row in rows], [1, 2])

    def test_exact_idempotent_replay_does_not_enqueue_additional_events(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            create_cmd = _create_cmd(key="create-key")
            repo1 = SQLiteConnectionRepository(db_path)
            repo1.save(create_cmd)
            self.assertEqual(len(_fetch_outbox_rows(db_path)), 1)
            repo1.save(_update_cmd(expected_revision=1, key="update-key", endpoint_config_ref=ENDPOINT_REF))
            self.assertEqual(len(_fetch_outbox_rows(db_path)), 2)
            repo2 = SQLiteConnectionRepository(db_path)
            repo2.save(create_cmd)
            self.assertEqual(len(_fetch_outbox_rows(db_path)), 2)

    def test_failed_mutations_do_not_enqueue_events(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            repo = SQLiteConnectionRepository(db_path)
            repo.save(_create_cmd(key="c1"))
            with self.assertRaises(ConnectionRevisionConflictError):
                repo.save(_update_cmd(expected_revision=99, key="stale"))
            with self.assertRaises(ConnectionIdempotencyConflictError):
                repo.save(
                    SaveConnectionCommand(
                        connection_id=CONNECTION_A,
                        provider_id=PROVIDER_A,
                        expected_revision=0,
                        idempotency_key="c1",
                        endpoint_config_ref=ENDPOINT_REF,
                    )
                )
            self.assertEqual(len(_fetch_outbox_rows(db_path)), 1)

    def test_outbox_insert_failure_rolls_back_state_and_receipt(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            repo = SQLiteConnectionRepository(db_path)
            repo.save(_create_cmd(key="c1"))
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
            failing_cmd = _update_cmd(
                expected_revision=1,
                key="fail-key",
                endpoint_config_ref=ENDPOINT_REF,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                repo.save(failing_cmd)
            before_retry = SQLiteConnectionRepository(db_path).list_connections()[0]
            self.assertEqual(before_retry.revision, 1)
            self.assertIsNone(before_retry.endpoint_config_ref)
            self.assertEqual(len(_fetch_outbox_rows(db_path)), 1)
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("DROP TRIGGER projection_outbox_fail_insert")
                conn.commit()
            finally:
                conn.close()
            after_retry = repo.save(failing_cmd)
            self.assertEqual(after_retry.revision, 2)
            replay = repo.save(failing_cmd)
            self.assertEqual(replay, after_retry)
            self.assertEqual(len(_fetch_outbox_rows(db_path)), 2)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable

from model_deck.adapters.storage.sqlite_connection_repository import (
    SQLiteConnectionRepository,
)
from model_deck.adapters.storage.sqlite_model_repository import SQLiteModelRepository
from model_deck.engine.connections.ports import (
    ConnectionIdempotencyConflictError,
    ConnectionRecord,
    ConnectionRevisionConflictError,
    SaveConnectionCommand,
)
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
REGISTRATION_A = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
REGISTRATION_B = "8d3e3f4a-5b6c-7d8e-9f0a-1b2c3d4e5f60"


class _IdSequence:
    def __init__(self, values: tuple[str, ...]) -> None:
        self._values = iter(values)
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            return next(self._values)


class FakeModelRepository:
    """Thread-safe behavioral fake for the model repository public contract."""

    def __init__(self, id_factory: Callable[[], str]) -> None:
        self._id_factory = id_factory
        self._records: dict[str, tuple[RegisteredModelRecord, bool]] = {}
        self._receipts: dict[tuple[str, str], tuple[object, object]] = {}
        self._lock = threading.Lock()

    def list_registered(
        self, *, connection_id: str | None = None
    ) -> list[RegisteredModelRecord]:
        with self._lock:
            records = [
                replace(record)
                for record, active in self._records.values()
                if active and (connection_id is None or record.connection_id == connection_id)
            ]
        return sorted(
            records,
            key=lambda record: (
                record.connection_id,
                record.provider_model_id,
                record.registration_id,
            ),
        )

    def register(self, command: RegisterModelCommand) -> RegisteredModelRecord:
        request = (
            command.connection_id,
            command.provider_model_id,
            command.display_name,
            command.expected_revision,
        )
        with self._lock:
            replay = self._replay("register", command.idempotency_key, request)
            if replay is not None:
                return replace(replay)
            if command.expected_revision != 0:
                raise ModelRevisionConflictError("register requires expected_revision 0")
            if any(
                active
                and record.connection_id == command.connection_id
                and record.provider_model_id == command.provider_model_id
                for record, active in self._records.values()
            ):
                raise ModelRevisionConflictError(
                    "active model already registered for connection"
                )
            record = RegisteredModelRecord(
                registration_id=self._id_factory(),
                provider_model_id=command.provider_model_id,
                connection_id=command.connection_id,
                display_name=command.display_name,
                revision=1,
            )
            self._records[record.registration_id] = (record, True)
            self._receipts[("register", command.idempotency_key)] = (request, record)
            return replace(record)

    def rename(self, command: RenameModelCommand) -> RegisteredModelRecord:
        request = (
            command.registration_id,
            command.display_name,
            command.expected_revision,
        )
        with self._lock:
            replay = self._replay("rename", command.idempotency_key, request)
            if replay is not None:
                return replace(replay)
            current = self._active(command.registration_id)
            if current.revision != command.expected_revision:
                raise ModelRevisionConflictError("stale revision")
            renamed = replace(
                current,
                display_name=command.display_name,
                revision=current.revision + 1,
            )
            self._records[renamed.registration_id] = (renamed, True)
            self._receipts[("rename", command.idempotency_key)] = (request, renamed)
            return replace(renamed)

    def remove(self, command: RemoveModelCommand) -> bool:
        request = (command.registration_id, command.expected_revision)
        with self._lock:
            replay = self._replay("remove", command.idempotency_key, request)
            if replay is not None:
                return bool(replay)
            current = self._active(command.registration_id)
            if current.revision != command.expected_revision:
                raise ModelRevisionConflictError("stale revision")
            self._records[current.registration_id] = (
                replace(current, revision=current.revision + 1),
                False,
            )
            self._receipts[("remove", command.idempotency_key)] = (request, True)
            return True

    def _active(self, registration_id: str) -> RegisteredModelRecord:
        stored = self._records.get(registration_id)
        if stored is None or not stored[1]:
            raise ModelRegistrationNotFoundError("registration not found")
        return stored[0]

    def _replay(self, operation: str, key: str, request: object) -> object | None:
        receipt = self._receipts.get((operation, key))
        if receipt is None:
            return None
        if receipt[0] != request:
            raise ModelIdempotencyConflictError(
                "idempotency key reused with different request payload"
            )
        return receipt[1]


class FakeConnectionRepository:
    """Thread-safe behavioral fake for the connection repository public contract."""

    def __init__(self) -> None:
        self._records: dict[str, ConnectionRecord] = {}
        self._receipts: dict[str, tuple[object, ConnectionRecord]] = {}
        self._lock = threading.Lock()

    def list_connections(self) -> list[ConnectionRecord]:
        with self._lock:
            records = [replace(record) for record in self._records.values()]
        return sorted(records, key=lambda record: (record.provider_id, record.connection_id))

    def save(self, command: SaveConnectionCommand) -> ConnectionRecord:
        request = (
            command.connection_id,
            command.provider_id,
            command.expected_revision,
            command.endpoint_config_ref,
            command.credential_ref,
        )
        with self._lock:
            receipt = self._receipts.get(command.idempotency_key)
            if receipt is not None:
                if receipt[0] != request:
                    raise ConnectionIdempotencyConflictError(
                        "idempotency key reused with different request payload"
                    )
                return replace(receipt[1])
            current = self._records.get(command.connection_id)
            if current is None:
                if command.expected_revision != 0:
                    raise ConnectionRevisionConflictError("connection not found")
                saved = ConnectionRecord(
                    connection_id=command.connection_id,
                    provider_id=command.provider_id,
                    revision=1,
                    endpoint_config_ref=command.endpoint_config_ref,
                    credential_ref=command.credential_ref,
                )
            else:
                if command.expected_revision == 0:
                    raise ConnectionRevisionConflictError("connection already exists")
                if current.revision != command.expected_revision:
                    raise ConnectionRevisionConflictError("stale revision")
                saved = ConnectionRecord(
                    connection_id=command.connection_id,
                    provider_id=command.provider_id,
                    revision=current.revision + 1,
                    endpoint_config_ref=command.endpoint_config_ref,
                    credential_ref=command.credential_ref,
                )
            self._records[saved.connection_id] = saved
            self._receipts[command.idempotency_key] = (request, saved)
            return replace(saved)


class RepositoryConformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def model_implementations(self):
        for name in ("fake", "sqlite"):
            ids = _IdSequence((REGISTRATION_A, REGISTRATION_B))
            if name == "fake":
                repository = FakeModelRepository(ids)
                factory = lambda repository=repository: repository
            else:
                path = self.root / f"{name}-models.sqlite3"
                repository = SQLiteModelRepository(path, id_factory=ids)
                factory = lambda path=path, ids=ids: SQLiteModelRepository(
                    path, id_factory=ids
                )
            yield name, repository, factory

    def connection_implementations(self):
        for name in ("fake", "sqlite"):
            if name == "fake":
                repository = FakeConnectionRepository()
                factory = lambda repository=repository: repository
            else:
                path = self.root / f"{name}-connections.sqlite3"
                repository = SQLiteConnectionRepository(path)
                factory = lambda path=path: SQLiteConnectionRepository(path)
            yield name, repository, factory

    @staticmethod
    def register(key: str = "register") -> RegisterModelCommand:
        return RegisterModelCommand(
            connection_id=CONNECTION_A,
            provider_model_id="openrouter/alpha",
            display_name="Alpha",
            expected_revision=0,
            idempotency_key=key,
        )

    @staticmethod
    def save(
        *, expected_revision: int = 0, key: str = "save", endpoint: str | None = None
    ) -> SaveConnectionCommand:
        return SaveConnectionCommand(
            connection_id=CONNECTION_A,
            provider_id="com.example.provider",
            expected_revision=expected_revision,
            idempotency_key=key,
            endpoint_config_ref=endpoint,
            credential_ref="ref:fixture.credential",
        )

    def test_model_lifecycle_identity_revision_and_detached_reads(self) -> None:
        for name, repository, _ in self.model_implementations():
            with self.subTest(implementation=name):
                created = repository.register(self.register())
                listed = repository.list_registered(connection_id=CONNECTION_A)
                self.assertEqual(listed, [created])
                self.assertIsNot(listed[0], created)
                renamed = repository.rename(
                    RenameModelCommand(created.registration_id, "Renamed", 1, "rename")
                )
                self.assertEqual(renamed.registration_id, created.registration_id)
                self.assertEqual(renamed.revision, 2)
                self.assertTrue(
                    repository.remove(RemoveModelCommand(created.registration_id, 2, "remove"))
                )
                self.assertEqual(repository.list_registered(), [])
                with self.assertRaises(ModelRegistrationNotFoundError):
                    repository.rename(
                        RenameModelCommand(created.registration_id, "Gone", 3, "gone")
                    )
                replacement = repository.register(self.register("register-again"))
                self.assertNotEqual(replacement.registration_id, created.registration_id)
                self.assertEqual(replacement.revision, 1)

    def test_model_exact_replays_and_changed_payload_conflict(self) -> None:
        for name, repository, _ in self.model_implementations():
            with self.subTest(implementation=name):
                command = self.register()
                created = repository.register(command)
                rename_command = RenameModelCommand(
                    created.registration_id, "Renamed", 1, "rename"
                )
                renamed = repository.rename(
                    rename_command
                )
                register_replay = repository.register(command)
                rename_replay = repository.rename(rename_command)
                self.assertEqual(register_replay, created)
                self.assertEqual(rename_replay, renamed)
                self.assertIsNot(register_replay, created)
                self.assertIsNot(rename_replay, renamed)
                with self.assertRaises(ModelIdempotencyConflictError):
                    repository.register(replace(command, display_name="Different"))
                self.assertEqual(repository.list_registered()[0].display_name, "Renamed")
                remove_command = RemoveModelCommand(
                    created.registration_id, 2, "remove"
                )
                self.assertTrue(repository.remove(remove_command))
                self.assertTrue(repository.remove(remove_command))
                with self.assertRaises(ModelIdempotencyConflictError):
                    repository.remove(replace(remove_command, expected_revision=99))
                self.assertEqual(repository.list_registered(), [])

    def test_model_conflicts_and_competing_updates(self) -> None:
        for name, repository, factory in self.model_implementations():
            with self.subTest(implementation=name):
                with self.assertRaises(ModelRevisionConflictError):
                    repository.register(replace(self.register(), expected_revision=1))
                created = repository.register(self.register("setup"))
                barrier = threading.Barrier(2)

                def attempt(display_name: str, key: str):
                    barrier.wait()
                    return factory().rename(
                        RenameModelCommand(created.registration_id, display_name, 1, key)
                    )

                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [
                        pool.submit(attempt, "First", "first"),
                        pool.submit(attempt, "Second", "second"),
                    ]
                    outcomes, errors = [], []
                    for future in futures:
                        try:
                            outcomes.append(future.result())
                        except BaseException as error:
                            errors.append(error)
                self.assertEqual(len(outcomes), 1)
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], ModelRevisionConflictError)
                self.assertEqual(repository.list_registered()[0], outcomes[0])

    def test_connection_lifecycle_revision_refs_and_detached_reads(self) -> None:
        for name, repository, _ in self.connection_implementations():
            with self.subTest(implementation=name):
                created = repository.save(self.save())
                listed = repository.list_connections()
                self.assertEqual(listed, [created])
                self.assertIsNot(listed[0], created)
                updated = repository.save(
                    self.save(expected_revision=1, key="update", endpoint="ref:endpoint.two")
                )
                self.assertEqual(updated.connection_id, created.connection_id)
                self.assertEqual(updated.revision, 2)
                self.assertEqual(updated.endpoint_config_ref, "ref:endpoint.two")

    def test_connection_exact_replay_and_changed_payload_conflict(self) -> None:
        for name, repository, _ in self.connection_implementations():
            with self.subTest(implementation=name):
                command = self.save()
                created = repository.save(command)
                repository.save(self.save(expected_revision=1, key="update", endpoint="ref:new"))
                replay = repository.save(command)
                self.assertEqual(replay, created)
                self.assertIsNot(replay, created)
                with self.assertRaises(ConnectionIdempotencyConflictError):
                    repository.save(replace(command, endpoint_config_ref="ref:different"))
                self.assertEqual(repository.list_connections()[0].revision, 2)

    def test_connection_conflicts_and_competing_updates(self) -> None:
        for name, repository, factory in self.connection_implementations():
            with self.subTest(implementation=name):
                with self.assertRaises(ConnectionRevisionConflictError):
                    repository.save(self.save(expected_revision=1, key="missing"))
                repository.save(self.save(key="setup"))
                barrier = threading.Barrier(2)

                def attempt(endpoint: str, key: str):
                    barrier.wait()
                    return factory().save(
                        self.save(expected_revision=1, key=key, endpoint=endpoint)
                    )

                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [
                        pool.submit(attempt, "ref:first", "first"),
                        pool.submit(attempt, "ref:second", "second"),
                    ]
                    outcomes, errors = [], []
                    for future in futures:
                        try:
                            outcomes.append(future.result())
                        except BaseException as error:
                            errors.append(error)
                self.assertEqual(len(outcomes), 1)
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], ConnectionRevisionConflictError)
                self.assertEqual(repository.list_connections()[0], outcomes[0])

    def test_public_records_expose_references_without_secret_fields(self) -> None:
        self.assertEqual(
            set(ConnectionRecord.__dataclass_fields__),
            {
                "connection_id",
                "provider_id",
                "revision",
                "endpoint_config_ref",
                "credential_ref",
            },
        )
        self.assertEqual(
            set(RegisteredModelRecord.__dataclass_fields__),
            {
                "registration_id",
                "provider_model_id",
                "connection_id",
                "display_name",
                "revision",
            },
        )


if __name__ == "__main__":
    unittest.main()

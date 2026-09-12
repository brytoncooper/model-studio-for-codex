import unittest
from typing import Any

from model_deck.engine.model_library.ports import (
    ModelIdempotencyConflictError,
    ModelMutationRepository,
    ModelRegistrationNotFoundError,
    ModelRevisionConflictError,
    RegisterModelCommand,
    RegisteredModelRecord,
    RemoveModelCommand,
    RenameModelCommand,
)
from model_deck.engine.model_library.use_cases import (
    RegisterModelUseCase,
    RemoveModelUseCase,
    RenameModelUseCase,
)

CONNECTION_ID = "550e8400-e29b-41d4-a716-446655440002"
REGISTRATION_ID = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"


class RecordingMutationRepository:
    def __init__(self) -> None:
        self.register_calls: list[RegisterModelCommand] = []
        self.rename_calls: list[RenameModelCommand] = []
        self.remove_calls: list[RemoveModelCommand] = []
        self.register_result = RegisteredModelRecord(
            registration_id=REGISTRATION_ID,
            provider_model_id="openrouter/test",
            connection_id=CONNECTION_ID,
            display_name="Test",
            revision=1,
        )
        self.rename_result = self.register_result
        self.remove_result = True
        self.register_error: BaseException | None = None
        self.rename_error: BaseException | None = None
        self.remove_error: BaseException | None = None

    def register(self, command: RegisterModelCommand) -> RegisteredModelRecord:
        self.register_calls.append(command)
        if self.register_error is not None:
            raise self.register_error
        return self.register_result

    def rename(self, command: RenameModelCommand) -> RegisteredModelRecord:
        self.rename_calls.append(command)
        if self.rename_error is not None:
            raise self.rename_error
        return self.rename_result

    def remove(self, command: RemoveModelCommand) -> bool:
        self.remove_calls.append(command)
        if self.remove_error is not None:
            raise self.remove_error
        return self.remove_result


def _register_params(**overrides: Any) -> dict[str, Any]:
    base = {
        "connection_id": CONNECTION_ID,
        "provider_model_id": "openrouter/test",
        "display_name": "Test",
        "expected_revision": 0,
        "idempotency_key": "reg-1",
    }
    base.update(overrides)
    return base


def _rename_params(**overrides: Any) -> dict[str, Any]:
    base = {
        "registration_id": REGISTRATION_ID,
        "display_name": "Renamed",
        "expected_revision": 1,
        "idempotency_key": "ren-1",
    }
    base.update(overrides)
    return base


def _remove_params(**overrides: Any) -> dict[str, Any]:
    base = {
        "registration_id": REGISTRATION_ID,
        "expected_revision": 1,
        "idempotency_key": "rm-1",
    }
    base.update(overrides)
    return base


class ModelLibraryMutationTests(unittest.TestCase):
    def test_register_forwards_command_and_result_shape(self) -> None:
        repo = RecordingMutationRepository()
        use_case = RegisterModelUseCase(repo)
        result = use_case.execute(_register_params())
        self.assertEqual(len(repo.register_calls), 1)
        cmd = repo.register_calls[0]
        self.assertEqual(cmd.connection_id, CONNECTION_ID)
        self.assertEqual(cmd.provider_model_id, "openrouter/test")
        self.assertEqual(cmd.display_name, "Test")
        self.assertEqual(cmd.expected_revision, 0)
        self.assertEqual(cmd.idempotency_key, "reg-1")
        self.assertEqual(
            result,
            {
                "model": {
                    "registration_id": REGISTRATION_ID,
                    "provider_model_id": "openrouter/test",
                    "connection_id": CONNECTION_ID,
                    "display_name": "Test",
                    "revision": 1,
                }
            },
        )

    def test_rename_forwards_command_and_result_shape(self) -> None:
        repo = RecordingMutationRepository()
        use_case = RenameModelUseCase(repo)
        result = use_case.execute(_rename_params())
        self.assertEqual(len(repo.rename_calls), 1)
        cmd = repo.rename_calls[0]
        self.assertEqual(cmd.registration_id, REGISTRATION_ID)
        self.assertEqual(cmd.display_name, "Renamed")
        self.assertEqual(cmd.expected_revision, 1)
        self.assertEqual(cmd.idempotency_key, "ren-1")
        self.assertIn("model", result)
        self.assertEqual(result["model"]["registration_id"], REGISTRATION_ID)

    def test_remove_forwards_command_and_result_shape(self) -> None:
        repo = RecordingMutationRepository()
        repo.remove_result = False
        use_case = RemoveModelUseCase(repo)
        result = use_case.execute(_remove_params())
        self.assertEqual(len(repo.remove_calls), 1)
        cmd = repo.remove_calls[0]
        self.assertEqual(cmd.registration_id, REGISTRATION_ID)
        self.assertEqual(cmd.expected_revision, 1)
        self.assertEqual(cmd.idempotency_key, "rm-1")
        self.assertEqual(result, {"removed": False})

    def test_validation_rejects_bool_expected_revision(self) -> None:
        repo = RecordingMutationRepository()
        use_case = RegisterModelUseCase(repo)
        with self.assertRaises(ValueError) as ctx:
            use_case.execute(_register_params(expected_revision=True))
        self.assertIn("expected_revision", str(ctx.exception))
        self.assertEqual(repo.register_calls, [])

    def test_validation_rejects_negative_revision(self) -> None:
        repo = RecordingMutationRepository()
        use_case = RemoveModelUseCase(repo)
        with self.assertRaises(ValueError):
            use_case.execute(_remove_params(expected_revision=-1))
        self.assertEqual(repo.remove_calls, [])

    def test_validation_rejects_invalid_uuid(self) -> None:
        repo = RecordingMutationRepository()
        use_case = RenameModelUseCase(repo)
        with self.assertRaises(ValueError) as ctx:
            use_case.execute(_rename_params(registration_id="not-a-uuid"))
        self.assertIn("registration_id", str(ctx.exception))
        self.assertEqual(repo.rename_calls, [])

    def test_validation_rejects_empty_idempotency_key(self) -> None:
        repo = RecordingMutationRepository()
        use_case = RegisterModelUseCase(repo)
        with self.assertRaises(ValueError):
            use_case.execute(_register_params(idempotency_key=""))
        self.assertEqual(repo.register_calls, [])

    def test_validation_rejects_empty_provider_model_id(self) -> None:
        repo = RecordingMutationRepository()
        use_case = RegisterModelUseCase(repo)
        with self.assertRaises(ValueError):
            use_case.execute(_register_params(provider_model_id=""))
        self.assertEqual(repo.register_calls, [])


    def test_validation_rejects_overlong_provider_model_id(self) -> None:
        repo = RecordingMutationRepository()
        use_case = RegisterModelUseCase(repo)
        with self.assertRaises(ValueError):
            use_case.execute(_register_params(provider_model_id="m" * 257))
        self.assertEqual(repo.register_calls, [])

    def test_validation_rejects_overlong_display_name(self) -> None:
        repo = RecordingMutationRepository()
        use_case = RenameModelUseCase(repo)
        with self.assertRaises(ValueError):
            use_case.execute(_rename_params(display_name="x" * 257))
        self.assertEqual(repo.rename_calls, [])

    def test_validation_rejects_overlong_idempotency_key(self) -> None:
        repo = RecordingMutationRepository()
        use_case = RemoveModelUseCase(repo)
        with self.assertRaises(ValueError):
            use_case.execute(_remove_params(idempotency_key="k" * 129))
        self.assertEqual(repo.remove_calls, [])

    def test_register_allows_empty_display_name(self) -> None:
        repo = RecordingMutationRepository()
        use_case = RegisterModelUseCase(repo)
        use_case.execute(_register_params(display_name=""))
        self.assertEqual(repo.register_calls[0].display_name, "")

    def test_validation_rejects_missing_display_name_on_register(self) -> None:
        repo = RecordingMutationRepository()
        use_case = RegisterModelUseCase(repo)
        params = _register_params()
        del params["display_name"]
        with self.assertRaises(ValueError) as ctx:
            use_case.execute(params)
        self.assertIn("display_name", str(ctx.exception))
        self.assertEqual(repo.register_calls, [])

    def test_validation_rejects_missing_display_name_on_rename(self) -> None:
        repo = RecordingMutationRepository()
        use_case = RenameModelUseCase(repo)
        params = _rename_params()
        del params["display_name"]
        with self.assertRaises(ValueError) as ctx:
            use_case.execute(params)
        self.assertIn("display_name", str(ctx.exception))
        self.assertEqual(repo.rename_calls, [])


    def test_register_rejects_non_hyphenated_uuid_hex(self) -> None:
        repo = RecordingMutationRepository()
        use_case = RegisterModelUseCase(repo)
        with self.assertRaises(ValueError) as ctx:
            use_case.execute(
                _register_params(connection_id="550e8400e29b41d4a716446655440002")
            )
        self.assertIn("connection_id", str(ctx.exception))
        self.assertEqual(repo.register_calls, [])

    def test_rename_rejects_urn_uuid(self) -> None:
        repo = RecordingMutationRepository()
        use_case = RenameModelUseCase(repo)
        with self.assertRaises(ValueError) as ctx:
            use_case.execute(
                _rename_params(
                    registration_id="urn:uuid:6ba7b810-9dad-11d1-80b4-00c04fd430c8"
                )
            )
        self.assertIn("registration_id", str(ctx.exception))
        self.assertEqual(repo.rename_calls, [])

    def test_remove_rejects_braced_uuid(self) -> None:
        repo = RecordingMutationRepository()
        use_case = RemoveModelUseCase(repo)
        with self.assertRaises(ValueError) as ctx:
            use_case.execute(
                _remove_params(
                    registration_id="{6ba7b810-9dad-11d1-80b4-00c04fd430c8}"
                )
            )
        self.assertIn("registration_id", str(ctx.exception))
        self.assertEqual(repo.remove_calls, [])

    def test_register_accepts_uppercase_uuid_unchanged(self) -> None:
        repo = RecordingMutationRepository()
        use_case = RegisterModelUseCase(repo)
        upper_connection = CONNECTION_ID.upper()
        use_case.execute(_register_params(connection_id=upper_connection))
        self.assertEqual(repo.register_calls[0].connection_id, upper_connection)

    def test_repository_revision_conflict_propagates(self) -> None:
        repo = RecordingMutationRepository()
        repo.register_error = ModelRevisionConflictError("stale revision")
        use_case = RegisterModelUseCase(repo)
        with self.assertRaises(ModelRevisionConflictError) as ctx:
            use_case.execute(_register_params())
        self.assertEqual(str(ctx.exception), "stale revision")

    def test_repository_idempotency_conflict_propagates(self) -> None:
        repo = RecordingMutationRepository()
        repo.rename_error = ModelIdempotencyConflictError("payload mismatch")
        use_case = RenameModelUseCase(repo)
        with self.assertRaises(ModelIdempotencyConflictError):
            use_case.execute(_rename_params())

    def test_repository_not_found_propagates(self) -> None:
        repo = RecordingMutationRepository()
        repo.remove_error = ModelRegistrationNotFoundError("missing")
        use_case = RemoveModelUseCase(repo)
        with self.assertRaises(ModelRegistrationNotFoundError):
            use_case.execute(_remove_params())

    def test_mutation_repository_is_runtime_checkable(self) -> None:
        repo = RecordingMutationRepository()
        self.assertIsInstance(repo, ModelMutationRepository)


if __name__ == "__main__":
    unittest.main()

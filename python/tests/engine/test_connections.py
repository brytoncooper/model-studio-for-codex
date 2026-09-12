import unittest
from typing import Any

from model_deck.engine.connections.ports import (
    ConnectionIdempotencyConflictError,
    ConnectionRecord,
    ConnectionRepository,
    ConnectionRevisionConflictError,
    SaveConnectionCommand,
)
from model_deck.engine.connections.use_cases import (
    ListConnectionsUseCase,
    SaveConnectionUseCase,
)

CONNECTION_ID = "550e8400-e29b-41d4-a716-446655440002"
PROVIDER_ID = "com.example.provider"


class RecordingConnectionRepository:
    def __init__(self) -> None:
        self.list_calls = 0
        self.save_calls: list[SaveConnectionCommand] = []
        self.list_result: list[ConnectionRecord] = [
            ConnectionRecord(
                connection_id=CONNECTION_ID,
                provider_id=PROVIDER_ID,
                revision=1,
                credential_ref="550e8400-e29b-41d4-a716-446655440010",
            )
        ]
        self.save_result = ConnectionRecord(
            connection_id=CONNECTION_ID,
            provider_id=PROVIDER_ID,
            revision=1,
            endpoint_config_ref="ref:endpoint.config",
        )
        self.save_error: BaseException | None = None

    def list_connections(self) -> list[ConnectionRecord]:
        self.list_calls += 1
        return list(self.list_result)

    def save(self, command: SaveConnectionCommand) -> ConnectionRecord:
        self.save_calls.append(command)
        if self.save_error is not None:
            raise self.save_error
        return self.save_result


def _save_params(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "connection": {
            "connection_id": CONNECTION_ID,
            "provider_id": PROVIDER_ID,
        },
        "expected_revision": 0,
        "idempotency_key": "conn-save-1",
    }
    base.update(overrides)
    if "connection" in overrides:
        base["connection"] = overrides["connection"]
    return base


class ConnectionUseCaseTests(unittest.TestCase):
    def test_list_forwards_repository_and_result_shape(self) -> None:
        repo = RecordingConnectionRepository()
        use_case = ListConnectionsUseCase(repo)
        result = use_case.execute({})
        self.assertEqual(repo.list_calls, 1)
        self.assertEqual(
            result,
            {
                "connections": [
                    {
                        "connection_id": CONNECTION_ID,
                        "provider_id": PROVIDER_ID,
                        "revision": 1,
                        "credential_ref": "550e8400-e29b-41d4-a716-446655440010",
                    }
                ]
            },
        )

    def test_list_omits_missing_optional_refs(self) -> None:
        repo = RecordingConnectionRepository()
        repo.list_result = [
            ConnectionRecord(
                connection_id=CONNECTION_ID,
                provider_id=PROVIDER_ID,
                revision=2,
            )
        ]
        use_case = ListConnectionsUseCase(repo)
        result = use_case.execute(None)
        item = result["connections"][0]
        self.assertNotIn("endpoint_config_ref", item)
        self.assertNotIn("credential_ref", item)

    def test_list_rejects_params(self) -> None:
        repo = RecordingConnectionRepository()
        use_case = ListConnectionsUseCase(repo)
        with self.assertRaises(ValueError) as ctx:
            use_case.execute({"filter": "x"})
        self.assertIn("unknown params field", str(ctx.exception))
        self.assertEqual(repo.list_calls, 0)

    def test_save_forwards_command_and_result_shape(self) -> None:
        repo = RecordingConnectionRepository()
        use_case = SaveConnectionUseCase(repo)
        params = _save_params(
            connection={
                "connection_id": CONNECTION_ID,
                "provider_id": PROVIDER_ID,
                "endpoint_config_ref": "ref:endpoint.config",
                "credential_ref": "550e8400-e29b-41d4-a716-446655440010",
            }
        )
        result = use_case.execute(params)
        self.assertEqual(len(repo.save_calls), 1)
        cmd = repo.save_calls[0]
        self.assertEqual(cmd.connection_id, CONNECTION_ID)
        self.assertEqual(cmd.provider_id, PROVIDER_ID)
        self.assertEqual(cmd.expected_revision, 0)
        self.assertEqual(cmd.idempotency_key, "conn-save-1")
        self.assertEqual(cmd.endpoint_config_ref, "ref:endpoint.config")
        self.assertEqual(cmd.credential_ref, "550e8400-e29b-41d4-a716-446655440010")
        self.assertEqual(
            result,
            {
                "connection": {
                    "connection_id": CONNECTION_ID,
                    "provider_id": PROVIDER_ID,
                    "revision": 1,
                    "endpoint_config_ref": "ref:endpoint.config",
                }
            },
        )

    def test_save_rejects_unknown_top_level_fields(self) -> None:
        repo = RecordingConnectionRepository()
        use_case = SaveConnectionUseCase(repo)
        with self.assertRaises(ValueError) as ctx:
            use_case.execute(_save_params(extra="nope"))
        self.assertIn("unknown params field", str(ctx.exception))
        self.assertEqual(repo.save_calls, [])

    def test_save_rejects_plaintext_secret_top_level_field(self) -> None:
        repo = RecordingConnectionRepository()
        use_case = SaveConnectionUseCase(repo)
        with self.assertRaises(ValueError) as ctx:
            use_case.execute(_save_params(secret="sk-live-secret"))
        self.assertIn("unknown params field", str(ctx.exception))
        self.assertEqual(repo.save_calls, [])

    def test_save_rejects_unknown_nested_fields(self) -> None:
        repo = RecordingConnectionRepository()
        use_case = SaveConnectionUseCase(repo)
        with self.assertRaises(ValueError) as ctx:
            use_case.execute(
                _save_params(
                    connection={
                        "connection_id": CONNECTION_ID,
                        "provider_id": PROVIDER_ID,
                        "revision": 3,
                    }
                )
            )
        self.assertIn("unknown connection field", str(ctx.exception))
        self.assertEqual(repo.save_calls, [])

    def test_save_rejects_nested_secret_like_fields(self) -> None:
        repo = RecordingConnectionRepository()
        use_case = SaveConnectionUseCase(repo)
        with self.assertRaises(ValueError) as ctx:
            use_case.execute(
                _save_params(
                    connection={
                        "connection_id": CONNECTION_ID,
                        "provider_id": PROVIDER_ID,
                        "api_key": "sk-live-secret",
                    }
                )
            )
        self.assertIn("unknown connection field", str(ctx.exception))
        self.assertEqual(repo.save_calls, [])

    def test_save_rejects_invalid_credential_ref(self) -> None:
        repo = RecordingConnectionRepository()
        use_case = SaveConnectionUseCase(repo)
        with self.assertRaises(ValueError) as ctx:
            use_case.execute(
                _save_params(
                    connection={
                        "connection_id": CONNECTION_ID,
                        "provider_id": PROVIDER_ID,
                        "credential_ref": "sk-live-secret-not-opaque-ref",
                    }
                )
            )
        self.assertIn("credential_ref", str(ctx.exception))
        self.assertEqual(repo.save_calls, [])

    def test_validation_rejects_missing_required_connection_fields(self) -> None:
        repo = RecordingConnectionRepository()
        use_case = SaveConnectionUseCase(repo)
        with self.assertRaises(ValueError):
            use_case.execute(
                _save_params(connection={"provider_id": PROVIDER_ID})
            )
        with self.assertRaises(ValueError):
            use_case.execute(
                _save_params(connection={"connection_id": CONNECTION_ID})
            )
        self.assertEqual(repo.save_calls, [])

    def test_validation_rejects_missing_top_level_save_fields(self) -> None:
        repo = RecordingConnectionRepository()
        use_case = SaveConnectionUseCase(repo)
        params = _save_params()
        del params["expected_revision"]
        with self.assertRaises(ValueError):
            use_case.execute(params)
        params = _save_params()
        del params["idempotency_key"]
        with self.assertRaises(ValueError):
            use_case.execute(params)
        self.assertEqual(repo.save_calls, [])

    def test_validation_rejects_bool_expected_revision(self) -> None:
        repo = RecordingConnectionRepository()
        use_case = SaveConnectionUseCase(repo)
        with self.assertRaises(ValueError) as ctx:
            use_case.execute(_save_params(expected_revision=True))
        self.assertIn("expected_revision", str(ctx.exception))
        self.assertEqual(repo.save_calls, [])

    def test_validation_rejects_negative_revision(self) -> None:
        repo = RecordingConnectionRepository()
        use_case = SaveConnectionUseCase(repo)
        with self.assertRaises(ValueError):
            use_case.execute(_save_params(expected_revision=-1))
        self.assertEqual(repo.save_calls, [])

    def test_validation_rejects_invalid_uuid(self) -> None:
        repo = RecordingConnectionRepository()
        use_case = SaveConnectionUseCase(repo)
        with self.assertRaises(ValueError) as ctx:
            use_case.execute(
                _save_params(
                    connection={
                        "connection_id": "not-a-uuid",
                        "provider_id": PROVIDER_ID,
                    }
                )
            )
        self.assertIn("connection_id", str(ctx.exception))
        self.assertEqual(repo.save_calls, [])

    def test_validation_rejects_empty_idempotency_key(self) -> None:
        repo = RecordingConnectionRepository()
        use_case = SaveConnectionUseCase(repo)
        with self.assertRaises(ValueError):
            use_case.execute(_save_params(idempotency_key=""))
        self.assertEqual(repo.save_calls, [])

    def test_validation_rejects_overlong_idempotency_key(self) -> None:
        repo = RecordingConnectionRepository()
        use_case = SaveConnectionUseCase(repo)
        with self.assertRaises(ValueError):
            use_case.execute(_save_params(idempotency_key="k" * 129))
        self.assertEqual(repo.save_calls, [])

    def test_validation_rejects_invalid_provider_id(self) -> None:
        repo = RecordingConnectionRepository()
        use_case = SaveConnectionUseCase(repo)
        with self.assertRaises(ValueError) as ctx:
            use_case.execute(
                _save_params(
                    connection={
                        "connection_id": CONNECTION_ID,
                        "provider_id": "Com.Example.Provider",
                    }
                )
            )
        self.assertIn("provider_id", str(ctx.exception))
        self.assertEqual(repo.save_calls, [])

    def test_validation_rejects_short_provider_id(self) -> None:
        repo = RecordingConnectionRepository()
        use_case = SaveConnectionUseCase(repo)
        with self.assertRaises(ValueError):
            use_case.execute(
                _save_params(
                    connection={
                        "connection_id": CONNECTION_ID,
                        "provider_id": "ab",
                    }
                )
            )
        self.assertEqual(repo.save_calls, [])

    def test_save_accepts_minimum_length_provider_id(self) -> None:
        repo = RecordingConnectionRepository()
        use_case = SaveConnectionUseCase(repo)
        use_case.execute(
            _save_params(
                connection={
                    "connection_id": CONNECTION_ID,
                    "provider_id": "a.b",
                }
            )
        )
        self.assertEqual(repo.save_calls[0].provider_id, "a.b")


    def test_validation_rejects_noncanonical_uuid_connection_id(self) -> None:
        repo = RecordingConnectionRepository()
        use_case = SaveConnectionUseCase(repo)
        hex_id = CONNECTION_ID.replace("-", "")
        urn_id = f"urn:uuid:{CONNECTION_ID}"
        braced_id = f"{{{CONNECTION_ID}}}"
        for bad_id in (hex_id, urn_id, braced_id):
            with self.assertRaises(ValueError) as ctx:
                use_case.execute(
                    _save_params(
                        connection={
                            "connection_id": bad_id,
                            "provider_id": PROVIDER_ID,
                        }
                    )
                )
            self.assertIn("connection_id", str(ctx.exception))
        self.assertEqual(repo.save_calls, [])

    def test_validation_rejects_noncanonical_uuid_opaque_refs(self) -> None:
        repo = RecordingConnectionRepository()
        use_case = SaveConnectionUseCase(repo)
        ref_uuid = "550e8400-e29b-41d4-a716-446655440010"
        hex_ref = ref_uuid.replace("-", "")
        urn_ref = f"urn:uuid:{ref_uuid}"
        braced_ref = f"{{{ref_uuid}}}"
        for field, bad_ref in (
            ("endpoint_config_ref", hex_ref),
            ("endpoint_config_ref", urn_ref),
            ("endpoint_config_ref", braced_ref),
            ("credential_ref", hex_ref),
            ("credential_ref", urn_ref),
            ("credential_ref", braced_ref),
        ):
            with self.assertRaises(ValueError) as ctx:
                use_case.execute(
                    _save_params(
                        connection={
                            "connection_id": CONNECTION_ID,
                            "provider_id": PROVIDER_ID,
                            field: bad_ref,
                        }
                    )
                )
            self.assertIn(field, str(ctx.exception))
        self.assertEqual(repo.save_calls, [])

    def test_save_accepts_uppercase_uuid_unchanged(self) -> None:
        repo = RecordingConnectionRepository()
        use_case = SaveConnectionUseCase(repo)
        upper = CONNECTION_ID.upper()
        use_case.execute(
            _save_params(
                connection={
                    "connection_id": upper,
                    "provider_id": PROVIDER_ID,
                }
            )
        )
        self.assertEqual(repo.save_calls[0].connection_id, upper)

    def test_repository_revision_conflict_propagates(self) -> None:
        repo = RecordingConnectionRepository()
        repo.save_error = ConnectionRevisionConflictError("stale revision")
        use_case = SaveConnectionUseCase(repo)
        with self.assertRaises(ConnectionRevisionConflictError) as ctx:
            use_case.execute(_save_params())
        self.assertEqual(str(ctx.exception), "stale revision")

    def test_repository_idempotency_conflict_propagates(self) -> None:
        repo = RecordingConnectionRepository()
        repo.save_error = ConnectionIdempotencyConflictError("payload mismatch")
        use_case = SaveConnectionUseCase(repo)
        with self.assertRaises(ConnectionIdempotencyConflictError):
            use_case.execute(_save_params())

    def test_connection_repository_is_runtime_checkable(self) -> None:
        repo = RecordingConnectionRepository()
        self.assertIsInstance(repo, ConnectionRepository)


if __name__ == "__main__":
    unittest.main()

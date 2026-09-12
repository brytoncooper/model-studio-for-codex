import dataclasses
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from model_deck.adapters.routing.registered import (
    ProviderRouteDefinition,
    RegisteredRouteConnectionNotFoundError,
    RegisteredRouteProviderDefinitionNotFoundError,
    RegisteredRouteResolver,
)
from model_deck.adapters.storage.sqlite_connection_repository import SQLiteConnectionRepository
from model_deck.adapters.storage.sqlite_model_repository import SQLiteModelRepository
from model_deck.engine.connections.ports import (
    ConnectionRecord,
    ConnectionRepository,
    SaveConnectionCommand,
)
from model_deck.engine.model_library.ports import (
    ModelRepository,
    RegisterModelCommand,
    RegisteredModelRecord,
    RemoveModelCommand,
)
from model_deck.engine.routing.ports import (
    CapabilityFeature,
    CapabilityTriState,
    ExecutionMode,
    RegistrationNotFoundError,
    RouteResolveRequest,
    RouteResolver,
    RouteSnapshot,
    UnknownCapabilityError,
    UnsupportedCapabilityError,
)

CONNECTION_ID = "550e8400-e29b-41d4-a716-446655440002"
REGISTRATION_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
PROVIDER_ID = "com.example.provider"
ENDPOINT_REF = "ref:endpoint.config"
CREDENTIAL_REF = "ref:credential.token"
CAPABILITY_REF = "ref:cap.snapshot"


def _route_definition(**overrides: Any) -> ProviderRouteDefinition:
    base = {
        "execution_mode": ExecutionMode.RESPONSES,
        "capability_features": (
            CapabilityFeature("tools", CapabilityTriState.SUPPORTED),
            CapabilityFeature("vision", CapabilityTriState.UNKNOWN),
        ),
        "capability_snapshot_ref": CAPABILITY_REF,
    }
    base.update(overrides)
    return ProviderRouteDefinition(**base)


class FakeModelRepository:
    def __init__(self, records: list[RegisteredModelRecord]) -> None:
        self._records = list(records)

    def list_registered(self, *, connection_id: str | None = None) -> list[RegisteredModelRecord]:
        if connection_id is None:
            return list(self._records)
        return [row for row in self._records if row.connection_id == connection_id]


class FakeConnectionRepository:
    def __init__(self, records: list[ConnectionRecord]) -> None:
        self._records = list(records)

    def list_connections(self) -> list[ConnectionRecord]:
        return list(self._records)

    def save(self, command: SaveConnectionCommand) -> ConnectionRecord:
        raise NotImplementedError


def _resolver(
    *,
    models: list[RegisteredModelRecord],
    connections: list[ConnectionRecord],
    definitions: dict[str, ProviderRouteDefinition] | None = None,
) -> RegisteredRouteResolver:
    return RegisteredRouteResolver(
        model_repository=FakeModelRepository(models),
        connection_repository=FakeConnectionRepository(connections),
        provider_route_definitions=definitions
        if definitions is not None
        else {PROVIDER_ID: _route_definition()},
    )


class RegisteredRouteResolverTests(unittest.TestCase):
    def test_implements_route_resolver(self) -> None:
        resolver = _resolver(
            models=[
                RegisteredModelRecord(
                    registration_id=REGISTRATION_ID,
                    provider_model_id="vendor/model-a",
                    connection_id=CONNECTION_ID,
                    display_name="Model A",
                    revision=2,
                )
            ],
            connections=[
                ConnectionRecord(
                    connection_id=CONNECTION_ID,
                    provider_id=PROVIDER_ID,
                    revision=4,
                    endpoint_config_ref=ENDPOINT_REF,
                    credential_ref=CREDENTIAL_REF,
                )
            ],
        )
        self.assertIsInstance(resolver, RouteResolver)

    def test_resolve_builds_snapshot_from_registration_connection_and_definition(self) -> None:
        resolver = _resolver(
            models=[
                RegisteredModelRecord(
                    registration_id=REGISTRATION_ID,
                    provider_model_id="vendor/model-a",
                    connection_id=CONNECTION_ID,
                    display_name="Model A",
                    revision=2,
                )
            ],
            connections=[
                ConnectionRecord(
                    connection_id=CONNECTION_ID,
                    provider_id=PROVIDER_ID,
                    revision=4,
                    endpoint_config_ref=ENDPOINT_REF,
                    credential_ref=CREDENTIAL_REF,
                )
            ],
            definitions={PROVIDER_ID: _route_definition(execution_mode=ExecutionMode.CUSTOM)},
        )
        snapshot = resolver.resolve_active_registration(
            RouteResolveRequest(registration_id=REGISTRATION_ID)
        )
        self.assertEqual(
            snapshot,
            RouteSnapshot(
                registration_id=REGISTRATION_ID,
                registration_revision=2,
                connection_id=CONNECTION_ID,
                connection_revision=4,
                provider_id=PROVIDER_ID,
                provider_model_id="vendor/model-a",
                execution_mode=ExecutionMode.CUSTOM,
                endpoint_config_ref=ENDPOINT_REF,
                credential_ref=CREDENTIAL_REF,
                capability_snapshot_ref=CAPABILITY_REF,
                capability_features=(
                    CapabilityFeature("tools", CapabilityTriState.SUPPORTED),
                    CapabilityFeature("vision", CapabilityTriState.UNKNOWN),
                ),
            ),
        )

    def test_missing_active_registration_raises_not_found(self) -> None:
        resolver = _resolver(models=[], connections=[])
        with self.assertRaises(RegistrationNotFoundError):
            resolver.resolve_active_registration(
                RouteResolveRequest(registration_id=REGISTRATION_ID)
            )

    def test_missing_connection_raises_adapter_error(self) -> None:
        resolver = _resolver(
            models=[
                RegisteredModelRecord(
                    registration_id=REGISTRATION_ID,
                    provider_model_id="vendor/model-a",
                    connection_id=CONNECTION_ID,
                    display_name="Model A",
                    revision=1,
                )
            ],
            connections=[],
        )
        with self.assertRaises(RegisteredRouteConnectionNotFoundError):
            resolver.resolve_active_registration(
                RouteResolveRequest(registration_id=REGISTRATION_ID)
            )

    def test_missing_provider_definition_raises_adapter_error(self) -> None:
        resolver = _resolver(
            models=[
                RegisteredModelRecord(
                    registration_id=REGISTRATION_ID,
                    provider_model_id="vendor/model-a",
                    connection_id=CONNECTION_ID,
                    display_name="Model A",
                    revision=1,
                )
            ],
            connections=[
                ConnectionRecord(
                    connection_id=CONNECTION_ID,
                    provider_id=PROVIDER_ID,
                    revision=1,
                )
            ],
            definitions={},
        )
        with self.assertRaises(RegisteredRouteProviderDefinitionNotFoundError):
            resolver.resolve_active_registration(
                RouteResolveRequest(registration_id=REGISTRATION_ID)
            )

    def test_capability_snapshot_ref_mismatch_raises_unknown(self) -> None:
        resolver = _resolver(
            models=[
                RegisteredModelRecord(
                    registration_id=REGISTRATION_ID,
                    provider_model_id="vendor/model-a",
                    connection_id=CONNECTION_ID,
                    display_name="Model A",
                    revision=1,
                )
            ],
            connections=[
                ConnectionRecord(
                    connection_id=CONNECTION_ID,
                    provider_id=PROVIDER_ID,
                    revision=1,
                )
            ],
        )
        with self.assertRaises(UnknownCapabilityError):
            resolver.resolve_active_registration(
                RouteResolveRequest(
                    registration_id=REGISTRATION_ID,
                    capability_snapshot_ref="ref:other.snapshot",
                )
            )

    def test_required_supported_unknown_capability_raises_unknown(self) -> None:
        resolver = _resolver(
            models=[
                RegisteredModelRecord(
                    registration_id=REGISTRATION_ID,
                    provider_model_id="vendor/model-a",
                    connection_id=CONNECTION_ID,
                    display_name="Model A",
                    revision=1,
                )
            ],
            connections=[
                ConnectionRecord(
                    connection_id=CONNECTION_ID,
                    provider_id=PROVIDER_ID,
                    revision=1,
                )
            ],
        )
        with self.assertRaises(UnknownCapabilityError):
            resolver.resolve_active_registration(
                RouteResolveRequest(
                    registration_id=REGISTRATION_ID,
                    capability_requirements=(
                        CapabilityFeature("vision", CapabilityTriState.SUPPORTED),
                    ),
                )
            )

    def test_required_supported_missing_capability_raises_unknown(self) -> None:
        resolver = _resolver(
            models=[
                RegisteredModelRecord(
                    registration_id=REGISTRATION_ID,
                    provider_model_id="vendor/model-a",
                    connection_id=CONNECTION_ID,
                    display_name="Model A",
                    revision=1,
                )
            ],
            connections=[
                ConnectionRecord(
                    connection_id=CONNECTION_ID,
                    provider_id=PROVIDER_ID,
                    revision=1,
                )
            ],
        )
        with self.assertRaises(UnknownCapabilityError):
            resolver.resolve_active_registration(
                RouteResolveRequest(
                    registration_id=REGISTRATION_ID,
                    capability_requirements=(
                        CapabilityFeature("streaming", CapabilityTriState.SUPPORTED),
                    ),
                )
            )

    def test_required_supported_unsupported_capability_raises_unsupported(self) -> None:
        resolver = _resolver(
            models=[
                RegisteredModelRecord(
                    registration_id=REGISTRATION_ID,
                    provider_model_id="vendor/model-a",
                    connection_id=CONNECTION_ID,
                    display_name="Model A",
                    revision=1,
                )
            ],
            connections=[
                ConnectionRecord(
                    connection_id=CONNECTION_ID,
                    provider_id=PROVIDER_ID,
                    revision=1,
                )
            ],
            definitions={
                PROVIDER_ID: _route_definition(
                    capability_features=(
                        CapabilityFeature("tools", CapabilityTriState.UNSUPPORTED),
                    )
                )
            },
        )
        with self.assertRaises(UnsupportedCapabilityError):
            resolver.resolve_active_registration(
                RouteResolveRequest(
                    registration_id=REGISTRATION_ID,
                    capability_requirements=(
                        CapabilityFeature("tools", CapabilityTriState.SUPPORTED),
                    ),
                )
            )

    def test_unknown_requirement_does_not_upgrade_snapshot_state(self) -> None:
        resolver = _resolver(
            models=[
                RegisteredModelRecord(
                    registration_id=REGISTRATION_ID,
                    provider_model_id="vendor/model-a",
                    connection_id=CONNECTION_ID,
                    display_name="Model A",
                    revision=1,
                )
            ],
            connections=[
                ConnectionRecord(
                    connection_id=CONNECTION_ID,
                    provider_id=PROVIDER_ID,
                    revision=1,
                )
            ],
        )
        snapshot = resolver.resolve_active_registration(
            RouteResolveRequest(
                registration_id=REGISTRATION_ID,
                capability_requirements=(
                    CapabilityFeature("vision", CapabilityTriState.UNKNOWN),
                ),
            )
        )
        vision = next(
            feature
            for feature in snapshot.capability_features or ()
            if feature.name == "vision"
        )
        self.assertEqual(vision.state, CapabilityTriState.UNKNOWN)

    def test_route_snapshot_is_immutable(self) -> None:
        resolver = _resolver(
            models=[
                RegisteredModelRecord(
                    registration_id=REGISTRATION_ID,
                    provider_model_id="vendor/model-a",
                    connection_id=CONNECTION_ID,
                    display_name="Model A",
                    revision=1,
                )
            ],
            connections=[
                ConnectionRecord(
                    connection_id=CONNECTION_ID,
                    provider_id=PROVIDER_ID,
                    revision=1,
                )
            ],
        )
        snapshot = resolver.resolve_active_registration(
            RouteResolveRequest(registration_id=REGISTRATION_ID)
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snapshot.provider_id = "other"  # type: ignore[misc]


class RegisteredRouteResolverSQLiteIntegrationTests(unittest.TestCase):
    def test_remove_hides_registration_but_retains_captured_snapshot(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            connections = SQLiteConnectionRepository(db_path)
            models = SQLiteModelRepository(db_path, id_factory=lambda: REGISTRATION_ID)
            connections.save(
                SaveConnectionCommand(
                    connection_id=CONNECTION_ID,
                    provider_id=PROVIDER_ID,
                    expected_revision=0,
                    idempotency_key="conn-1",
                    endpoint_config_ref=ENDPOINT_REF,
                    credential_ref=CREDENTIAL_REF,
                )
            )
            created = models.register(
                RegisterModelCommand(
                    connection_id=CONNECTION_ID,
                    provider_model_id="vendor/model-a",
                    display_name="Model A",
                    expected_revision=0,
                    idempotency_key="reg-1",
                )
            )
            resolver = RegisteredRouteResolver(
                model_repository=models,
                connection_repository=connections,
                provider_route_definitions={PROVIDER_ID: _route_definition()},
            )
            captured = resolver.resolve_active_registration(
                RouteResolveRequest(registration_id=created.registration_id)
            )
            models.remove(
                RemoveModelCommand(
                    registration_id=created.registration_id,
                    expected_revision=created.revision,
                    idempotency_key="rm-1",
                )
            )
            with self.assertRaises(RegistrationNotFoundError):
                resolver.resolve_active_registration(
                    RouteResolveRequest(registration_id=created.registration_id)
                )
            self.assertEqual(captured.registration_revision, created.revision)
            self.assertEqual(captured.connection_revision, 1)
            self.assertEqual(captured.endpoint_config_ref, ENDPOINT_REF)
            self.assertEqual(captured.credential_ref, CREDENTIAL_REF)
            self.assertEqual(models.list_registered(), [])
            self.assertEqual(len(connections.list_connections()), 1)

    def test_connection_revision_and_refs_are_captured_on_resolve(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            connections = SQLiteConnectionRepository(db_path)
            models = SQLiteModelRepository(db_path, id_factory=lambda: REGISTRATION_ID)
            connections.save(
                SaveConnectionCommand(
                    connection_id=CONNECTION_ID,
                    provider_id=PROVIDER_ID,
                    expected_revision=0,
                    idempotency_key="conn-1",
                )
            )
            updated = connections.save(
                SaveConnectionCommand(
                    connection_id=CONNECTION_ID,
                    provider_id=PROVIDER_ID,
                    expected_revision=1,
                    idempotency_key="conn-2",
                    endpoint_config_ref=ENDPOINT_REF,
                    credential_ref=CREDENTIAL_REF,
                )
            )
            created = models.register(
                RegisterModelCommand(
                    connection_id=CONNECTION_ID,
                    provider_model_id="vendor/model-a",
                    display_name="Model A",
                    expected_revision=0,
                    idempotency_key="reg-1",
                )
            )
            resolver = RegisteredRouteResolver(
                model_repository=models,
                connection_repository=connections,
                provider_route_definitions={PROVIDER_ID: _route_definition()},
            )
            snapshot = resolver.resolve_active_registration(
                RouteResolveRequest(registration_id=created.registration_id)
            )
            self.assertEqual(snapshot.connection_revision, updated.revision)
            self.assertEqual(snapshot.endpoint_config_ref, ENDPOINT_REF)
            self.assertEqual(snapshot.credential_ref, CREDENTIAL_REF)
            self.assertIsInstance(models, ModelRepository)
            self.assertIsInstance(connections, ConnectionRepository)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from model_deck.engine.connections.ports import ConnectionRecord, ConnectionRepository
from model_deck.engine.model_library.ports import ModelRepository, RegisteredModelRecord
from model_deck.engine.routing.ports import (
    CapabilityFeature,
    CapabilityFeatureTuple,
    CapabilityTriState,
    ExecutionMode,
    RegistrationNotFoundError,
    RouteResolveRequest,
    RouteSnapshot,
    UnknownCapabilityError,
    UnsupportedCapabilityError,
)


class RegisteredRouteConnectionNotFoundError(LookupError):
    pass


class RegisteredRouteProviderDefinitionNotFoundError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderRouteDefinition:
    execution_mode: ExecutionMode
    capability_features: CapabilityFeatureTuple
    capability_snapshot_ref: str | None = None


class RegisteredRouteResolver:
    def __init__(
        self,
        *,
        model_repository: ModelRepository,
        connection_repository: ConnectionRepository,
        provider_route_definitions: Mapping[str, ProviderRouteDefinition],
    ) -> None:
        self._model_repository = model_repository
        self._connection_repository = connection_repository
        self._provider_route_definitions = MappingProxyType(dict(provider_route_definitions))

    def resolve_active_registration(self, request: RouteResolveRequest) -> RouteSnapshot:
        registration = _find_active_registration(
            self._model_repository,
            request.registration_id,
        )
        connection = _find_connection(
            self._connection_repository,
            registration.connection_id,
        )
        route_definition = _find_provider_route_definition(
            self._provider_route_definitions,
            connection.provider_id,
        )
        _validate_capability_constraints(
            request,
            route_definition.capability_snapshot_ref,
            route_definition.capability_features,
        )
        return RouteSnapshot(
            registration_id=registration.registration_id,
            registration_revision=registration.revision,
            connection_id=connection.connection_id,
            connection_revision=connection.revision,
            provider_id=connection.provider_id,
            provider_model_id=registration.provider_model_id,
            execution_mode=route_definition.execution_mode,
            endpoint_config_ref=connection.endpoint_config_ref,
            credential_ref=connection.credential_ref,
            capability_snapshot_ref=route_definition.capability_snapshot_ref,
            capability_features=route_definition.capability_features,
        )


def _find_active_registration(
    repository: ModelRepository,
    registration_id: str,
) -> RegisteredModelRecord:
    for record in repository.list_registered():
        if record.registration_id == registration_id:
            return record
    raise RegistrationNotFoundError(f"active registration not found: {registration_id}")


def _find_connection(
    repository: ConnectionRepository,
    connection_id: str,
) -> ConnectionRecord:
    for record in repository.list_connections():
        if record.connection_id == connection_id:
            return record
    raise RegisteredRouteConnectionNotFoundError(
        f"connection not found for registration: {connection_id}"
    )


def _find_provider_route_definition(
    provider_route_definitions: Mapping[str, ProviderRouteDefinition],
    provider_id: str,
) -> ProviderRouteDefinition:
    try:
        return provider_route_definitions[provider_id]
    except KeyError as exc:
        raise RegisteredRouteProviderDefinitionNotFoundError(
            f"provider route definition not configured: {provider_id}"
        ) from exc


def _capability_states(
    features: CapabilityFeatureTuple | None,
) -> dict[str, CapabilityTriState]:
    if features is None:
        return {}
    return {feature.name: feature.state for feature in features}


def _validate_capability_constraints(
    request: RouteResolveRequest,
    resolved_snapshot_ref: str | None,
    resolved_features: CapabilityFeatureTuple | None,
) -> None:
    if request.capability_snapshot_ref is not None:
        if request.capability_snapshot_ref != resolved_snapshot_ref:
            raise UnknownCapabilityError("capability snapshot ref mismatch")

    if not request.capability_requirements:
        return

    resolved_states = _capability_states(resolved_features)
    for requirement in request.capability_requirements:
        if requirement.state != CapabilityTriState.SUPPORTED:
            continue
        resolved_state = resolved_states.get(requirement.name)
        if resolved_state is None or resolved_state == CapabilityTriState.UNKNOWN:
            raise UnknownCapabilityError(
                f"required capability is unknown: {requirement.name}"
            )
        if resolved_state == CapabilityTriState.UNSUPPORTED:
            raise UnsupportedCapabilityError(
                f"required capability is unsupported: {requirement.name}"
            )

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class ExecutionMode(str, Enum):
    CHAT_COMPLETIONS = "chat_completions"
    RESPONSES = "responses"
    CUSTOM = "custom"


class CapabilityTriState(str, Enum):
    UNKNOWN = "unknown"
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class CapabilityFeature:
    name: str
    state: CapabilityTriState


CapabilityFeatureTuple = tuple[CapabilityFeature, ...]


@dataclass(frozen=True, slots=True)
class ContinuationScope:
    connection_id: str
    provider_model_id: str
    provider_id: str
    execution_mode: ExecutionMode
    handle: str


@dataclass(frozen=True, slots=True)
class RouteSnapshot:
    registration_id: str
    registration_revision: int
    connection_id: str
    connection_revision: int
    provider_id: str
    provider_model_id: str
    execution_mode: ExecutionMode
    endpoint_config_ref: str | None = None
    credential_ref: str | None = None
    capability_snapshot_ref: str | None = None
    capability_features: CapabilityFeatureTuple | None = None


@dataclass(frozen=True, slots=True)
class RouteResolveRequest:
    registration_id: str
    capability_snapshot_ref: str | None = None
    capability_requirements: CapabilityFeatureTuple | None = None


@runtime_checkable
class RouteResolver(Protocol):
    def resolve_active_registration(self, request: RouteResolveRequest) -> RouteSnapshot: ...


class RegistrationNotFoundError(LookupError):
    pass


class RegistrationRemovedError(LookupError):
    pass


class UnknownCapabilityError(ValueError):
    pass


class UnsupportedCapabilityError(ValueError):
    pass

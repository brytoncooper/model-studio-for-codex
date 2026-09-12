"""Supervisor-owned identities and trusted authority lookup contracts."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ActivationIdentity:
    engine_instance_id: str
    audience: str
    activation_id: str
    plugin_id: str
    plugin_version: str


@dataclass(frozen=True, slots=True)
class ActivationState:
    identity: ActivationIdentity
    effects: frozenset[str]
    resource_scopes: frozenset[str]
    capability_grants: frozenset[str]
    expires_at: datetime
    revocation_generation: int
    active: bool = True


@dataclass(frozen=True, slots=True)
class OriginState:
    principal_id: str
    engine_instance_id: str
    audience: str
    effects: frozenset[str]
    resource_scopes: frozenset[str]
    capability_grants: frozenset[str]
    expires_at: datetime
    revocation_generation: int
    active: bool = True


@dataclass(frozen=True, slots=True)
class OperationAuthority:
    operation_id: str
    effects: frozenset[str]
    resource_scopes: frozenset[str]
    capability_grants: frozenset[str]


@dataclass(frozen=True, slots=True, repr=False)
class AuthorityContext:
    invocation_id: str
    activation: ActivationIdentity
    origin_principal_id: str
    operation_id: str
    effects: frozenset[str]
    resource_scopes: frozenset[str]
    capability_grants: frozenset[str]
    expires_at: datetime
    activation_generation: int
    origin_generation: int


class TrustedAuthorityState(Protocol):
    """Resolve current supervisor state; None means absent or no authority.

    Snapshots must come from trusted engine ownership, never worker JSON.
    Revocation increments the respective generation; generations never reset
    while an identity remains valid. Implementations synchronize their reads.
    """

    def activation(self, identity: ActivationIdentity) -> ActivationState | None: ...

    def origin(self, principal_id: str) -> OriginState | None: ...

    def operation(self, operation_id: str) -> OperationAuthority | None: ...


class TrustedContextStore(Protocol):
    """Own issued contexts, independently of opaque live handles.

    put is atomic insert-only by invocation_id; collisions must raise and must
    never overwrite. get returns the original immutable context or None.
    Durable implementations preserve the exact deadline, captured scopes and
    generations. Workers must have no write access to this repository.
    """

    def put(self, context: AuthorityContext) -> None: ...

    def get(self, invocation_id: str) -> AuthorityContext | None: ...

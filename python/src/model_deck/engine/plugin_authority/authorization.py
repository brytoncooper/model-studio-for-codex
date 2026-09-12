"""Resolve authenticated plugin authority without implicit delegation upgrades."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
import secrets
import threading
import uuid

from .ports import (
    ActivationIdentity, ActivationState, AuthorityContext, OperationAuthority,
    OriginState, TrustedAuthorityState, TrustedContextStore,
)


class AuthorityDeniedError(PermissionError):
    def __init__(self) -> None:
        super().__init__("plugin authority denied")


def _names(values: frozenset[str]) -> frozenset[str]:
    if type(values) is not frozenset or any(type(value) is not str or not value for value in values):
        raise AuthorityDeniedError()
    return values


def _text(value: str) -> None:
    if type(value) is not str or not value:
        raise AuthorityDeniedError()


def _timestamp(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AuthorityDeniedError()


def _generation(value: int) -> None:
    if type(value) is not int or value < 0:
        raise AuthorityDeniedError()


class PluginAuthority:
    """Application authorization policy over injected, trusted state and clock.

    issue/capture/reauthorize_captured are supervisor APIs, not worker methods.
    A broker supplies authenticated_activation from its private channel, never
    from request params. Follow-up jobs/subscriptions retain invocation_id in
    their own trusted records and use reauthorize_captured on every call.
    """

    def __init__(
        self, *, engine_instance_id: str, audience: str,
        state: TrustedAuthorityState, contexts: TrustedContextStore,
        clock: Callable[[], datetime],
        invocation_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
        handle_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
    ) -> None:
        _text(engine_instance_id)
        _text(audience)
        self._engine_instance_id = engine_instance_id
        self._audience = audience
        self._state = state
        self._contexts = contexts
        self._clock = clock
        self._invocation_id_factory = invocation_id_factory
        self._handle_factory = handle_factory
        self._handles: dict[str, str] = {}
        self._lock = threading.Lock()

    def _current(
        self, identity: ActivationIdentity, principal_id: str, operation_id: str,
    ) -> tuple[ActivationState, OriginState, OperationAuthority, datetime]:
        if type(identity) is not ActivationIdentity:
            raise AuthorityDeniedError()
        for value in (identity.engine_instance_id, identity.audience, identity.activation_id,
                      identity.plugin_id, identity.plugin_version, principal_id, operation_id):
            _text(value)
        if identity.engine_instance_id != self._engine_instance_id or identity.audience != self._audience:
            raise AuthorityDeniedError()
        activation = self._state.activation(identity)
        origin = self._state.origin(principal_id)
        operation = self._state.operation(operation_id)
        if not isinstance(activation, ActivationState) or not isinstance(origin, OriginState) \
                or not isinstance(operation, OperationAuthority):
            raise AuthorityDeniedError()
        if activation.identity != identity or origin.principal_id != principal_id \
                or origin.engine_instance_id != self._engine_instance_id \
                or origin.audience != self._audience or operation.operation_id != operation_id:
            raise AuthorityDeniedError()
        now = self._clock()
        _timestamp(now)
        for owner in (activation, origin):
            _timestamp(owner.expires_at)
            _generation(owner.revocation_generation)
            if owner.active is not True or owner.expires_at <= now:
                raise AuthorityDeniedError()
        for owner in (activation, origin, operation):
            _names(owner.effects)
            _names(owner.resource_scopes)
            _names(owner.capability_grants)
        return activation, origin, operation, now

    def issue(
        self, authenticated_activation: ActivationIdentity, origin_principal_id: str,
        operation_id: str, *, expires_at: datetime,
    ) -> str:
        """Capture intersected authority and return a fresh opaque live handle."""
        activation, origin, operation, now = self._current(
            authenticated_activation, origin_principal_id, operation_id,
        )
        _timestamp(expires_at)
        deadline = min(expires_at, activation.expires_at, origin.expires_at)
        if deadline <= now:
            raise AuthorityDeniedError()
        invocation_id = self._invocation_id_factory()
        handle = self._handle_factory()
        _text(invocation_id)
        _text(handle)
        context = AuthorityContext(
            invocation_id=invocation_id, activation=authenticated_activation,
            origin_principal_id=origin_principal_id, operation_id=operation_id,
            effects=activation.effects & origin.effects & operation.effects,
            resource_scopes=activation.resource_scopes & origin.resource_scopes & operation.resource_scopes,
            capability_grants=activation.capability_grants & origin.capability_grants & operation.capability_grants,
            expires_at=deadline, activation_generation=activation.revocation_generation,
            origin_generation=origin.revocation_generation,
        )
        with self._lock:
            if handle in self._handles:
                raise AuthorityDeniedError()
            self._contexts.put(context)
            self._handles[handle] = invocation_id
        return handle

    def capture(self, handle: str, authenticated_activation: ActivationIdentity) -> AuthorityContext:
        """Return currently narrowed context for a supervisor-owned job/subscription."""
        _text(handle)
        with self._lock:
            invocation_id = self._handles.get(handle)
        if invocation_id is None:
            raise AuthorityDeniedError()
        return self._resolve_captured(invocation_id, authenticated_activation)

    def _resolve_captured(
        self, invocation_id: str, identity: ActivationIdentity,
    ) -> AuthorityContext:
        _text(invocation_id)
        context = self._contexts.get(invocation_id)
        if type(context) is not AuthorityContext or context.invocation_id != invocation_id \
                or context.activation != identity:
            raise AuthorityDeniedError()
        activation, origin, operation, now = self._current(
            identity, context.origin_principal_id, context.operation_id,
        )
        _timestamp(context.expires_at)
        _generation(context.activation_generation)
        _generation(context.origin_generation)
        if context.expires_at <= now or activation.revocation_generation != context.activation_generation \
                or origin.revocation_generation != context.origin_generation:
            raise AuthorityDeniedError()
        return replace(
            context,
            effects=_names(context.effects) & activation.effects & origin.effects & operation.effects,
            resource_scopes=_names(context.resource_scopes) & activation.resource_scopes & origin.resource_scopes & operation.resource_scopes,
            capability_grants=_names(context.capability_grants) & activation.capability_grants & origin.capability_grants & operation.capability_grants,
            expires_at=min(context.expires_at, activation.expires_at, origin.expires_at),
        )

    @staticmethod
    def _require(
        context: AuthorityContext, *, effect: str, resource_scope: str,
        capability_grant: str, private_namespace: str | None,
    ) -> AuthorityContext:
        for value in (effect, resource_scope, capability_grant):
            _text(value)
        if effect not in context.effects or resource_scope not in context.resource_scopes \
                or capability_grant not in context.capability_grants:
            raise AuthorityDeniedError()
        if private_namespace is not None and private_namespace != context.activation.plugin_id:
            raise AuthorityDeniedError()
        return context

    def authorize(
        self, handle: str, authenticated_activation: ActivationIdentity, *,
        effect: str, resource_scope: str, capability_grant: str,
        private_namespace: str | None = None,
    ) -> AuthorityContext:
        return self._require(
            self.capture(handle, authenticated_activation), effect=effect,
            resource_scope=resource_scope, capability_grant=capability_grant,
            private_namespace=private_namespace,
        )

    def reauthorize_captured(
        self, invocation_id: str, authenticated_activation: ActivationIdentity, *,
        effect: str, resource_scope: str, capability_grant: str,
        private_namespace: str | None = None,
    ) -> AuthorityContext:
        """Recheck original authority from trusted persistence, without renewal."""
        return self._require(
            self._resolve_captured(invocation_id, authenticated_activation), effect=effect,
            resource_scope=resource_scope, capability_grant=capability_grant,
            private_namespace=private_namespace,
        )

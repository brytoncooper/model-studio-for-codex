from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from collections.abc import Callable
from typing import Any

from model_deck.engine.routing.ports import (
    ContinuationScope,
    RouteResolveRequest,
    RouteResolver,
    RouteSnapshot,
)
from model_deck.engine.sessions.ports import (
    CreateSessionCommand,
    GetSessionCommand,
    SelectModelCommand,
    SessionRecord,
    SessionRepository,
)

_OPAQUE_REF_PATTERN = re.compile(r"^ref:[a-z][a-z0-9._-]{0,120}$")
_MAX_OPAQUE_REF = 128


def _reject_unknown_keys(
    mapping: Mapping[str, Any],
    allowed: frozenset[str],
    *,
    context: str,
) -> None:
    unknown = set(mapping.keys()) - allowed
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"unknown {context} field(s): {names}")


def _canonical_uuid_spelling(value: str) -> bool:
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return str(parsed).casefold() == value.casefold()


def _validate_uuid_field(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    if not _canonical_uuid_spelling(value):
        raise ValueError(f"{name} must be a UUID")
    return value


def _validate_expected_revision(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("expected_revision must be an integer")
    if value < 0:
        raise ValueError("expected_revision must be >= 0")
    return value


def _validate_opaque_ref(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    if len(value) > _MAX_OPAQUE_REF:
        raise ValueError(f"{name} must be at most 128 characters")
    if _OPAQUE_REF_PATTERN.fullmatch(value):
        return value
    if _canonical_uuid_spelling(value):
        return value
    raise ValueError(f"{name} must be a UUID or ref: opaque reference")


def _optional_host_context_ref(params: Mapping[str, Any]) -> str | None:
    if "host_context_ref" not in params:
        return None
    return _validate_opaque_ref("host_context_ref", params["host_context_ref"])


def _validate_continuation_reset(params: Mapping[str, Any]) -> bool:
    if "continuation_reset" not in params:
        return False
    value = params["continuation_reset"]
    if not isinstance(value, bool):
        raise ValueError("continuation_reset must be a boolean")
    return value


def _continuation_scope_payload(scope: ContinuationScope) -> dict[str, Any]:
    return {
        "connection_id": scope.connection_id,
        "provider_model_id": scope.provider_model_id,
        "provider_id": scope.provider_id,
        "execution_mode": scope.execution_mode.value,
        "handle": scope.handle,
    }


def _new_continuation_handle() -> str:
    return f"ref:continuation.{uuid.uuid4()}"


def _scope_for_route(
    route: RouteSnapshot,
    handle_factory: Callable[[], str],
) -> ContinuationScope:
    handle = handle_factory()
    _validate_opaque_ref("continuation handle", handle)
    return ContinuationScope(
        connection_id=route.connection_id,
        provider_model_id=route.provider_model_id,
        provider_id=route.provider_id,
        execution_mode=route.execution_mode,
        handle=handle,
    )


def _session_payload(record: SessionRecord) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "session_id": record.session_id,
        "registration_id": record.registration_id,
        "revision": record.revision,
    }
    if record.host_context_ref is not None:
        payload["host_context_ref"] = record.host_context_ref
    if record.continuation_scope is not None:
        payload["continuation_scope"] = _continuation_scope_payload(
            record.continuation_scope
        )
    return payload


def _create_result(record: SessionRecord) -> dict[str, Any]:
    return {
        "session_id": record.session_id,
        "revision": record.revision,
    }


def _select_result(record: SessionRecord) -> dict[str, Any]:
    return {
        "session_id": record.session_id,
        "revision": record.revision,
    }


def _route_matches_continuation(
    route: RouteSnapshot, scope: ContinuationScope
) -> bool:
    return (
        route.connection_id == scope.connection_id
        and route.provider_id == scope.provider_id
        and route.provider_model_id == scope.provider_model_id
        and route.execution_mode == scope.execution_mode
    )


def _resolve_registration(
    route_resolver: RouteResolver, registration_id: str
) -> RouteSnapshot:
    request = RouteResolveRequest(registration_id=registration_id)
    return route_resolver.resolve_active_registration(request)


def _ensure_continuation_scope_compatible(
    route: RouteSnapshot,
    current: SessionRecord,
    *,
    continuation_reset: bool,
) -> None:
    if continuation_reset or current.continuation_scope is None:
        return
    if not _route_matches_continuation(route, current.continuation_scope):
        raise ValueError(
            "registration selection is incompatible with the session continuation scope"
        )


class CreateSessionUseCase:
    def __init__(
        self,
        repository: SessionRepository,
        route_resolver: RouteResolver,
        *,
        continuation_handle_factory: Callable[[], str] = _new_continuation_handle,
    ) -> None:
        self._repository = repository
        self._route_resolver = route_resolver
        self._continuation_handle_factory = continuation_handle_factory

    def execute(self, params: Mapping[str, Any] | None) -> dict[str, Any]:
        params = dict(params or {})
        allowed = frozenset({"registration_id", "host_context_ref"})
        _reject_unknown_keys(params, allowed, context="params")
        registration_id = _validate_uuid_field(
            "registration_id", params.get("registration_id")
        )
        host_context_ref = _optional_host_context_ref(params)
        route = _resolve_registration(self._route_resolver, registration_id)
        command = CreateSessionCommand(
            registration_id=registration_id,
            host_context_ref=host_context_ref,
            continuation_scope=_scope_for_route(
                route,
                self._continuation_handle_factory,
            ),
        )
        record = self._repository.create(command)
        return _create_result(record)


class GetSessionUseCase:
    def __init__(self, repository: SessionRepository) -> None:
        self._repository = repository

    def execute(self, params: Mapping[str, Any] | None) -> dict[str, Any]:
        params = dict(params or {})
        allowed = frozenset({"session_id"})
        _reject_unknown_keys(params, allowed, context="params")
        session_id = _validate_uuid_field("session_id", params.get("session_id"))
        command = GetSessionCommand(session_id=session_id)
        record = self._repository.get(command)
        return {"session": _session_payload(record)}


class SelectSessionModelUseCase:
    def __init__(
        self,
        repository: SessionRepository,
        route_resolver: RouteResolver,
        *,
        continuation_handle_factory: Callable[[], str] = _new_continuation_handle,
    ) -> None:
        self._repository = repository
        self._route_resolver = route_resolver
        self._continuation_handle_factory = continuation_handle_factory

    def execute(self, params: Mapping[str, Any] | None) -> dict[str, Any]:
        params = dict(params or {})
        allowed = frozenset(
            {
                "session_id",
                "registration_id",
                "expected_revision",
                "continuation_reset",
            }
        )
        _reject_unknown_keys(params, allowed, context="params")
        session_id = _validate_uuid_field("session_id", params.get("session_id"))
        registration_id = _validate_uuid_field(
            "registration_id", params.get("registration_id")
        )
        expected_revision = _validate_expected_revision(params.get("expected_revision"))
        continuation_reset = _validate_continuation_reset(params)
        current = self._repository.get(GetSessionCommand(session_id=session_id))
        route = _resolve_registration(self._route_resolver, registration_id)
        _ensure_continuation_scope_compatible(
            route,
            current,
            continuation_reset=continuation_reset,
        )
        command = SelectModelCommand(
            session_id=session_id,
            registration_id=registration_id,
            expected_revision=expected_revision,
            continuation_reset=continuation_reset,
            replacement_continuation_scope=(
                _scope_for_route(route, self._continuation_handle_factory)
                if continuation_reset
                else None
            ),
        )
        record = self._repository.select_model(command)
        return _select_result(record)

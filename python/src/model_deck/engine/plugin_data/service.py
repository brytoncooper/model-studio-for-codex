"""Supervisor-side storage broker over settled PluginAuthority and PluginDataRepository."""
from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from ..plugin_authority import (
    ActivationIdentity,
    AuthorityDeniedError,
    PluginAuthority,
)
from .ports import (
    LIST_LIMIT_MAX,
    LIST_LIMIT_MIN,
    PluginDataNotFoundError,
    PluginDataQuotaExceededError,
    PluginDataRepository,
    PluginDataRevisionConflictError,
)

_OPERATIONS = ("get", "list", "put", "delete")


class BrokerDataError(ValueError):
    pass


class BrokerDataInvalidRequestError(BrokerDataError):
    def __init__(self) -> None:
        super().__init__("broker invalid request")


class BrokerDataPayloadInvalidError(BrokerDataError):
    def __init__(self) -> None:
        super().__init__("broker payload invalid")


class BrokerDataNotFoundError(BrokerDataError):
    def __init__(self) -> None:
        super().__init__("broker data not found")


class BrokerDataRevisionConflictError(BrokerDataError):
    def __init__(self) -> None:
        super().__init__("broker revision conflict")


class BrokerDataQuotaExceededError(BrokerDataError):
    def __init__(self) -> None:
        super().__init__("broker quota exceeded")


class BrokerDataDeniedError(PermissionError):
    def __init__(self) -> None:
        super().__init__("broker authority denied")


class BrokerDataGuardError(BrokerDataError):
    def __init__(self) -> None:
        super().__init__("broker guard failed")


class BrokerDataStoreError(BrokerDataError):
    def __init__(self) -> None:
        super().__init__("broker store unavailable")


def _has_lone_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(char) <= 0xDFFF for char in text)


def _check_handle(handle: object) -> str:
    if type(handle) is not str or not handle or len(handle) > 128:
        raise BrokerDataInvalidRequestError()
    return handle


def _check_namespace(namespace: object) -> str:
    if type(namespace) is not str or not namespace or len(namespace) > 256:
        raise BrokerDataInvalidRequestError()
    return namespace


def _check_key(key: object) -> str:
    if type(key) is not str or len(key) > 256 or _has_lone_surrogate(key):
        raise BrokerDataInvalidRequestError()
    return key


def _check_prefix(prefix: object) -> str:
    if type(prefix) is not str or len(prefix) > 256 or _has_lone_surrogate(prefix):
        raise BrokerDataInvalidRequestError()
    return prefix


def _check_limit(limit: object) -> int:
    if type(limit) is not int or not LIST_LIMIT_MIN <= limit <= LIST_LIMIT_MAX:
        raise BrokerDataInvalidRequestError()
    return limit


def _check_expected_revision(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise BrokerDataInvalidRequestError()
    return value


def _reject_non_json(value: Any, seen: set[int]) -> None:
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        return
    if type(value) is float:
        if math.isnan(value) or math.isinf(value):
            raise BrokerDataPayloadInvalidError()
        return
    if type(value) is str:
        return
    if type(value) is list:
        if id(value) in seen:
            raise BrokerDataPayloadInvalidError()
        seen.add(id(value))
        try:
            for item in value:
                _reject_non_json(item, seen)
        finally:
            seen.discard(id(value))
        return
    if type(value) is dict:
        if id(value) in seen:
            raise BrokerDataPayloadInvalidError()
        seen.add(id(value))
        try:
            for dict_key, item in value.items():
                if type(dict_key) is not str:
                    raise BrokerDataPayloadInvalidError()
                _reject_non_json(item, seen)
        finally:
            seen.discard(id(value))
        return
    raise BrokerDataPayloadInvalidError()


def _check_value(value: Any) -> None:
    try:
        _reject_non_json(value, set())
    except BrokerDataPayloadInvalidError:
        raise
    except (TypeError, ValueError, RecursionError):
        raise BrokerDataPayloadInvalidError() from None
    try:
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise BrokerDataPayloadInvalidError() from None


class PluginDataBroker:
    """Supervisor-side broker; synchronous, no sockets or callbacks."""

    def __init__(
        self,
        *,
        authority: PluginAuthority,
        repository: PluginDataRepository,
        grants: dict[str, tuple[str, str, str]],
        mutation_guard: Callable[[], AbstractContextManager[None]],
    ) -> None:
        if not isinstance(authority, PluginAuthority):
            raise BrokerDataInvalidRequestError()
        if not isinstance(repository, PluginDataRepository):
            raise BrokerDataInvalidRequestError()
        if type(grants) is not dict or set(grants) != set(_OPERATIONS):
            raise BrokerDataInvalidRequestError()
        parsed: dict[str, tuple[str, str, str]] = {}
        for operation in _OPERATIONS:
            triple = grants[operation]
            if type(triple) is not tuple or len(triple) != 3:
                raise BrokerDataInvalidRequestError()
            for part in triple:
                if type(part) is not str or not part:
                    raise BrokerDataInvalidRequestError()
            parsed[operation] = triple
        if not callable(mutation_guard):
            raise BrokerDataInvalidRequestError()
        self._authority = authority
        self._repository = repository
        self._grants = parsed
        self._mutation_guard = mutation_guard

    def revocation_barrier(self) -> AbstractContextManager[None]:
        return self._mutation_guard()

    @contextmanager
    def _guarded(self) -> Iterator[None]:
        try:
            guard = self._mutation_guard()
        except Exception:
            raise BrokerDataGuardError() from None
        try:
            with guard:
                yield
        except (BrokerDataError, BrokerDataDeniedError):
            raise
        except Exception:
            raise BrokerDataGuardError() from None

    def _authorize(
        self,
        handle: str,
        activation: ActivationIdentity,
        operation: str,
        namespace: str,
    ) -> None:
        if type(activation) is not ActivationIdentity:
            raise BrokerDataInvalidRequestError()
        if namespace != activation.plugin_id:
            raise BrokerDataDeniedError()
        effect, scope, grant = self._grants[operation]
        try:
            self._authority.authorize(
                handle,
                activation,
                effect=effect,
                resource_scope=scope,
                capability_grant=grant,
                private_namespace=namespace,
            )
        except AuthorityDeniedError:
            raise BrokerDataDeniedError() from None
        except BrokerDataError:
            raise
        except Exception:
            raise BrokerDataStoreError() from None

    def get(
        self, handle: str, authenticated_activation: ActivationIdentity, *, namespace: str, key: str,
    ) -> dict[str, Any]:
        handle = _check_handle(handle)
        namespace = _check_namespace(namespace)
        key = _check_key(key)
        with self._guarded():
            self._authorize(handle, authenticated_activation, "get", namespace)
            try:
                entry = self._repository.get(namespace, key)
            except PluginDataNotFoundError:
                raise BrokerDataNotFoundError() from None
            except BrokerDataError:
                raise
            except (KeyError, ValueError):
                raise BrokerDataInvalidRequestError() from None
            except Exception:
                raise BrokerDataStoreError() from None
        return {"value": entry.value, "revision": entry.revision}

    def list(
        self, handle: str, authenticated_activation: ActivationIdentity, *, namespace: str,
        prefix: str = "", limit: int = 200,
    ) -> dict[str, Any]:
        handle = _check_handle(handle)
        namespace = _check_namespace(namespace)
        prefix = _check_prefix(prefix)
        limit = _check_limit(limit)
        with self._guarded():
            self._authorize(handle, authenticated_activation, "list", namespace)
            try:
                items = self._repository.list(namespace, prefix, limit)
            except BrokerDataError:
                raise
            except ValueError:
                raise BrokerDataInvalidRequestError() from None
            except Exception:
                raise BrokerDataStoreError() from None
        return {"items": [{"key": item.key, "revision": item.revision} for item in items]}

    def put(
        self, handle: str, authenticated_activation: ActivationIdentity, *, namespace: str,
        key: str, value: Any, expected_revision: int | None = None,
    ) -> dict[str, int]:
        handle = _check_handle(handle)
        namespace = _check_namespace(namespace)
        key = _check_key(key)
        expected = _check_expected_revision(expected_revision)
        _check_value(value)
        with self._guarded():
            self._authorize(handle, authenticated_activation, "put", namespace)
            try:
                entry = self._repository.put(namespace, key, value, expected)
            except PluginDataRevisionConflictError:
                raise BrokerDataRevisionConflictError() from None
            except PluginDataQuotaExceededError:
                raise BrokerDataQuotaExceededError() from None
            except BrokerDataError:
                raise
            except TypeError:
                raise BrokerDataPayloadInvalidError() from None
            except ValueError:
                raise BrokerDataInvalidRequestError() from None
            except Exception:
                raise BrokerDataStoreError() from None
        return {"revision": entry.revision}

    def delete(
        self, handle: str, authenticated_activation: ActivationIdentity, *, namespace: str,
        key: str, expected_revision: int | None = None,
    ) -> dict[str, bool]:
        handle = _check_handle(handle)
        namespace = _check_namespace(namespace)
        key = _check_key(key)
        expected = _check_expected_revision(expected_revision)
        with self._guarded():
            self._authorize(handle, authenticated_activation, "delete", namespace)
            try:
                deleted = self._repository.delete(namespace, key, expected)
            except PluginDataRevisionConflictError:
                raise BrokerDataRevisionConflictError() from None
            except BrokerDataError:
                raise
            except ValueError:
                raise BrokerDataInvalidRequestError() from None
            except Exception:
                raise BrokerDataStoreError() from None
        return {"deleted": bool(deleted)}

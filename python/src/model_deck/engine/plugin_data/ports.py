from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

DEFAULT_QUOTA_BYTES = 10 * 1024 * 1024
DEFAULT_QUOTA_KEYS = 10000
DEFAULT_VALUE_MAX_BYTES = 1024 * 1024
LIST_LIMIT_MIN = 1
LIST_LIMIT_MAX = 200


@dataclass(frozen=True, slots=True)
class PluginDataQuota:
    max_bytes: int = DEFAULT_QUOTA_BYTES
    max_keys: int = DEFAULT_QUOTA_KEYS
    max_value_bytes: int = DEFAULT_VALUE_MAX_BYTES


@dataclass(frozen=True, slots=True)
class PluginDataEntry:
    namespace: str
    key: str
    value: Any
    revision: int


@dataclass(frozen=True, slots=True)
class PluginDataListItem:
    key: str
    revision: int


@runtime_checkable
class PluginDataRepository(Protocol):
    def get(self, namespace: str, key: str) -> PluginDataEntry: ...
    def list(self, namespace: str, prefix: str = "", limit: int = 200) -> list[PluginDataListItem]: ...
    def put(self, namespace: str, key: str, value: Any, expected_revision: int | None = None) -> PluginDataEntry: ...
    def delete(self, namespace: str, key: str, expected_revision: int | None = None) -> bool: ...


class PluginDataNotFoundError(KeyError):
    pass


class PluginDataRevisionConflictError(ValueError):
    pass


class PluginDataQuotaExceededError(ValueError):
    pass

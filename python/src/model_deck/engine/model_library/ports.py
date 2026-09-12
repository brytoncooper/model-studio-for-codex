from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class RegisteredModelRecord:
    registration_id: str
    provider_model_id: str
    connection_id: str
    display_name: str
    revision: int


@dataclass(frozen=True, slots=True)
class CatalogModelRecord:
    provider_model_id: str
    connection_id: str
    display_name: str
    catalog_revision: str | None = None


@dataclass(frozen=True, slots=True)
class CatalogListQuery:
    connection_id: str
    query: str | None = None
    cursor: str | None = None
    limit: int | None = None


@dataclass(frozen=True, slots=True)
class CatalogPage:
    items: tuple[CatalogModelRecord, ...]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class RegisterModelCommand:
    connection_id: str
    provider_model_id: str
    display_name: str
    expected_revision: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class RenameModelCommand:
    registration_id: str
    display_name: str
    expected_revision: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class RemoveModelCommand:
    registration_id: str
    expected_revision: int
    idempotency_key: str


@runtime_checkable
class ModelRepository(Protocol):
    def list_registered(self, *, connection_id: str | None = None) -> list[RegisteredModelRecord]: ...


@runtime_checkable
class CatalogCacheRepository(Protocol):
    def list_catalog(self, query: CatalogListQuery) -> CatalogPage: ...


@runtime_checkable
class ModelMutationRepository(Protocol):
    def register(self, command: RegisterModelCommand) -> RegisteredModelRecord: ...

    def rename(self, command: RenameModelCommand) -> RegisteredModelRecord: ...

    def remove(self, command: RemoveModelCommand) -> bool: ...


class CatalogUnavailableError(ValueError):
    pass


class ModelRevisionConflictError(ValueError):
    pass


class ModelIdempotencyConflictError(ValueError):
    pass


class ModelRegistrationNotFoundError(ValueError):
    pass

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from model_deck.engine.model_library.ports import (
    CatalogCacheRepository,
    CatalogListQuery,
    CatalogUnavailableError,
    ModelMutationRepository,
    ModelRepository,
    RegisterModelCommand,
    RegisteredModelRecord,
    RemoveModelCommand,
    RenameModelCommand,
)


class UnsupportedCollectionError(ValueError):
    pass

_MAX_CATALOG_LIMIT = 200
_MAX_PROVIDER_MODEL_ID = 256
_MAX_DISPLAY_NAME = 256
_MAX_IDEMPOTENCY_KEY = 128


class ListModelsUseCase:
    def __init__(
        self,
        repository: ModelRepository,
        catalog_reader: CatalogCacheRepository | None = None,
    ) -> None:
        self._repository = repository
        self._catalog_reader = catalog_reader

    def execute(self, params: Mapping[str, Any] | None) -> dict[str, Any]:
        params = dict(params or {})
        collection = params.get("collection", "registered")
        if collection == "registered":
            return self._list_registered(params)
        if collection == "catalog":
            return self._list_catalog(params)
        raise UnsupportedCollectionError(f"unsupported collection: {collection}")

    def _list_registered(self, params: Mapping[str, Any]) -> dict[str, Any]:
        connection_id = params.get("connection_id")
        if connection_id is not None and not isinstance(connection_id, str):
            raise ValueError("connection_id must be a string")
        items = []
        for row in self._repository.list_registered(connection_id=connection_id):
            items.append(
                {
                    "kind": "registered",
                    "registration_id": row.registration_id,
                    "provider_model_id": row.provider_model_id,
                    "connection_id": row.connection_id,
                    "display_name": row.display_name,
                    "revision": row.revision,
                }
            )
        return {"collection": "registered", "items": items}

    def _list_catalog(self, params: Mapping[str, Any]) -> dict[str, Any]:
        connection_id = params.get("connection_id")
        if not isinstance(connection_id, str) or not connection_id:
            raise ValueError("connection_id is required when collection=catalog")
        if self._catalog_reader is None:
            raise CatalogUnavailableError("catalog cache is not configured")
        query_text = params.get("query")
        if query_text is not None and not isinstance(query_text, str):
            raise ValueError("query must be a string")
        cursor = params.get("cursor")
        if cursor is not None and not isinstance(cursor, str):
            raise ValueError("cursor must be a string")
        limit = params.get("limit")
        if limit is not None:
            if not isinstance(limit, int) or isinstance(limit, bool):
                raise ValueError("limit must be an integer")
            if limit < 1 or limit > _MAX_CATALOG_LIMIT:
                raise ValueError("limit must be between 1 and 200")
        page = self._catalog_reader.list_catalog(
            CatalogListQuery(
                connection_id=connection_id,
                query=query_text,
                cursor=cursor,
                limit=limit,
            )
        )
        items = []
        for row in page.items:
            item: dict[str, Any] = {
                "kind": "catalog",
                "provider_model_id": row.provider_model_id,
                "connection_id": row.connection_id,
                "display_name": row.display_name,
            }
            if row.catalog_revision is not None:
                item["catalog_revision"] = row.catalog_revision
            items.append(item)
        result: dict[str, Any] = {
            "collection": "catalog",
            "items": items,
            "cache_only": True,
        }
        if page.next_cursor is not None:
            result["next_cursor"] = page.next_cursor
        return result


def _validate_uuid_field(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a UUID") from exc
    if str(parsed).casefold() != value.casefold():
        raise ValueError(f"{name} must be a UUID")
    return value


def _validate_expected_revision(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("expected_revision must be an integer")
    if value < 0:
        raise ValueError("expected_revision must be >= 0")
    return value


def _validate_idempotency_key(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("idempotency_key must be a non-empty string")
    if len(value) > _MAX_IDEMPOTENCY_KEY:
        raise ValueError("idempotency_key must be at most 128 characters")
    return value


def _validate_provider_model_id(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("provider_model_id must be a non-empty string")
    if len(value) > _MAX_PROVIDER_MODEL_ID:
        raise ValueError("provider_model_id must be at most 256 characters")
    return value


def _validate_display_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("display_name must be a string")
    if len(value) > _MAX_DISPLAY_NAME:
        raise ValueError("display_name must be at most 256 characters")
    return value


def _model_result(record: RegisteredModelRecord) -> dict[str, Any]:
    return {
        "model": {
            "registration_id": record.registration_id,
            "provider_model_id": record.provider_model_id,
            "connection_id": record.connection_id,
            "display_name": record.display_name,
            "revision": record.revision,
        }
    }


class RegisterModelUseCase:
    def __init__(self, repository: ModelMutationRepository) -> None:
        self._repository = repository

    def execute(self, params: Mapping[str, Any] | None) -> dict[str, Any]:
        params = dict(params or {})
        command = RegisterModelCommand(
            connection_id=_validate_uuid_field("connection_id", params.get("connection_id")),
            provider_model_id=_validate_provider_model_id(params.get("provider_model_id")),
            display_name=_validate_display_name(params.get("display_name")),
            expected_revision=_validate_expected_revision(params.get("expected_revision")),
            idempotency_key=_validate_idempotency_key(params.get("idempotency_key")),
        )
        record = self._repository.register(command)
        return _model_result(record)


class RenameModelUseCase:
    def __init__(self, repository: ModelMutationRepository) -> None:
        self._repository = repository

    def execute(self, params: Mapping[str, Any] | None) -> dict[str, Any]:
        params = dict(params or {})
        command = RenameModelCommand(
            registration_id=_validate_uuid_field("registration_id", params.get("registration_id")),
            display_name=_validate_display_name(params.get("display_name")),
            expected_revision=_validate_expected_revision(params.get("expected_revision")),
            idempotency_key=_validate_idempotency_key(params.get("idempotency_key")),
        )
        record = self._repository.rename(command)
        return _model_result(record)


class RemoveModelUseCase:
    def __init__(self, repository: ModelMutationRepository) -> None:
        self._repository = repository

    def execute(self, params: Mapping[str, Any] | None) -> dict[str, Any]:
        params = dict(params or {})
        command = RemoveModelCommand(
            registration_id=_validate_uuid_field("registration_id", params.get("registration_id")),
            expected_revision=_validate_expected_revision(params.get("expected_revision")),
            idempotency_key=_validate_idempotency_key(params.get("idempotency_key")),
        )
        removed = self._repository.remove(command)
        return {"removed": removed}

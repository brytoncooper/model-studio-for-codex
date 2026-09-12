from __future__ import annotations

import json
from pathlib import Path

from model_deck.engine.model_library.ports import (
    CatalogListQuery,
    CatalogModelRecord,
    CatalogPage,
    CatalogUnavailableError,
)

_CURSOR_PREFIX = "offset:"
_DEFAULT_PAGE_LIMIT = 50
_MAX_PAGE_LIMIT = 200


class JsonFixtureCatalogCacheRepository:
    """Cache-only catalog reader backed by an explicit JSON fixture path."""

    def __init__(self, fixture_path: Path | None) -> None:
        self._fixture_path = fixture_path
        self._payload: dict | None = None
        self._loaded = False

    def list_catalog(self, query: CatalogListQuery) -> CatalogPage:
        payload = self._load_payload()
        if payload is None:
            raise CatalogUnavailableError("catalog cache is not available")
        connection_id = payload.get("connection_id")
        if connection_id != query.connection_id:
            raise CatalogUnavailableError(f"no catalog cache for connection_id={query.connection_id}")
        revision = payload.get("catalog_revision")
        if revision is not None and not isinstance(revision, str):
            raise CatalogUnavailableError("catalog cache fixture has invalid catalog_revision")
        models = payload.get("models")
        if not isinstance(models, list):
            raise CatalogUnavailableError("catalog cache fixture has no models")
        rows: list[CatalogModelRecord] = []
        for entry in models:
            if not isinstance(entry, dict):
                raise CatalogUnavailableError("catalog cache fixture has invalid model entry")
            provider_model_id = entry.get("provider_model_id")
            display_name = entry.get("display_name")
            if not isinstance(provider_model_id, str) or not isinstance(display_name, str):
                raise CatalogUnavailableError("catalog cache fixture model missing provider_model_id/display_name")
            rows.append(
                CatalogModelRecord(
                    provider_model_id=provider_model_id,
                    connection_id=query.connection_id,
                    display_name=display_name,
                    catalog_revision=revision if isinstance(revision, str) else None,
                )
            )
        rows.sort(key=lambda row: row.provider_model_id)
        if query.query:
            needle = query.query.casefold()
            rows = [
                row
                for row in rows
                if needle in row.provider_model_id.casefold() or needle in row.display_name.casefold()
            ]
        offset = _parse_cursor(query.cursor)
        if offset > len(rows):
            offset = len(rows)
        page_limit = query.limit if query.limit is not None else _DEFAULT_PAGE_LIMIT
        if page_limit < 1 or page_limit > _MAX_PAGE_LIMIT:
            raise ValueError("limit must be between 1 and 200")
        page_rows = tuple(rows[offset : offset + page_limit])
        next_offset = offset + len(page_rows)
        next_cursor = _format_cursor(next_offset) if next_offset < len(rows) else None
        return CatalogPage(items=page_rows, next_cursor=next_cursor)

    def _load_payload(self) -> dict | None:
        if self._loaded:
            return self._payload
        self._loaded = True
        path = self._fixture_path
        if path is None or not path.is_file():
            self._payload = None
            return None
        if path.is_symlink():
            raise CatalogUnavailableError(f"catalog cache fixture symlink refused: {path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CatalogUnavailableError(f"catalog cache fixture unreadable: {path.name}") from exc
        if not isinstance(raw, dict):
            raise CatalogUnavailableError("catalog cache fixture must be a JSON object")
        self._payload = raw
        return self._payload


def _parse_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if not cursor.startswith(_CURSOR_PREFIX):
        raise ValueError("cursor is invalid")
    suffix = cursor[len(_CURSOR_PREFIX) :]
    if not suffix.isdigit():
        raise ValueError("cursor is invalid")
    return int(suffix)


def _format_cursor(offset: int) -> str:
    return f"{_CURSOR_PREFIX}{offset}"

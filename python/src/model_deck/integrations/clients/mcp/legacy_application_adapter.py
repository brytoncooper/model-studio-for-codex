from __future__ import annotations

from typing import Any

from model_deck.integrations.clients.mcp.errors import McpReadError
from model_deck.integrations.clients.mcp.ports import LegacyMcpApplicationAdapter


class DelegatingLegacyMcpApplicationAdapter:
    """Named legacy adapter that forwards to the existing MCP Deck implementation."""

    def __init__(self, deck: Any) -> None:
        self._deck = deck

    def list_endpoints(self) -> dict[str, Any]:
        return self._invoke("list_endpoints")

    def search_models(self, query: str, endpoint: str | None = None, limit: int = 20) -> dict[str, Any]:
        return self._invoke("search_models", query=query, endpoint=endpoint, limit=limit)

    def list_added_models(self) -> dict[str, Any]:
        return self._invoke("list_added_models")

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._deck.call(name, arguments)
        except Exception as exc:
            raise self._translate(exc) from exc

    def _invoke(self, name: str, **kwargs: Any) -> dict[str, Any]:
        try:
            method = getattr(self._deck, name)
            return method(**kwargs)
        except Exception as exc:
            raise self._translate(exc) from exc

    @staticmethod
    def _translate(exc: Exception) -> Exception:
        deck_error = exc.__class__.__name__
        if deck_error == "DeckError":
            return McpReadError(str(exc))
        return exc


def as_legacy_adapter(deck: Any) -> LegacyMcpApplicationAdapter:
    return DelegatingLegacyMcpApplicationAdapter(deck)

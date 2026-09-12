from __future__ import annotations

from typing import Any

from model_deck.engine.model_library.ports import CatalogUnavailableError
from model_deck.engine.model_library.use_cases import ListModelsUseCase, UnsupportedCollectionError
from model_deck.integrations.clients.mcp.errors import McpReadError
from model_deck.integrations.clients.mcp.ports import (
    LegacyMcpApplicationAdapter,
    McpCatalogEndpointResolver,
    McpCatalogSearchProvenance,
    McpRegisteredModelPresentation,
)
from model_deck.integrations.clients.mcp.presenters import (
    UnverifiedCacheCatalogProvenance,
    clamp_mcp_limit,
    present_catalog_search,
    present_list_added_models,
)


class McpModelReadService:
    """MCP model list/search reads routed through engine use cases with legacy fallback."""

    def __init__(
        self,
        *,
        list_models: ListModelsUseCase,
        legacy: LegacyMcpApplicationAdapter,
        presentation: McpRegisteredModelPresentation,
        catalog_resolver: McpCatalogEndpointResolver | None = None,
        catalog_provenance: McpCatalogSearchProvenance | None = None,
    ) -> None:
        self._list_models = list_models
        self._legacy = legacy
        self._presentation = presentation
        self._catalog_resolver = catalog_resolver
        self._catalog_provenance = catalog_provenance or UnverifiedCacheCatalogProvenance()

    def list_endpoints(self) -> dict[str, Any]:
        return self._legacy.list_endpoints()

    def list_added_models(self) -> dict[str, Any]:
        try:
            engine_result = self._list_models.execute({"collection": "registered"})
        except (ValueError, UnsupportedCollectionError) as exc:
            raise McpReadError(str(exc)) from exc
        return present_list_added_models(engine_result, self._presentation)

    def search_models(self, query: str, endpoint: str | None = None, limit: int = 20) -> dict[str, Any]:
        connection_id = None
        if self._catalog_resolver is not None:
            connection_id = self._catalog_resolver.catalog_connection_for_endpoint(endpoint)
        if connection_id is None:
            return self._legacy.search_models(query, endpoint=endpoint, limit=limit)
        endpoint_name = endpoint or self._catalog_resolver.default_catalog_endpoint_name() or "OpenRouter"
        try:
            engine_result = self._list_models.execute(
                {
                    "collection": "catalog",
                    "connection_id": connection_id,
                    "query": str(query or ""),
                    "limit": clamp_mcp_limit(limit),
                }
            )
        except CatalogUnavailableError as exc:
            raise McpReadError(str(exc)) from exc
        except ValueError as exc:
            raise McpReadError(str(exc)) from exc
        billing, billing_note = self._presentation.billing_for(connection_id)
        source, verified, note = self._catalog_provenance.catalog_search_metadata(connection_id)
        return present_catalog_search(
            engine_result,
            endpoint_name=endpoint_name,
            billing=billing,
            billing_note=billing_note,
            source=source,
            verified=verified,
            note=note,
        )

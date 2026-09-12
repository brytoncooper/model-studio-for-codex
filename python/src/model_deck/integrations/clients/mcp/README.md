# MCP client adapter (B06)

This package converges **model read** orchestration for MCP onto `model_deck.engine.model_library` use cases. It does not own the stdio JSON-RPC entrypoint (`model_deck_mcp.py`); the host wires that entrypoint to these types after review.

## Boundary

| MCP tool / read | Path |
| --- | --- |
| `list_added_models` | `ListModelsUseCase` (`collection=registered`) + `McpRegisteredModelPresentation` |
| `search_models` | `ListModelsUseCase` (`collection=catalog`) when `McpCatalogEndpointResolver` maps the endpoint to a cached connection; otherwise `LegacyMcpApplicationAdapter` |
| `list_endpoints` | `LegacyMcpApplicationAdapter` until connection projection exposes MCP-shaped endpoint rows |
| Writes (`add_model`, `remove_model`, …) | `LegacyMcpApplicationAdapter.call` only |

## Injection rules

- Repositories and catalog readers are injected into `ListModelsUseCase` by the host/bootstrap layer.
- No Codex settings, Keychain, or subprocess calls inside this package.
- No new storage imports; use engine ports and existing adapters only.

## Parent wiring (not done in B06 slice)

```python
legacy = as_legacy_adapter(deck)
reads = McpModelReadService(
    list_models=list_models_use_case,
    legacy=legacy,
    presentation=host_presentation,
    catalog_resolver=host_resolver,
)
# Deck.list_added_models -> reads.list_added_models()
# Deck.search_models -> reads.search_models(...)
```

Unconverted behavior stays on `DelegatingLegacyMcpApplicationAdapter` so rollback is a one-line delegate swap.

## Catalog search provenance

Cache-backed `search_models` must not invent `verified: true`. Inject `McpCatalogSearchProvenance` (default `UnverifiedCacheCatalogProvenance`) so the host can supply truthful `source`, `verified`, and `note` when the refresh pipeline has provider metadata.

## Registered model roles

`McpRegisteredModelPresentation.host_role` is the only source for the MCP `role` field. When it returns `None`, the row omits `role` rather than substituting `registration_id`.

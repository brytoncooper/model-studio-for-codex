# MCP client adapter (B06)

This package converges **model read** orchestration for MCP onto `model_deck.engine.model_library` use cases. The root stdio entrypoint (`model_deck_mcp.py`) composes these types after its existing tool argument validation. Registered-model reads use `ListModelsUseCase`; search and endpoint listing use the named legacy adapter. Writes still use the existing root implementation and are not converged.

## Boundary

| MCP tool / read | Path |
| --- | --- |
| `list_added_models` | `ListModelsUseCase` (`collection=registered`) + `McpRegisteredModelPresentation` |
| `search_models` | `ListModelsUseCase` (`collection=catalog`) when `McpCatalogEndpointResolver` maps the endpoint to a cached connection; otherwise `LegacyMcpApplicationAdapter` |
| `list_endpoints` | `LegacyMcpApplicationAdapter` until connection projection exposes MCP-shaped endpoint rows |
| Writes (`add_model`, `remove_model`, …) | Root `Deck.call` invokes its existing raw Deck methods; application-adapter write convergence remains pending |

## Injection rules

- Repositories and catalog readers are injected into `ListModelsUseCase` by the host/bootstrap layer.
- No Codex settings, Keychain, or subprocess calls inside this package.
- No new storage imports; use engine ports and existing adapters only.

## Root entrypoint composition

```python
legacy = as_legacy_adapter(deck)
reads = McpModelReadService(
    list_models=list_models_use_case,
    legacy=legacy,
    presentation=host_presentation,
    catalog_resolver=host_resolver,
)
# Deck.call selects reads after existing TOOL_INDEX validation.
# Deck.search_models/list_endpoints remain raw legacy implementations.
```

The root uses `catalog_resolver=None` to preserve existing search ranking,
total-match counts, prices, provider metadata and selected-account routing.
Cache-backed search is available at the service seam but is not selected by the
root entrypoint. `McpReadError` becomes `DeckError` at this boundary, retaining the
existing agent-visible error envelope. Raw Deck methods do not call the service,
so the named adapter cannot recursively enter read composition.

The root builds a fresh [registry snapshot](registry_snapshot.py) for each
registered-model request. It reuses `RoutingRegistry.load_models`,
`display_name_for`, `Deck.saved_endpoint_name`, `endpoint_billing` and the shared
legacy row-formatting helper. No second parser or repository storage exists.
Opaque UUID connection references include the captured account, base URL and wire
already normalized/resolved by the registry. They distinguish different captured
routes even when two models share an account, as well as unkeyed routes;
names and billing remain presentation data. Snapshot revision 1 is read-only
compatibility metadata, not a revision usable for new application mutations.
Rows and connection mappings are detached at construction. Registered output
retains `billing_note: null` when the legacy formatter supplies no note.

## Source and staged-package loading

The Architecture root entrypoint explicitly adds sibling `python/src` only when
it contains `model_deck/__init__.py`. A packaged entrypoint requires that package
under sibling `vendor` (`Contents/Resources/vendor` in a staged app). Missing
packages or dependencies fail startup with a clear error; no silent fallback to
legacy orchestration is allowed. Source execution does not require PYTHONPATH.

The legacy `build.sh` does not include the new engine package and is not a
supported packager for this Architecture MCP entrypoint. The staged packager
must supply the engine wheel/packages and dependencies from its offline
artifacts. This change does not modify that script or an installed app.

## Tests and limits

From the repository root, run `PYTHONPATH=python/src:. python -m unittest
test_model_deck_mcp`; from `python`, run `PYTHONPATH=src python -m unittest
tests.engine.test_mcp_model_reads`. Root tests exercise real JSON-RPC read
composition, mixed routes, fresh reads after mutations, selected-account
search, validation/error envelopes, explicit source/vendor loading and missing
package refusal. Loader tests stage only temporary fixture resources and send
initialization, not live model or credential requests. They do not qualify a
signed app artifact, runtime installation or complete B06 write convergence.

## Catalog search provenance

Cache-backed `search_models` must not invent `verified: true`. Inject `McpCatalogSearchProvenance` (default `UnverifiedCacheCatalogProvenance`) so the host can supply truthful `source`, `verified`, and `note` when the refresh pipeline has provider metadata.

## Registered model roles

`McpRegisteredModelPresentation.host_role` is the only source for the MCP `role` field. When it returns `None`, the row omits `role` rather than substituting `registration_id`.

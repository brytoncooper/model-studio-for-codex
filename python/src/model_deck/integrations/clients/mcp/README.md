# MCP client adapter (B06)

This package converges registered-model reads and available MCP writes on the
public engine API. The root stdio entrypoint (`model_deck_mcp.py`) composes the
engine path after its existing tool argument validation when explicit isolated
rendezvous and credential paths are present.

## Boundary

| MCP tool / read | Path |
| --- | --- |
| `list_added_models` | `ListModelsUseCase` (`collection=registered`) + `McpRegisteredModelPresentation` |
| `search_models` | `ListModelsUseCase` (`collection=catalog`) when `McpCatalogEndpointResolver` maps the endpoint to a cached connection; otherwise `LegacyMcpApplicationAdapter` |
| `list_endpoints` | `LegacyMcpApplicationAdapter` until connection projection exposes MCP-shaped endpoint rows |
| `add_model` | Authenticated `connections.list` resolution, then `models.register` |
| `set_display_name` | Authoritative `models.list` revision, then `models.rename` |
| `remove_model` | Authoritative `models.list` revision, then `models.remove` |

## Injection rules

- Repositories and catalog readers are injected into `ListModelsUseCase` by the host/bootstrap layer.
- No Codex settings, Keychain, registry writes, or host-file writes inside this package.
- No new storage imports; use engine ports and existing adapters only.

## Engine selection

`MODEL_DECK_ENGINE_RENDEZVOUS_PATH` and
`MODEL_DECK_ENGINE_CREDENTIAL_PATH` must both be absolute paths. When supplied,
the entrypoint loads the descriptor and credential from disk, authenticates to
the Unix socket, and routes registered reads plus add/remove/display-name
mutations through the engine. Supplying only one path fails closed. When both
are absent, the named legacy adapter remains available as the rollback path.

The engine path forwards current revisions and synthesizes fresh bounded
idempotency keys. Its presentation does not guess a role, price, or billing
route from the model name: unknown price is `not listed`, role is omitted, and
billing is attributed to the saved provider connection.

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

In legacy mode, the root builds a fresh [registry snapshot](registry_snapshot.py) for each
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
package refusal. The full-path projection test stages the entrypoint and vendor
packages into a fresh directory with no source `PYTHONPATH`, connects to an
authenticated isolated engine, and exercises add/list/rename/remove through
stdio. It does not qualify a signed app artifact, runtime installation, or live
credentials.

## Catalog search provenance

Cache-backed `search_models` must not invent `verified: true`. Inject `McpCatalogSearchProvenance` (default `UnverifiedCacheCatalogProvenance`) so the host can supply truthful `source`, `verified`, and `note` when the refresh pipeline has provider metadata.

## Registered model roles

`McpRegisteredModelPresentation.host_role` is the only source for the MCP `role` field. When it returns `None`, the row omits `role` rather than substituting `registration_id`.

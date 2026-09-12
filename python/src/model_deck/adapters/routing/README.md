# Registered route resolution

`RegisteredRouteResolver` (`registered.py`) turns a `RouteResolveRequest` into
a `RouteSnapshot` by joining three caller-supplied sources: the model
repository (active registrations), the connection repository (connections),
and the provider route-definition table. It performs no I/O itself and owns no
state beyond those references.

## Contracts

- Repository types come from `engine.connections.ports` and
  `engine.model_library.ports`; request, snapshot, capability, and
  `ExecutionMode` types come from `engine.routing.ports`. The adapter
  redefines none of them.
- `ProviderRouteDefinition` pairs an `ExecutionMode` with the provider's
  capability features and optional capability snapshot ref. The mapping passed
  at construction is defensively copied behind a `MappingProxyType`, so later
  caller-side mutation cannot change resolution.
- The snapshot carries references only (`endpoint_config_ref`,
  `credential_ref`, `capability_snapshot_ref`) plus resolved revisions. Secret
  material and endpoint bodies never pass through this adapter.

## Resolution invariants

- Order is fixed: active registration, then its connection, then the
  provider's route definition. Each miss raises a distinct error:
  `RegistrationNotFoundError`, `RegisteredRouteConnectionNotFoundError`, or
  `RegisteredRouteProviderDefinitionNotFoundError`.
- Capability checks run after the join. A requested snapshot ref that differs
  from the resolved ref raises `UnknownCapabilityError`.
- Only requirements in state `SUPPORTED` are enforced. A required feature
  that is missing or `UNKNOWN` on the route raises `UnknownCapabilityError`;
  one that is `UNSUPPORTED` raises `UnsupportedCapabilityError`.
  Requirements in any other state are skipped.

## Extension

- To serve a new provider, add one `ProviderRouteDefinition` entry keyed by
  its provider id. No subclassing or code change is needed.
- New execution modes, capability states, or snapshot fields belong to
  `engine.routing.ports`; this adapter forwards whatever the ports define.

## Testing

Focused suite: `python/tests/engine/test_registered_route_resolver.py`.

## Limits

- Lookup is a linear scan of `list_registered()` / `list_connections()` per
  resolve, with no caching. If resolution becomes hot, cache above the
  adapter against registration and connection revisions; do not add a cache
  inside it.
- The adapter trusts its repositories: it cannot distinguish a stale listing
  from a deleted registration or connection, and it reports both as
  not-found.

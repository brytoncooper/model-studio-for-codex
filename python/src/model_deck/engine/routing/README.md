# Routing (B12)

`model_deck.engine.routing` defines the application port for resolving a
registration to the current provider/connection/model snapshot. The package
holds the contract only; concrete resolvers live in
`python/src/model_deck/adapters/routing/` (for example
`RegisteredRouteResolver`). The sessions and runs use cases depend on the
`RouteResolver` Protocol defined here, never on a concrete implementation.

## Files

- `__init__.py` — re-exports the public types from `ports.py`.
- `ports.py` — enums, frozen dataclasses, the `RouteResolver` Protocol, and
  the four domain errors.

## Public contracts

### Enums

- `ExecutionMode` — `chat_completions`, `responses`, `custom`.
- `CapabilityTriState` — `unknown`, `supported`, `unsupported`.

### Records

- `CapabilityFeature` (frozen, slots) — `name`, `state`. Used as the
  element type of the immutable `CapabilityFeatureTuple`.
- `ContinuationScope` (frozen, slots) — `connection_id`, `provider_id`,
  `provider_model_id`, `execution_mode`, `handle`. The shape is declared
  here for the session and runs packages to share, but no record produced by
  the routing port carries one — see Limitations.
- `RouteSnapshot` (frozen, slots) — `registration_id`, `registration_revision`,
  `connection_id`, `connection_revision`, `provider_id`, `provider_model_id`,
  `execution_mode`, and optional `endpoint_config_ref`, `credential_ref`,
  `capability_snapshot_ref`, `capability_features`.
- `RouteResolveRequest` — `registration_id`, optional
  `capability_snapshot_ref`, optional `capability_requirements`.

### Resolver protocol

`RouteResolver.resolve_active_registration(request) -> RouteSnapshot`.

### Errors

- `RegistrationNotFoundError(LookupError)` — no registration matches the id.
- `RegistrationRemovedError(LookupError)` — registration was removed before
  resolution.
- `UnknownCapabilityError(ValueError)` — request referenced an unrecognized
  capability.
- `UnsupportedCapabilityError(ValueError)` — request required a capability
  the registration cannot satisfy.

## Invariants

- Every record is a frozen dataclass with `slots=True`; mutation, optional or
  otherwise, is rejected.
- `RouteSnapshot.credential_ref`, `endpoint_config_ref`, and
  `capability_snapshot_ref` are opaque references (`ref:` prefixed) or
  canonical UUIDs; the package never carries raw credential values.
- `RouteSnapshot` exposes only opaque references and revisions; concrete
  endpoint configuration or credential material stays inside the resolver.
- `CapabilityFeatureTuple` is immutable; resolvers return the same tuple shape
  every call.
- Resolvers are injected by callers; this package does not construct one and
  does not reach into storage or provider code.
- Errors are distinct types. Resolvers raise the specific `LookupError` or
  `ValueError` subtype rather than returning `None` or a single generic
  exception.

## How to extend

Implement `RouteResolver` and pass it into the sessions and runs use cases
(which already type-hint it through `model_deck.engine.routing.ports`). The
adapters package owns the actual storage-backed implementation; do not add
storage or provider imports to this package. The resolver's return type is
`RouteSnapshot` only — it never returns a `ContinuationScope` and never sets
the opaque `handle`. Any future population of a continuation handle is an
adapter-level concern, not a port contract.

## Tests

Run from the Architecture `python` directory:

```sh
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m unittest tests.engine.test_registered_route_resolver
```

This test exercises the SQLite-backed `RegisteredRouteResolver` against the
connections and model library repositories, including the registration-removed
path. Port contract coverage lives in `tests.engine.test_session_run_ports`,
which imports directly from this package.

## Limitations

- The port declares one method. Bulk lookup, listing, or filtering across
  registrations is not part of this contract.
- The package does not evaluate capabilities. Resolvers implement their own
  policy for `capability_snapshot_ref` and `capability_requirements`; this
  port enforces only the type shapes and error vocabulary.
- `ContinuationScope` is declared here for shared use, but the routing port
  does not produce one. The resolver returns a `RouteSnapshot`; whether (and
  how) any future code path populates the opaque `handle` is an
  adapter-level decision, not a routing-package contract.
- The package owns only the contract. Persistence, cache invalidation,
  transport, and provider IO are out of scope.

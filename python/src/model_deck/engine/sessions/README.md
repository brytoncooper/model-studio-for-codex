# Sessions (B12)

`model_deck.engine.sessions` owns the application port and use cases for session
records bound to a model registration. It backs the wire methods
`engine.v1.sessions.create`, `engine.v1.sessions.get`, and
`engine.v1.sessions.select_model` registered in
`python/src/model_deck/engine/dispatch.py`.

## Files

- `__init__.py` — re-exports the public types from `ports.py` and `use_cases.py`.
- `ports.py` — frozen dataclasses, `SessionRepository` Protocol, and the three
  domain errors.
- `use_cases.py` — the three use case classes plus parameter validation and
  route/continuation helpers.

## Public contracts

### Records and commands

- `SessionRecord` (frozen, slots) — `session_id`, `registration_id`, `revision`,
  optional `host_context_ref`, optional `continuation_scope`.
- `CreateSessionCommand` — `registration_id` plus optional `host_context_ref`.
- `GetSessionCommand` — `session_id`.
- `SelectModelCommand` — `session_id`, `registration_id`, `expected_revision`,
  optional `continuation_reset` (defaults to `False`).

### Continuation scope

`ContinuationScope` is defined in `model_deck.engine.routing.ports` and reused
here. It carries `connection_id`, `provider_id`, `provider_model_id`,
`execution_mode`, and an opaque `handle`. The use cases do not construct or
update a `ContinuationScope` themselves; the repository adapter is the only
writer. The session port today neither captures a handle from the resolver nor
hands one off to the runs package.

### Repository protocol

`SessionRepository` declares three methods:

- `create(CreateSessionCommand) -> SessionRecord`
- `get(GetSessionCommand) -> SessionRecord`
- `select_model(SelectModelCommand) -> SessionRecord`

### Errors

- `SessionNotFoundError(LookupError)` — `get` did not find the session.
- `SessionRevisionConflictError(ValueError)` — `expected_revision` did not match
  storage state on `select_model`.
- `SessionActiveRunConflictError(ValueError)` — `select_model` was rejected
  because a run is still in flight against this session.

### Use cases

- `CreateSessionUseCase(repository, route_resolver)` — validates params,
  resolves the registration through `RouteResolver`, calls `repository.create`,
  returns `{ session_id, revision }`. The resolved `RouteSnapshot` is consumed
  only as a validation probe; it is not converted into a continuation scope
  here.
- `GetSessionUseCase(repository)` — validates `session_id`, calls
  `repository.get`, returns the full `SessionRecord` payload (omitting
  `host_context_ref` and `continuation_scope` when they are unset).
- `SelectSessionModelUseCase(repository, route_resolver)` — validates params,
  re-reads the session, resolves the new registration, enforces continuation
  compatibility against the stored scope (unless `continuation_reset=True`), and
  calls `repository.select_model`. Returns `{ session_id, revision }`. The use
  case never replaces the stored scope itself; it only signals intent via the
  `continuation_reset` flag on the command.

## Invariants

- Unknown params are rejected explicitly (`_reject_unknown_keys`).
- UUID fields use canonical 8-4-4-4-12 spelling; case is preserved.
- `host_context_ref` accepts either a canonical UUID or an opaque reference
  matching `^ref:[a-z][a-z0-9._-]{0,120}$` and must be at most 128 characters.
- `expected_revision` must be a non-negative integer.
- `select_model` without `continuation_reset` is rejected when the new route
  differs from the stored scope on `connection_id`, `provider_id`,
  `provider_model_id`, or `execution_mode`. Equal scopes proceed unchanged.
  When `continuation_scope` is `None` the compatibility check is a no-op.
- `CreateSessionUseCase` calls `route_resolver.resolve_active_registration`
  before mutating the repository. A resolver failure (`RegistrationNotFoundError`,
  `RegistrationRemovedError`, capability errors) leaves the repository untouched.
- `SessionRecord` is frozen. Revisions start at 1 on `create`, do not change
  on `get`, and increment on a successful `select_model`. The repository
  adapter is the only writer; reads and idempotent replays do not bump the
  revision.

## How to extend

Implement `SessionRepository` against your storage adapter. Inject the concrete
repository and the existing `RouteResolver` adapter into the three use cases.
The package does not import storage, transport, or provider code; the use cases
only reach into `model_deck.engine.routing.ports`. To expose a new wire method
on sessions, add the operation to `_OPERATION_CATALOG` in
`python/src/model_deck/engine/dispatch.py` alongside its params/result schemas
under `contracts/engine.v1/methods/`.

## Tests

Run from the Architecture `python` directory:

```sh
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m unittest tests.engine.test_session_use_cases
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m unittest tests.engine.test_session_run_ports
```

`test_session_use_cases` exercises validation, repository wiring, continuation
compatibility, and unknown-key rejection across the three use cases.
`test_session_run_ports` covers the immutable shape of `SessionRecord` and the
cross-package port vocabulary.

## Limitations

- No active-run gating lives in this package. `SessionActiveRunConflictError`
  is raised by the repository adapter when it observes an in-flight run; the
  use case only propagates it.
- The use cases depend on the caller supplying a `RouteResolver` that raises
  on missing or removed registrations; returning `None` is not supported.
- Storage-side revision conflicts are surfaced through the repository contract;
  the use case does not attempt CAS retries.
- The session port does not capture or hand off a continuation handle today.
  Compatibility checking between a new `RouteSnapshot` and the stored
  `ContinuationScope` lives in `select_model`; capture of an opaque `handle`
  and any hand-off into provider work belong to the repository adapter (and,
  eventually, the runs package), not to this package's use cases.
- The package does not handle transport framing, persistence, or provider IO.

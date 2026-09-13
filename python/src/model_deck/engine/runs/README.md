# Runs (B12)

`model_deck.engine.runs` owns the application port and use cases for model runs
bound to a session and route. It backs the wire methods
`engine.v1.runs.start`, `engine.v1.runs.get`, `engine.v1.runs.cancel`, and
`engine.v1.runs.submit_tool_result`, registered in
`python/src/model_deck/engine/dispatch.py`. Run-scoped event subscriptions
live in `python/src/model_deck/adapters/events/live_replay.py` and use the
`RunEventReplayPort` defined here.

## Files

- `__init__.py` — re-exports the public types from `ports.py` and `use_cases.py`.
- `ports.py` — frozen dataclasses, state and outcome enums, the
  `RunRepository` and `RunEventReplayPort` Protocols, all wire-result types, and
  the limit constants.
- `use_cases.py` — `StartRunUseCase`, `GetRunUseCase`, `CancelRunUseCase`,
  `SubmitToolResultUseCase`, the in-process `RunApplicationCoordinator`, and
  parameter validation helpers.

## Fresh runtime composition

Each call to `build_engine_server(...)` in
`python/src/model_deck/bootstrap.py` composes a brand-new in-memory runtime:
fresh `EngineServer`, fresh `RunApplicationCoordinator`, fresh live replay
buffer, and a fresh snapshot of any caller-supplied provider routes. The
runtime is independent of every other runtime that has been or will be
composed, even when multiple calls share a `state_root`, an `artifact_root`,
a `socket_root`, or a `ProviderExecutionPort` reference.

The fresh runtime is laid over the durable state that already lives at the
supplied `state_root`. `EngineServer.start()` invokes the run repository's
durable recovery exactly once per start, after acquiring the exclusive
engine instance lock and before publishing rendezvous: claimed non-terminal
runs terminalize as `INTERRUPTED`, unclaimed `ACCEPTED` runs come back as
`dispatchable_requests`, and terminal runs stay terminal. Two engines
sharing `state_root` cannot coexist — the lock prevents a second runtime
from recovering another instance's work and aborts startup if recovery
fails. Calling `start()` again on an already running instance does not
repeat recovery and does not reset in-memory handles.

The fresh snapshot of the caller-supplied `provider_route_definitions`
applies for the lifetime of the runtime. Subsequent mutations of the
caller's mapping, lists, or `CapabilityFeature` instances do not reach the
running engine; bootstrap reconstructs each entry from scratch inside
`_snapshot_provider_routes` and validates every `capability_snapshot_ref`
against the same UUID / `ref:` opaque reference rules used by
`engine.connections.use_cases` before any state is created. A reference is
accepted only when it is a non-empty string of at most 128 characters that
is either a canonical UUID (`uuid.UUID(value)` valid and
`str(parsed).casefold() == value.casefold()`) or a `ref:` opaque reference
matching `^ref:[a-z][a-z0-9._-]{0,120}$`. Empty, malformed, or overlong
references, and non-string types, are rejected with `ValueError` and no
state is created on disk.

## Public contracts

### States and outcomes

- `RunState` — `ACCEPTED`, `RUNNING`, `WAITING_FOR_TOOL`, `CANCELLING`,
  `COMPLETED`, `FAILED`, `CANCELLED`, `INTERRUPTED`. The last four are
  terminal; see `TERMINAL_RUN_STATES`.
- `ActiveRunState` — the non-terminal subset used for `expected_state` on
  transitions.
- `TerminalOutcome` — pairs `COMPLETED`, `FAILED`, `CANCELLED`,
  `INTERRUPTED`. `TerminalResult` carries `outcome` plus optional `error`.

### Records and commands

- `RunRequest` — `run_id`, `session_id`, `client_request_id`, `idempotency_key`,
  `route_snapshot`, `NormalizedRunInput`, and a tuple of `ToolDefinition`.
- `RunRecord` — `run_id`, `session_id`, `state`, `client_request_id`,
  `registration_id`, `route_snapshot`, `principal_id`,
  `authorized_host_context_ref`, optional `terminal_result`, `last_sequence`.
- `NormalizedRunInput` and `ToolDefinition` carry messages and advertised function
  definitions for a run start. `ToolCallDescriptor` describes a later emitted call.
- `RunAdmissionKey` — `principal_id`, `operation_id`, `idempotency_key`.
- `StartRunCommand` — `admission_key`, `request_hash`, `session_id`,
  `client_request_id`, `registration_id`, `route_snapshot`, `input`, `tools`,
  optional `authorized_host_context_ref`.
- `RunAdmissionResult` — `run` plus `dispatch_required` flag.
- `ClaimDispatchCommand` — `run_id`, `dispatch_token`.
- `AppendApplicationEventCommand` / `AppendApplicationEventResult` —
  monotonic sequence appends keyed on `expected_state` / `new_state`.
- `CancelRunCommand` / `CancelRunResult` — `run_id`, `idempotency_key`, and
  the committed event when present.
- `SubmitToolResultCommand` / `SubmitToolResultResult` — caller authorization,
  `(run_id, call_id, idempotency_key)` idempotency, and
  `provider_submission_required`.
- `CompleteTerminalCommand` / `CompleteTerminalResult` — final-state writes.
- `GetRunCommand` — `run_id`.
- `RestartRecoveryResult` — `observed_at`, `dispatchable_requests`,
  `interrupted_run_ids`.

### Events

- `ApplicationRunEvent` — `kind`, `run_id`, `session_id`, `sequence`,
  `event_schema_version`, `observed_at`, `payload`.
- `ProviderRunEvent` — `kind`, `run_id`, `observed_at`, `payload`.
- `ApplicationEventPublisher.publish_application_event(event)`.
- `ProviderRunEventSink.publish_provider_event(event)`.

### Provider boundary

The bootstrap caller owns the lifecycle of any injected `ProviderExecutionPort`.
Bootstrap holds only a reference and dispatches `start` / `submit_tool_result`
/ `request_cancel`; teardown of provider resources (network connections,
worker pools, cached state) belongs to the caller. The matching test
`tests.engine.test_provider_bootstrap.ProviderBootstrapTests.test_cancel_and_restart_keep_application_recovery_and_caller_ownership`
asserts `provider.closes == 0` across the lifetime of an injected engine
instance, confirming bootstrap never invokes `ProviderExecutionPort.close`
implicitly. Calling `build_engine_server(...)` again with the same
`state_root` composes a fresh in-memory runtime over the existing durable
state (see "Fresh runtime composition" above) and issues a new dispatch
handle for the same provider reference; the caller decides when to swap,
reload, or close that reference.

- `ProviderExecutionPort.start(request, sink) -> ProviderRunHandle`.
- `ProviderRunHandle.submit_tool_result(call_id, result)` returns
  `SubmitToolResultProviderResult` with `SubmitToolResultProviderOutcome`
  (`ACCEPTED` / `REJECTED`).
- `ProviderRunHandle.request_cancel(*, deadline)` returns
  `CancelProviderRunResult` with `request_accepted` and
  `ProviderCancelTerminationStatus` (`UNKNOWN`, `CONFIRMED`, `UNCONFIRMED`).

### Replay port

`RunEventReplayPort` covers `subscribe`, `ack`, `read_available`,
`unsubscribe`; the page types are `EventReplayPage`,
`ReplaySubscriptionHandle`, and `EventReplayAckResult`. Outcomes are
`EventReplayOutcome.DELIVERED`, `SLOW_READER`, `RESUME_UNAVAILABLE`.

### Limits

Exposed as module constants:

- `SUBSCRIBER_QUEUE_MAX_EVENTS = 256`
- `SUBSCRIBER_QUEUE_MAX_BYTES = 1_048_576`
- `RUN_LIVE_REPLAY_MAX_BYTES = 8_388_608`
- `RUN_LIVE_REPLAY_MAX_DURATION_SECONDS = 60`
- `ADMISSION_RECORD_RETENTION_MIN_HOURS = 24`

### Errors

`RunNotFoundError`, `RunStateConflictError`, `RunDispatchClaimError`,
`RunTerminalConflictError`, `RunAdmissionRequestHashConflictError`,
`ToolResultIdempotencyConflictError`, `RunAuthorizationMismatchError`,
`ToolCallNotOutstandingError`.

### Use cases

- `StartRunUseCase(run_repository, session_repository, route_resolver,
  provider, coordinator)` — validates params, captures `principal_id`,
  resolves the registration, admits or replays, then dispatches via the
  coordinator.
- `GetRunUseCase(run_repository)` — returns the run summary.
- `CancelRunUseCase(run_repository, coordinator, *, cancel_deadline)` —
  records intent, requests cancellation from the provider when possible, and
  terminalizes when the provider confirms.
- `SubmitToolResultUseCase(run_repository, coordinator)` — applies caller
  authorization, idempotency, and outstanding-call checks, then forwards to
  the provider handle on the first submission.
- `RunApplicationCoordinator(run_repository, *, event_publisher=None)` —
  in-process handle registry; `recover_after_restart(provider, observed_at)`
  replays dispatchable requests and publishes the durable interrupted events
  returned by `RunRepository.recover_after_restart`.

## Invariants

When bootstrap configures a run repository, `EngineServer.start()` invokes its
durable recovery only after acquiring the exclusive engine instance lock and
before starting the listener or publishing rendezvous. Construction does not
recover runs, failed lock acquisition cannot recover another instance's work,
and duplicate `start()` calls on an already running instance do not repeat
recovery. Recovery failure aborts startup and releases the lock. The timestamp
is current UTC at startup. Bootstrap deliberately calls the repository, not
the coordinator's dispatching recovery helper: claimed nonterminal work becomes
interrupted, terminal work stays terminal, and unclaimed accepted work remains
available for explicit dispatch. Startup never retries provider execution.

- Identity for replay is `(principal_id, operation_id, idempotency_key)`; the
  run's `request_hash` is compared separately and raises
  `RunAdmissionRequestHashConflictError` on mismatch.
- `claim_dispatch` is one-time. After a successful claim the run is never
  `ACCEPTED` again, so recovery never has to re-admit claimed work.
- Recovery returns unclaimed `ACCEPTED` runs as `dispatchable_requests`
  (their `RunRequest` is reconstructed from the captured `RouteSnapshot` and
  durable input/tools) and terminalizes every claimed non-terminal run as
  `INTERRUPTED`. Recovery does not re-resolve active registrations.
- Provider terminal events (`run.completed`, `run.failed`, `run.cancelled`,
  `run.interrupted`) end the run exactly once; non-terminal events append with
  strictly increasing `sequence`.
- `submit_tool_result` validates `principal_id` and `host_context_ref`
  against the run, then asks the repository to record the result. The
  repository decides whether the call still needs provider submission: an
  exact replay of a previously stored receipt returns
  `provider_submission_required=False` (including when the run is already
  terminal, because the prior receipt stands); a new submission against the
  same `(run_id, call_id, idempotency_key)` triple with a different payload
  raises `ToolResultIdempotencyConflictError`. Only when the repository
  signals `provider_submission_required=True` does the use case forward to
  the provider handle; on `REJECTED` or missing handle the run is terminalized
  as `INTERRUPTED` and the use case raises `RunStateConflictError`.
- Live replay is bounded by `RUN_LIVE_REPLAY_MAX_BYTES` and
  `RUN_LIVE_REPLAY_MAX_DURATION_SECONDS`. Stale cursors return
  `RESUME_UNAVAILABLE`; slow readers get `SLOW_READER` without affecting other
  subscribers. Stream content is never regenerated or persisted for replay.
- Validation: `input.messages` ≤ 256 items, `tools` ≤ 128 items, `call_id` and
  `tool_name` ≤ 128 chars, `idempotency_key` ≤ 128 chars, `client_request_id`
  ≤ 64 chars. The combined `input` + `tools` payload is bounded to 1 MiB.
- `capability_snapshot_ref` accepts canonical UUIDs or `ref:` opaque refs;
  supplying tools triggers `CapabilityFeature("tools", SUPPORTED)` on route
  resolve.

## How to extend

Implement `RunRepository` against durable storage; it must enforce identity,
revision CAS, sequence monotonicity, terminal-once, retention
(`ADMISSION_RECORD_RETENTION_MIN_HOURS`), and the recovery contract above.
Implement `ProviderExecutionPort` to dispatch provider work and emit events
through `ProviderRunEventSink`. Implement `RunEventReplayPort` for run-scoped
event delivery using the bounded limits above. Wire everything through the
`RunApplicationCoordinator` and the four use cases; do not bypass them to
mutate runs directly. To expose new run-side wire methods, add the operation to
`_OPERATION_CATALOG` in `python/src/model_deck/engine/dispatch.py` together
with the matching schemas under `contracts/engine.v1/methods/`.

## Tests

Run from the Architecture `python` directory:

```sh
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m unittest tests.engine.test_run_use_cases
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m unittest tests.engine.test_session_run_ports
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m unittest tests.engine.test_engine_run_dispatch
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m unittest tests.engine.test_run_startup_recovery
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m unittest tests.engine.test_provider_bootstrap
```

`tests.engine.test_provider_bootstrap` lives at
`python/tests/engine/test_provider_bootstrap.py`. It is the focused test
module for the optional `provider_execution` /
`provider_route_definitions` injection pair accepted by
`build_engine_server`, the `_snapshot_provider_routes` defense against
caller mutation, and the `ProviderExecutionPort` lifecycle ownership
contract. Run it from the Architecture `python` directory with the same
`PYTHONPATH=src` prefix as the rest of the suite; it builds a real engine
over a Unix socket using the shared deterministic provider fixture.

`test_run_use_cases` exercises validation, admission replay, dispatch and
recovery paths, cancellation flow, tool submission idempotency, and provider
event mapping. `test_session_run_ports` covers the port vocabulary,
constants, and `RunRepository` docstrings. `test_engine_run_dispatch` brings
up a real `build_engine_server` to validate end-to-end B12 dispatch over a
Unix socket with the deterministic provider fixture.

## Limitations

- No scheduler; provider work runs in the caller's thread or process. The
  `RunApplicationCoordinator` keeps provider handles in an in-process map and
  cannot survive a restart. Bootstrap recovery interrupts claimed work without
  provider retry; the coordinator's explicit recovery helper can dispatch
  previously unclaimed accepted requests.
- Storage is not implemented in this package. Authorization checks
  (`principal_id`, `host_context_ref`) are enforced at the repository layer;
  the use cases only forward.
- The 60-second live replay buffer is in-memory only and is not regenerated
  on demand. Subscribers reconnect and resume from a fresh sequence; older
  cursors return `RESUME_UNAVAILABLE`.
- The deterministic provider fixture exercises the engine wiring and is not
  evidence of live provider parity. The package makes no claim about model
  catalog contents, billing, or uptime.
- A new tool submission whose underlying call has already been completed is
  rejected. Replaying the exact prior tool-result receipt for the same
  `(run_id, call_id, idempotency_key)` triple is allowed and returns the
  recorded receipt without re-dispatching to the provider, even when the run
  is already terminal.
- `recover_after_restart` is the supported durable recovery flow. Startup
  sequencing belongs to `EngineServer` and bootstrap; graceful shutdown and
  restart-time configuration reload are separate concerns.

### Tool definitions: unreleased v1 correction

The original unreleased branch incorrectly admitted tool calls as run-start
advertisements, contrary to the existing `authorized_tool` vocabulary. Start
now accepts name, optional description, object `input_schema`, and explicit
`host_execution_required`. Names are nonempty, unique and at most 128 characters.
Schemas are finite bounded JSON, validated offline as Draft 2020-12. External
references and external base IDs are rejected; local fragment references are
allowed. This does not execute tools or grant host authority.

`tool_definitions.py` owns parsing and detached wire encoding. The public
`model_deck_contracts.validate_tool_input_schema` helper owns offline JSON Schema
validation and returns a detached dictionary; only the contracts package imports
the schema library. The provider gets
`ToolDefinition`, while emitted calls retain call IDs, names and arguments.
Definition content participates in admission hashing and durable recovery.
SQLite stores `tools_json` as `{schema_version: 1, definitions: [...]}`. Legacy
empty arrays remain readable as no definitions. Legacy nonempty arrays and
unknown versions raise `StoredToolDefinitionsCompatibilityError`; recovery
rolls back without changing state or dispatching them. No arguments-to-schema
conversion or live database migration is attempted.

Extend focused port, admission and SQLite tests when changing these rules.
Schema resources must be regenerated from canonical contracts and checked for
byte equality. Provider-specific conversion belongs in provider adapters.

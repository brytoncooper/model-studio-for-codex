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
  `route_snapshot`, `NormalizedRunInput`, and a tuple of `ToolCallDescriptor`.
- `RunRecord` — `run_id`, `session_id`, `state`, `client_request_id`,
  `registration_id`, `route_snapshot`, `principal_id`,
  `authorized_host_context_ref`, optional `terminal_result`, `last_sequence`.
- `NormalizedRunInput` and `ToolCallDescriptor` carry the messages and tool
  descriptors for a run start.
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
```

`test_run_use_cases` exercises validation, admission replay, dispatch and
recovery paths, cancellation flow, tool submission idempotency, and provider
event mapping. `test_session_run_ports` covers the port vocabulary,
constants, and `RunRepository` docstrings. `test_engine_run_dispatch` brings
up a real `build_engine_server` to validate end-to-end B12 dispatch over a
Unix socket with the deterministic provider fixture.

## Limitations

- No scheduler; provider work runs in the caller's thread or process. The
  `RunApplicationCoordinator` keeps provider handles in an in-process map and
  cannot survive a restart — recovery is the only durable retry path.
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
- `recovery_after_restart` is the only supported recovery flow. Process-level
  supervisor behavior, graceful shutdown, or restart-time configuration reload
  belong outside this package.

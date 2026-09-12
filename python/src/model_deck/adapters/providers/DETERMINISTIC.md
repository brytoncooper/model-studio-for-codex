# Deterministic provider fixture

`DeterministicProviderExecutionPort` (`deterministic.py`) is a scripted,
network-free `ProviderExecutionPort` for tests and isolated qualification. A
fixed tuple of script steps drives exactly the provider events a scenario
needs: start, content deltas, tool requests, usage observations, terminal
outcomes, crash variants, and cancellation holds. Provider id defaults to
`com.modeldeck.provider.deterministic` and execution mode to `CUSTOM`.

## Contracts

- Port, event, handle, and outcome types come from `engine.runs.ports`;
  `ExecutionMode`, `CapabilityTriState`, and `RouteSnapshot` come from
  `engine.routing.ports`. The fixture redefines none of them.
- `start` validates the request's route snapshot before doing anything: a
  provider-id or execution-mode mismatch raises
  `DeterministicRouteMismatchError`, and a script containing
  `EmitToolRequested` against a route whose `tools` capability is not
  `SUPPORTED` raises `DeterministicCapabilityRejectedError`.
- Starting the same run id twice raises `DeterministicDuplicateStartError`.
  The request is recorded and available via `recorded_request(run_id)`.

## Script execution invariants

- `emit_next()` (aliased as `advance()`) publishes one event per call and
  returns `True`, except `HoldCancellation`, which only arms the cancel hold
  and continues to the next step. With `auto_advance`, `start` drains the
  script until terminal or blocked on a tool result.
- One tool call may be outstanding at a time. While one is open, `emit_next`
  returns `False` without consuming the script. `submit_tool_result` accepts
  only the outstanding call id; a repeated identical result is accepted
  idempotently, anything else is rejected.
- Terminal emission is exactly once. Any second terminal, publish-after-
  terminal, or `emit_next` after terminal raises
  `DeterministicRunClosedError`. A script that ends without a terminal step
  raises `DeterministicScriptExhaustedError`.
- `CrashInterrupted` / `CrashFailed` emit their terminal immediately, like
  their non-crash counterparts with fixture error payloads. Terminal errors
  are normalized to `{code, retryable, message?}` and non-dict or malformed
  errors raise `DeterministicProviderError`.
- Cancel before terminal records the deadline and, unless a hold is armed,
  emits `run.cancelled` confirmed. With `HoldCancellation` in the script the
  cancel stays unconfirmed until the script reaches its own terminal. Cancel
  after terminal reports confirmed only if the terminal was `run.cancelled`.
- `observed_at` defaults to a fixed timestamp on every step, so runs are
  byte-reproducible unless a step overrides it.

## Extension

New scenarios are new step tuples, not new classes. Add step types only for
behavior the engine must distinguish (a new event kind or gating rule); pure
payload variation belongs in existing steps' fields.

## Testing

Focused suite: `python/tests/engine/test_deterministic_provider.py`.

## Limits

- This is a test double, never a real provider: no network, no model, no
  retries, no timeouts, and no clock. Timeouts and scheduling belong to the
  engine under test.
- Single outstanding tool call per run handle, and in-memory recorded
  requests. `start` creates a fresh handle for each distinct run id, so one
  port (and one configured script) supports multiple runs; starting the same
  run id twice raises. Use separate ports for different scripts. The port
  keeps no locks, so thread safety is not established.

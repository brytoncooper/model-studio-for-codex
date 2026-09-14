# Cursor provider execution and isolated V2 binding (B14)

## V2 composition

`CursorProfile` and `compose_cursor_profile` bind the existing coordinator and
`CursorProcessRuntime` to the pinned Cursor SDK broker. The non-secret profile
carries an application-owned credential command, selected `cursor/<sdk-model>`
id, SDK Python path/version, and isolated project/state paths. Loading verifies
that the interpreter contains `cursor-sdk==1.0.31`.

The broker enables only request-supplied custom MCP tools. Cursor-native file,
shell, web, settings, external MCP, and subagent features remain disabled, so
Codex owns approvals and actual tool execution. Cancellation terminates only
the owned broker process group and reports remote termination as unknown.
Missing SDK usage or cost is omitted, not invented as zero.

## Ownership

The Cursor provider package owns its coordinator, process-runtime adapter,
profile composition, tests, and this guide. Its current implementation files
are:

- `python/src/model_deck/integrations/providers/cursor/__init__.py`
- `python/src/model_deck/integrations/providers/cursor/configuration.py`
- `python/src/model_deck/integrations/providers/cursor/coordinator.py`
- `python/src/model_deck/integrations/providers/cursor/process_runtime.py`
- `python/src/model_deck/integrations/providers/cursor/PROCESS_RUNTIME.md`
- `python/tests/provider_cursor/test_configuration.py`
- `python/tests/provider_cursor/test_coordinator.py`
- `python/tests/provider_cursor/test_process_runtime.py`
- `docs/providers/cursor.md`

The application CLI composes that public package and the V2 build copies the
retained compatibility broker into its isolated artifact. Vendor SDK details
remain outside the engine, and the provider does not reach into another
provider's private implementation or storage.

## Flow

`CursorExecutionCoordinator` implements the frozen B12
`ProviderExecutionPort`. For each `start(RunRequest, sink)` call it builds
an immutable, detached `CursorStartRequest` (run id, session id,
connection id, provider model id, input messages tuple, tools tuple,
optional continuation handle) carrying deep copies, so later caller
mutation cannot affect the dispatched request. The tools tuple carries
`ToolDefinition` advertisements (name, nested input schema,
host-execution flag, optional description) detached to the runtime seam,
distinct from emitted tool-call IDs. The route snapshot carries
no continuation handle today, so the adapter leaves that field unset for
the later real-SDK binding to fill.

A start whose route snapshot carries a `provider_id` other than the
cursor provider id is rejected before any registration or dispatch.
The coordinator pre-registers the run handle before invoking the
injected `CursorSdkRuntimePort`, so synchronous SDK callbacks during
`start` are safe, then binds the returned owned session. A second start
for the same run id raises `CursorDuplicateStartError` and never
redispatches. A start failure removes the registration and re-raises,
never retaining a partial handle.

SDK events (`CursorSdkEvent`) arrive on the per-run callback and are
translated to B12 `ProviderRunEvent` with the fixed request run id, the
injected UTC clock string, and a detached JSON payload. Only the exact
B12 provider kinds are accepted, and every payload is validated
against its kind shape before reaching the sink: `run.started` and
`run.cancelling` take `None` or an empty object, `content.delta`
takes exactly `{channel: str, delta: str}`, `tool.requested` takes
exactly `{tool_call: {call_id, tool_name, arguments}}`,
`usage.observed` takes exactly `{usage: object}`, and each terminal
kind takes `{terminal_result: {outcome, error?}}` with the outcome
matching the kind and any error a structured object. Unknown keys
and malformed payloads raise `CursorProtocolError`, and string bounds, usage UUIDs/timestamps, and nullable fields follow the frozen schema. Every
payload must serialize as finite JSON (`allow_nan=False`), so
NaN/Infinity anywhere in the payload is rejected. The first event must be `run.started`
(exactly once); exactly one terminal event is allowed and nothing may
follow it. At most one tool call may stay outstanding: a duplicate or
different `tool.requested` while one is outstanding, and a
`run.completed` while one is outstanding, are protocol errors. Unknown
or malformed events raise `CursorProtocolError` before reaching the
sink.

Tool results go only to the currently outstanding call. The first
accepted forward clears the outstanding call and stores a detached
receipt; an exact replay returns the prior accepted result without a
second SDK call, while a changed replay or a wrong call id returns
`REJECTED`. An SDK request reusing an already-completed tool call ID is
rejected before changing outstanding-call state. Cancel delegates to the owned session only; an already
terminal or closed handle answers `request_accepted=False` with
`UNKNOWN` and makes no SDK call. The adapter never synthesizes a
terminal event: B12 `CancelRunUseCase` terminalizes a `CONFIRMED` cancel
that emits nothing, while a synchronous terminal callback from the
runtime is preserved. A terminal event closes its owned SDK session
exactly once; a terminal that arrives synchronously before
`runtime.start` returns defers the close until the session binds, and a handle closed before bind leaves the open set once bound.
Per-handle and coordinator state moves under `threading.RLock`.
No state lock is held while calling the sink, runtime, or session. Events
are validated and queued under the per-handle lock. One publisher drains
that queue in order outside the lock. Concurrent or reentrant callbacks
return after enqueueing; sink failures surface on the draining callback
thread after all queued events have been attempted. Delivery failures do
not roll state back, resurrect a run, or prevent terminal cleanup.

Close rejects new session operations immediately, but defers the owned
session's actual close until already-reserved submit/cancel operations
return. Those operations retain their SDK outcome, including an accepted
submission racing with close. Close is nonblocking and cannot forcibly
terminate a hung SDK operation. A synchronous terminal callback during an
SDK operation also defers physical close until that operation returns.
Terminal IDs retain the same handle and reject redispatch during the
configured replay window (300 seconds by default, starting at harvesting).
Expiration is checked on coordinator reads and starts. After expiration,
the same ID may be started again.

Parallel runs keep isolated handles and sessions, and per-run cleanup
never touches other runs. `close()` is a full coordinator shutdown for
tests and process teardown. A failing session close does not skip the
remaining sessions; shutdown attempts each close and then raises the first
error. Close is attempted exactly once even when the SDK close itself fails.

No SDK import occurs in the engine interpreter. Credential material is resolved
only when a run starts and is sent to the owned SDK broker over private stdin.
The V2 Codex bridge supplies role-preserving conversation input and
host-authorized tools.

## Tests

`python/tests/provider_cursor/test_coordinator.py` exercises the seam
against an injected fake runtime/session facade: request mapping and
detachment, structural conformance to `ProviderExecutionPort` and
`ProviderRunHandle`, synchronous start callbacks, start-failure cleanup,
duplicate start with no redispatch, tool suspension with exact replay,
mismatch, wrong call, and receipt isolation, confirmed/unconfirmed and
terminal cancel races, terminal close exactly once, post-terminal,
unknown, and malformed rejection, parallel isolation with cancel
targeting one run, provider-id mismatch with no dispatch, nested
NaN/Infinity rejection, per-kind payload shape tables, and
concurrent terminal/close/tool-submit/cancel races.

Run:

```bash
cd '/Users/brytoncooper/Documents/Model Deck Architecture/python' \
  && PYTHONPATH=src /opt/homebrew/bin/python3.12 \
  -W error::ResourceWarning -m unittest tests.provider_cursor.test_coordinator
```

```bash
cd '/Users/brytoncooper/Documents/Model Deck Architecture' \
  && /opt/homebrew/bin/python3.12 scripts/architecture_check.py \
  --roots python/src/model_deck/integrations/providers/cursor --fail-on-warnings
```

## Limits

The isolated V2 route has live proof for Composer 2.5, tool-driven editing,
checks, follow-up context, local cancellation, token usage, and owned-process
cleanup. The compatibility broker is still packaged from the retained legacy
root; moving its SDK install/update mechanics wholly behind the new package is
remaining original B14 work. Provider-native continuation handles remain B15;
V2 follow-up preserves context through Codex and starts a new SDK generation on
the same route.

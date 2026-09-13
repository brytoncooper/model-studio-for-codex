# Cursor process adapter

`CursorProcessRuntime` implements the coordinator's `CursorSdkRuntimePort` using
an injected process with `events.get(timeout=...)`, `tool_result(call_id, output)`,
and `close()`. It owns event conversion, tool alias checks, forwarding, and
asynchronous cleanup. It imports neither the legacy root modules nor Cursor SDK.

## Composition contracts

```python
PreparedCursorRun(payload: Mapping[str, Any], tool_aliases: Mapping[str, str])
CursorProcessRuntime(
    process_factory: Callable[[Mapping[str, Any]], CursorProcessPort],
    prepare_payload: Callable[[CursorStartRequest], PreparedCursorRun],
    normalize_usage: Callable[
        [CursorStartRequest, tuple[dict, ...], dict | None],
        Iterable[Mapping],
    ],
)
```

The compatibility root injects the existing `CursorSdkProcess` and existing
payload helpers. Prepared payload is detached and forwarded unchanged, including
model, API key, tools, message, reasoning, and service tier. Preparation supplies
trusted missing context: instructions, host/thread metadata, credential scope,
format/tool-choice settings and aliases. It must preserve encrypted-input and
compaction checks. The original broker still owns SDK model/reasoning/Fast
selection; this adapter does not reconstruct prompt envelopes or configure SDKs.
Aliases map wire tool names to host-authorized `ToolDefinition.name` values.
Opaque continuation is refused; it is not silently resumed or billed again.

`normalize_usage` receives the request, intermediate **whole usage events**, and
the final whole done/error event (or `None` after local shutdown). It returns
frozen engine usage records. Composition must use existing reconciliation:
intermediate usage sums, nonempty final usage replaces that sum, cached/reasoning
subsets are not added twice, and final charged cost is recorded once. The callback
also supplies timestamps and registration attribution unavailable in the start
request. Usage is emitted once before termination, not once per intermediate
observation. At most 1024 intermediate observations and 64 output records are
accepted. Before SDK startup, no usage event is fabricated.

## Event and lifecycle guarantees

Only SDK `started` produces `run.started`. Text/thinking become text/reasoning
content deltas, split at the frozen 65536-character limit. Tool requests restore
the authorized alias. SDK `done: finished` completes only without an outstanding
tool; other done statuses interrupt. SDK errors use a fixed failure event, even
before startup. The coordinator permits early failure/interruption, but still
rejects early text, tools, cancellation events, or successful completion.

The legacy process represents EOF as an error event, so this adapter reports that
as failure; it does not infer a distinct exit status from error-message text.
Exceptions from factories, SDK events, callbacks, or cleanup are not echoed as
provider diagnostics. No retries are performed.

A session owns one event-pump thread and at most one cleanup thread. Callbacks,
process writes and cleanup run outside the session state lock. Close schedules
legacy process cleanup and returns promptly. Cancel returns accepted/UNKNOWN;
terminating a local broker is not provider cancellation confirmation. Once local
cleanup returns, the event pump emits interruption unless already terminal.
The deadline argument does not impose a provider termination guarantee or shorten
legacy cleanup. Repeated close/cancel does not close twice or emit two terminals.
Tool acceptance means local forwarding returned successfully, not an SDK receipt.

The coordinator reserves in-flight tool submissions and queues SDK callbacks
until outcome bookkeeping is complete. It drains in order outside locks. A
completion received during accepted forwarding can then complete; completion
received during rejected/failed forwarding raises a protocol error and leaves
the tool outstanding. SDK tool payloads retain their wrapper for validation;
published engine tool events use flat call_id/tool_name/arguments fields.

## Checks and remaining work

```sh
PYTHONPATH=src python -B -m unittest tests.provider_cursor.test_coordinator tests.provider_cursor.test_process_runtime
```

Run from `python/`. Tests use fake injected queues/processes plus one temporary
SQLite engine integration; no SDK installation, subprocess, network, credentials,
or live app is exercised. Root compatibility wiring and live qualification remain
separate work. A hung injected cleanup can leave its daemon thread running; the
adapter cannot prove termination beyond the injected process's contract.

# OpenAI-compatible streaming boundary

This package decodes provider stream bytes and checks normalized run-event
ordering. It does not make HTTP requests, resolve credentials, translate host
history, choose fallback protocols, or prove live provider compatibility.

## Public contracts

`SseDecoder(max_event_bytes=...)` accepts byte chunks through `feed` and returns
tuples of detached `SseMessage` values. Call `finish` at end of input to detect
truncation. LF, CRLF and CR line endings, fragmented UTF-8/BOM, multiline data,
comments and persistent event IDs are supported. The byte bound applies per
event, not to the sum of complete events supplied in one chunk.

The OpenAI `[DONE]` marker sets `done`; subsequent fields are rejected.
An event-type-only message can produce an empty-data event. This is the
adapter's tested behavior, not a claim of complete browser EventSource parity.
`decode_json_object` requires an object and rejects malformed/nonfinite JSON.

`ProviderEventTerminalValidator(run_id)` consumes existing `ProviderRunEvent`
objects through `submit`. It requires start before output or successful
completion, while allowing `run.failed` and `run.interrupted` to terminate an
attempt that could not start. It enforces matching run identity, terminal
exclusivity, and no events after termination. Provider tool events carry the
engine's flat `{call_id, tool_name, arguments}` payload. Only one tool call may
be outstanding; `mark_tool_result` requires its matching ID. Completing while
a tool call remains outstanding is rejected. `finish_segment` distinguishes
terminal completion from an intentional wait for a tool result.

## Invariants and extension

Each decoder and validator belongs to one stream/run segment. Keep wire parsing
separate from request routing and host conversion. Add vendor normalization in
an adapter that produces the engine-owned event types; do not add provider
branches to the core. Preserve bounded buffering and explicit malformed-stream
failures when extending the decoder.

## Verification

From `python/`, use Python 3.12 with `PYTHONPATH=src`:

```sh
python3.12 -m unittest tests.provider_openai_compatible.test_sse
```

The focused suite and independent probes cover fragmentation, byte boundaries,
UTF-8, JSON overflow, event IDs, terminal markers and cancellation ordering.
Actual HTTP translation, billing-safe fallback, and host/provider parity remain
separate integration requirements.

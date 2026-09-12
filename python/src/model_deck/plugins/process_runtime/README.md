# Process runtime

Owns one explicitly injected subprocess and its bounded stdio channel. Lifecycle
payload/state rules belong to `lifecycle_session`; framing belongs to
`stdio_codec`. This package adds process ownership, response matching, provider
schema checks, bounded queues, and cleanup. It does not install plugins or own
engine runs, credentials, billing, or provider selection.

## Public API

- `ProcessRuntimeConfig(argv, package_dir, timeout_s=5.0, max_frames=16,
  max_stderr_bytes=65536, max_pending_requests=16)` specifies the executable,
  working directory, and limits. No shell, downloads, or discovery.
- `ProcessRuntime(config).spawn()` starts exactly one child. A runtime cannot
  respawn after closing.
- `run_hello(session, nonce)`, `run_activation(session)`, and
  `run_drain(session, deadline_ms)` preserve the synchronous lifecycle API.
  Hello and activation must succeed on this runtime with the same session object.
- `provider_channel()` returns a `ProviderChannel` only after that activation.
  Its `activation_id` identifies the binding. Constructing a facade directly
  confers no authority: every operation checks the runtime's bound activation.
- `channel.request(method, params, timeout_s=None)` returns the validated result
  object. `ProviderMethod` enumerates the exact allowed wire names:
  `plugin.v1.provider.start`, `.submit_tool_result`, `.cancel`, `.resume`, `.ack`.
  The equivalent complete strings are accepted. No generic broker dispatch is
  exposed. Params and results use the frozen provider schemas; no token or
  context fields are added. `ack` is a request with a `credit` result.
- `channel.receive_event(timeout_s=...)` returns detached `provider.event` params,
  or `None` when its polling interval elapses. Events are notifications with no
  request ID or response. The polling interval may be zero.
- `close()` wakes pending callers, terminates/reaps only the recorded child,
  and closes its pipes. `stderr_bytes_drained` reports bounded retained stderr
  bytes; stderr contents are never included in errors or runtime repr.

## Concurrency and failure rules

One stdout reader starts at spawn and owns the persistent decoder throughout
hello, activation, provider traffic, and drain. It validates each complete batch
before releasing results. Unknown or duplicate response IDs, boolean/noninteger
IDs, invalid envelopes, unsolicited worker requests, and unrecognized
notifications fail closed. Complete and partial trailing frames are not dropped.
Provider events are forbidden before activation acceptance.

Concurrent provider requests have separate pending IDs. Complete writes are
serialized independently of response waiting; cancellation, tool-result
submission, and event receipt can run concurrently. Lifecycle calls remain
exclusive with each other. Draining refuses new start/resume admission while
allowing existing-run traffic. Inactive or failed sessions invalidate the channel.

Each command has one monotonic deadline covering write-lock acquisition, writing,
and reply waiting. Timeout or invalid transport/protocol data fails the channel
and closes its owned child, releasing all waiters. There is no retry, restart,
or billed resubmission. A valid cancel result preserves the distinction between
`accepted` and optional `confirmed`.

Pending requests default to 16 and are capped by the validated configuration
(maximum 1024). Queued provider events are capped at 256 events and 1 MiB of
encoded frames, whichever comes first. Overflow closes the channel rather than
silently dropping events. The codec independently caps each frame at 1 MiB.
`max_frames` remains a per-lifecycle-exchange bound, not a provider lifetime cap.
Stderr drains continuously with bounded retention. Child termination waits at
most `min(timeout_s, 5)` seconds before kill, followed by a bounded reap and
reader-thread joins.

## Extension boundary and limitations

The provider proxy must enforce route/run ownership, declared resume capability,
event sequencing, credits, outstanding tool calls, and exactly-one-terminal
transitions. A successfully authenticated channel does not authorize arbitrary
connection credentials. Credential brokerage is a separate binding and is not
implemented here. Events may precede their command reply; the proxy must correlate
and validate them before exposing them to engine consumers.

Shutdown does not claim provider-side cancellation confirmation or freeze later
session mutation. Run interruption/accounting belongs to the engine proxy. A
poll timeout is not a command failure. Returned payloads belong to the caller;
no raw child error body is propagated. Error tracebacks suppress underlying
schema/codec exception rendering; they are not a general local-variable scrubber.

## Tests

From `python/`, run the isolated modules:

```sh
PYTHONPATH=src python -B -m unittest tests.plugins.test_process_runtime tests.plugins.test_process_provider_channel
```

Tests launch only owned synthetic Python fixture children with outer watchdogs.
Coverage includes lifecycle compatibility, binding, interleaved and partial
frames, reversed concurrent replies, command/poll deadlines, EOF, close wakeups,
queue/pending limits, schema failures, and secret-safe error rendering. No live
app, network, installation, or credential configuration is exercised.

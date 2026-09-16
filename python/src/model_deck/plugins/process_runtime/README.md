# Process runtime

Owns one explicitly injected subprocess and its bounded stdio channel. Lifecycle
payload/state rules belong to `lifecycle_session`; framing belongs to
`stdio_codec`. This package adds process ownership, response matching, invocation
and provider schema checks, bounded queues, and cleanup. It does not install
plugins or own engine runs, credentials, billing, authority, or provider
selection.

## Public API

- `ProcessRuntimeConfig(argv, package_dir, timeout_s=5.0, max_frames=16,
  max_stderr_bytes=65536, max_pending_requests=16, heartbeat_interval_s=5.0,
  heartbeat_timeout_s=2.0, heartbeat_max_missed=3)` specifies the executable,
  working directory, and limits. No shell, downloads, or discovery. The three
  heartbeat fields are validated like the rest; `heartbeat_interval_s=0.0`
  turns heartbeats off entirely.
- `ProcessRuntime(config, *, allowed_broker_methods=(),
  broker_request_handler=None, worker_loss_listener=None).spawn()` starts
  exactly one child. A runtime cannot respawn after closing. Trusted
  composition supplies the broker allowlist and handler; the worker cannot
  widen either one.
- `health()` returns a `WorkerHealth`, and `heartbeat_supported` reports
  whether the child answers the heartbeat method at all. `last_failure` is the
  error that ended the runtime; `last_listener_failure` is set only when a
  worker-loss listener raised.
- `run_hello(session, nonce)`, `run_activation(session)`, and
  `run_drain(session, deadline_ms)` preserve the synchronous lifecycle API.
  Hello and activation must succeed on this runtime with the same session object.
- `invocation_channel()` returns an `InvocationChannel` only while that
  activation is active. Import the type and `BrokerRequestHandler` from
  `model_deck.plugins.process_runtime.invocation_channel`.
- `invocation_channel.invoke(operation_id, input, broker_context, *,
  timeout_s=None)` sends canonical `plugin.v1.invoke` and returns its validated
  result. The caller supplies authority-derived broker context. The runtime does
  not mint an invocation handle and rejects a context whose activation id does
  not match its bound activation.
- A broker handler receives `(bound_activation_id, method, params)`. The first
  value comes from the authenticated runtime, never from worker-echoed context.
  The handler derives the authoritative activation/plugin/invocation identity
  and applies grants and revocation state. Params and results are detached and
  validated against the inventory-owned broker schemas.
- `provider_channel()` returns a `ProviderChannel` only after activation. Its
  `activation_id` identifies the binding. Constructing a facade directly confers
  no authority: every operation checks the runtime's bound activation.
- `provider_channel.request(method, params, timeout_s=None)` returns a validated
  result. `ProviderMethod` permits exactly `plugin.v1.provider.start`,
  `.submit_tool_result`, `.cancel`, `.resume`, and `.ack`.
- `provider_channel.receive_event(timeout_s=...)` returns detached
  `provider.event` params, or `None` when polling elapses. The polling interval
  may be zero.
- `close()` wakes pending callers, terminates and reaps only the recorded child,
  and closes its pipes. `stderr_bytes_drained` reports bounded retained stderr
  bytes; stderr contents are never included in errors or runtime repr.

## Concurrency and failure rules

One stdout reader starts at spawn and owns the persistent decoder throughout
hello, activation, invocation, provider traffic, and drain. It validates each
complete batch before releasing results. Unknown or duplicate response IDs,
duplicate in-flight worker request IDs, invalid envelopes, broker methods absent
from the inventory, and unrecognized notifications fail closed. The only two
exceptions are the heartbeat's, described under **Worker liveness**: a response
id the runtime itself abandoned when a beat missed its deadline, and an error
reply to a beat. Complete and partial trailing frames are not dropped. Provider events and broker requests are
forbidden before activation acceptance.

Worker broker requests use their own correlation-id direction and never enter
the supervisor's outbound pending map. The reader validates and enqueues them;
two bounded daemon workers call the trusted handler without holding the reader
condition or write lock. An independent deadline thread tracks every queued or
running broker request, so callback saturation cannot keep the subprocess open
after a request expires. Only the constructor allowlist can reach the handler.
An unlisted request, missing handler, handler rejection, invalid result, duplicate
id, queue overflow, or response framing failure returns or records a fixed
bounded failure and closes the runtime without echoing request input or
invocation handles.

Concurrent supervisor requests have separate pending IDs. Complete writes are
serialized independently of response waiting. Lifecycle calls remain exclusive
with each other. Draining refuses new invocation and provider start/resume
admission while allowing broker calls and existing-run provider traffic.
Inactive or failed sessions invalidate every channel.

Each command has one monotonic deadline covering write-lock acquisition, writing,
and reply waiting. Timeout or invalid transport/protocol data fails the channel
and closes its owned child, releasing all waiters. There is no retry, restart,
or billed resubmission. A valid provider cancel result preserves the distinction
between `accepted` and optional `confirmed`.

Pending requests in each direction default to 16 and are capped by the validated
configuration (maximum 1024). Queued provider events are capped at 256 events and
1 MiB of encoded frames, whichever comes first. Overflow closes the channel
rather than silently dropping events. The codec independently caps each frame at
1 MiB. `max_frames` applies to canonical invoke exchanges as well as the existing
lifecycle wire names; it is not a provider lifetime cap. Stderr drains
continuously with bounded retention. Child termination waits at most
`min(timeout_s, 5)` seconds before kill, followed by a bounded reap and thread
joins.

Broker callbacks have monotonic response deadlines and late results cannot write
after runtime closure. The independent deadline owner terminates and reaps the
child and clears correlation state even when every callback worker is stuck.
Python cannot forcibly terminate a callback already executing in a thread, so
callback code must also bound its own external work. A stuck callback may remain
alive in its daemon thread until it returns, but it cannot block the stdout
reader or subprocess cleanup. This is not operating-system containment of
handler code.

## Worker liveness

A runtime reports a lost worker exactly once, to the optional
`worker_loss_listener` passed to the constructor. The call happens at the tail
of the stop path, after the child is terminated and reaped, holding neither the
write lock nor the runtime condition. A listener that raises has its exception
swallowed and recorded on `last_listener_failure`; nothing else changes. With
no listener the runtime behaves exactly as it did before.

An owner-initiated `close()` is not a loss and reports nothing. Neither does a
lifecycle result that the session rejects: the supervisor is tearing down a
worker that is still alive, which is not a reason to restart anything. Every
other stop reports, with these `WorkerLossCode` values:

- `exited` — stdout reached EOF, or a write found the child already gone. The
  event carries the reaped `exit_code`, including the negative signal number of
  a child that was killed.
- `timeout` — a per-exchange deadline elapsed.
- `unresponsive` — the heartbeat verdict below.
- `killed` — the broker-deadline owner terminated the child, or the runtime
  failed the worker for a protocol, framing, id, or transport reason.

Once an activation is active, a daemon thread beats at the child every
`heartbeat_interval_s` with the canonical inventory method
`plugin.v1.heartbeat` and params `{"activation_id": ...}`, using the same
exchange path as every other request and its own `heartbeat_timeout_s`
deadline. The monitor skips a beat rather than queueing behind other work: any
exchange still inside its own deadline is itself evidence of life, and so is a
draining or not-yet-active session. Any answer at all marks the worker
`healthy` — the runtime does not police the heartbeat result body, because a
child that answers has proven its read/write loop is alive. A reply that
arrives after its beat's deadline is dropped instead of failing the runtime on
an unknown response id, and it does not clear the missed count: only a beat
answered inside its own deadline does.

A beat that times out counts as missed and moves health to `unresponsive`. At
`heartbeat_max_missed` consecutive misses the runtime checks the process: a
child that has exited is reported `exited`, and a child still running is
terminated, reaped, and reported `unresponsive`. `health()` returns the
`WorkerHealth` record — `starting` before the first answer, `healthy`,
`unresponsive` while beats are being missed and after an unresponsive verdict,
and `dead` once the child is gone for any other reason, including after an
ordinary `close()`.

A child that answers a heartbeat with a JSON-RPC error is alive but does not
implement the method. That is not a loss: the worker stays `healthy`, beats
stop for the life of that runtime, and `heartbeat_supported` becomes False.
`WorkerHealth` has no field for that distinction, so it is reported on the
runtime instead. The heartbeat is also disabled, before any beat is sent, when
the worker's own activation id cannot satisfy the frozen heartbeat params
schema; a working child is never failed over that.

Liveness does not restart anything. A runtime still cannot respawn after
closing, and a replacement activation is a new runtime owned by the activation
lifecycle.

## Extension boundary and limitations

The invocation authority owner validates broker context plugin and invocation
handles against the runtime-bound activation id, current grants, and revocation
generation. The process runtime validates shape and activation binding but does
not confer broker authority.

The provider proxy enforces route/run ownership, declared resume capability,
event sequencing, credits, outstanding tool calls, and exactly-one-terminal
transitions. A successfully authenticated provider channel does not authorize
arbitrary connection credentials. Events may precede their command reply; the
proxy correlates and validates them before exposing them to engine consumers.

Shutdown does not claim provider-side cancellation confirmation or freeze later
session mutation. Run interruption/accounting belongs to the engine proxy. A
poll timeout is not a command failure. Returned payloads belong to the caller;
no raw child error body is propagated. Error tracebacks suppress underlying
schema/codec exception rendering; they are not a general local-variable scrubber.

The existing hello, activate, and drain exchanges retain their historical
`plugin.v1.lifecycle.*` wire names for fixture compatibility in this slice. The
operations inventory names those methods `plugin.v1.*`; reconciling that legacy
mismatch requires separate qualification. New invocation uses the canonical
inventory name `plugin.v1.invoke`.

## Worker health records (`health.py`)

`health.py` is a stdlib-only module of frozen records. It imports nothing from
the rest of the codebase, performs no I/O, reads no clock, and touches no
process; every timestamp is a `time.monotonic()` reading passed in by the
caller, and every ledger method returns a new value.

- `WorkerHealthState` — `starting`, `healthy`, `unresponsive`, `dead`.
- `WorkerHealth(state, last_heartbeat_monotonic, consecutive_missed)`, where the
  heartbeat is None until the worker first answers.
- `WorkerLossCode` — `malformed_eof`, `timeout`, `unresponsive`, `exited`,
  `killed`. This is a runtime distinction, not the domain error vocabulary.
- `WorkerLossEvent(activation_id, failure_code, at_monotonic, exit_code=None)`,
  with `activation_id` None for a worker lost before activation binding.
- `WorkerLossListener`, a protocol with `on_worker_lost(event) -> None`.
- `RestartPolicy(max_attempts, initial_backoff_s, multiplier, max_backoff_s,
  reset_after_healthy_s)`, validated at construction, plus
  `backoff_for_attempt(policy, attempt)`.
- `RestartLedger(attempts, next_allowed_at_monotonic, last_failure_code,
  gave_up)` with the pure methods `record_failure(policy, now,
  failure_code=None)`, `record_healthy(policy, now)`, and `can_attempt(now)`.

`WorkerHealthState`, `WorkerHealth`, `WorkerLossCode`, `WorkerLossEvent`, and
`WorkerLossListener` are live: see **Worker liveness** above for exactly which
code the runtime emits for which loss. `WorkerHealth` carries no
heartbeat-unsupported field, so the runtime reports that separately as
`heartbeat_supported`.

**`RestartPolicy`, `backoff_for_attempt`, and `RestartLedger` are still not
called by anything.** They belong to the activation lifecycle (U13), not to
this package: a runtime never restarts itself and still cannot respawn after
closing. Nothing in this package constructs a policy or a ledger, and no
composition path passes a `worker_loss_listener` yet, so in the assembled
system a lost worker is reported to nobody until a supervisor subscribes.

## Tests

From `python/`, run the isolated modules:

```sh
PYTHONPATH=src python -B -m unittest tests.plugins.test_process_runtime tests.plugins.test_process_provider_channel tests.plugins.test_process_invocation_channel
```

The health records have their own module, which launches no child at all:

```sh
PYTHONPATH=src python -B -m unittest tests.plugins.test_worker_health_records
```

Liveness has its own module, whose children really do exit, get killed, answer
beats, reject the heartbeat method, and answer too late:

```sh
PYTHONPATH=src python -B -m unittest tests.plugins.test_worker_heartbeat
```

Tests launch only owned synthetic Python fixture children with outer watchdogs.
Coverage includes lifecycle compatibility, binding, interleaved and partial
frames, reversed concurrent replies, command/poll deadlines, EOF, close wakeups,
queue/pending limits, broker request round trips, callback isolation, schema
failures, unsolicited callback saturation, oversized broker responses, and
secret-safe error rendering. No live app, network, installation, or credential
configuration is exercised.

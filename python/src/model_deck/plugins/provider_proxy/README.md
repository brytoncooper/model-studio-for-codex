# External provider execution proxy

`ExternalProviderExecution` implements the engine's public `ProviderExecutionPort`
and returns `ProviderRunHandle` objects. It owns run/route/session-to-worker-handle
binding, tool-call state, one event pump and ordered per-run delivery workers. `process_runtime.ProviderChannel`
owns authenticated transport; runtime lifecycle retains process ownership.

Construct one proxy per authenticated activation with the contributed provider ID
and an explicit aware-datetime clock. Call `start` with an admitted `RunRequest`
and a `ProviderRunEventSink`. Route IDs, model, mode, capabilities and endpoint
reference are encoded to the frozen provider wire schema. Run input is forwarded
as the engine's normalized conversation items
(`engine.v1/vocabulary.schema.json#/definitions/normalized_input_item`), already
tagged by admission and passed to the worker unchanged; the proxy neither
reshapes nor reinterprets message or tool-history values. An engine-issued
`continuation_scope` rides beside the run request and is forwarded verbatim when
the admitted request carries one. Credentials are never included. A missing endpoint reference is rejected as `unsupported_capability`
before dispatch; no synthetic reference is generated.

## Invariants

- Reserve run identity before sending start, allowing concurrent starts and
  notifications preceding their responses. Buffered events are checked against
  the returned handle before release. Handles cannot move between runs or be
  reused in an activation. Provider and activation identities cannot change.
- Validate outbound and inbound schemas. Worker sequence starts at zero, advances
  exactly by one, and must equal the enclosed event sequence. Only version 1
  events are accepted. The engine assigns its independent public sequence.
- Wire `tool.requested.event.tool_call` is translated into the engine provider
  event's flat call_id/tool_name/arguments payload. Other event payloads retain
  their existing engine-port shape.
- Workers cannot emit `run.accepted`, select another run/session, use an
  unadvertised tool or repeat a tool call ID. Only outstanding results are sent;
  an explicit rejected result remains outstanding. Completed success with an
  unresolved tool, mismatched terminal outcome or repeated terminal is rejected.
- Acknowledge cumulatively only after sink processing. Worker-reported credit
  never expands the local 256-event/1-MiB queue. Pre-response buffers share these
  bounds, including callback/ACK payloads currently in flight; runtime independently
  bounds transport buffering. Identity tombstones
  cap the proxy at 256 total admitted runs and 256 tool call IDs per run.
- Cancellation acceptance and confirmed termination remain separate. An absent
  `confirmed` is UNKNOWN; false is UNCONFIRMED. Deadline conversion uses the
  injected clock, caps wire milliseconds at 60,000, and makes no expired request.
  Waiting for the state lock consumes the deadline, which is rechecked before
  channel dispatch.
- Protocol/transport failure interrupts every still-active owned run once, with
  a fixed payload that never includes worker exception text. Close stops the
  proxy and interrupts active runs; it never closes or terminates the runtime.
  No automatic retry, resume, restart or billed resubmission exists.

## Extension and limitations

Only use public engine ports, provider channel methods and contracts validation.
Extend wire methods through an agreed schema/API contract first. This adapter is
ordered within each run: sequence, tool and terminal state are reserved under
the state lock before callbacks. Sink callbacks and channel requests execute
outside that lock. Each admitted run has one daemon delivery worker, bounded by
the 256-run lifetime cap. A slow sink delays only its own deliveries and ACKs;
other runs and close can progress. Tool-result forwarding reserves the call ID
until the response arrives, preventing duplicate submissions; that run's next
events wait for the result decision without blocking other runs.

Interruption reserves each terminal once and queues its callback for that run.
Close stops admission and the pump without waiting for blocked sink callbacks;
queued interruption follows any callback already in progress. Callbacks cannot
be forcibly terminated, so a blocked callback's worker remains until it returns.
Reentrant close from a terminal callback does not enqueue a second terminal.
The injected clock must remain callable and return an aware datetime.

Broker credential authorization/binding is a separate unfinished integration.
The route wire vocabulary does not transmit engine registration/connection
revision fields; the proxy retains the admitted route object locally. This
package alone does not prove external archive installation, model selection,
credential isolation, or built-in/external provider conformance parity. No B18
completion or live qualification claim follows from these fixture tests.

## Tests

From Architecture `python/`, with an isolated Python 3.11+ environment containing
the contract dependencies:

```sh
PYTHONPATH=src python -B -m unittest tests.plugins.test_provider_proxy
```

The controllable channel fixture covers schema parity, concurrent early events,
run/session/handle isolation, activation changes, tools, cancellation states and
deadlines, duplicate/gapped sequences, invalid terminals, transport failure,
queue byte/event limits, slow-sink isolation, reentrant close/tool submission,
blocked channel waits, deadline contention, and no-dispatch/no-retry paths. A
real RunApplicationCoordinator/SQLite fixture proves waiting-for-tool, tool-result
forwarding and exact replay, return to running, and terminal completion. It does not launch or
change installed applications.

# OpenAI-compatible provider execution

## Purpose and ownership

`execution.py` implements the engine `ProviderExecutionPort` for an
OpenAI-compatible HTTP endpoint. It owns request composition, streaming HTTP
lifetimes, Responses-to-chat fallback, tool-result continuation, and local
cancellation. `events.py` owns the adjacent conversion from Responses-style
wire events into engine `ProviderRunEvent` values. `configuration.py` owns
the V2 profile loader, composer, and the credential-command subprocess
resolver. The SSE decoder, JSON envelope helpers, and segment-terminal
validator remain in the same package.

Endpoint settings and credentials remain outside this package. Callers inject
an `EndpointResolver(endpoint_config_ref, connection_revision)` and a
`CredentialResolver(credential_ref)`. The resolved endpoint value contains
only host, port, TLS selection, path prefix, wire mode, and optional vendor ID.
The credential resolver returns the secret used for the Authorization header.

## Public contract

`OpenAICompatibleExecutionPort.start(request, sink)` returns an
`OpenAICompatibleRunHandle` immediately after starting a daemon reader thread.
The handle implements `submit_tool_result(call_id, result)` and
`request_cancel(deadline=...)` from the engine provider port.

`WireMode.RESPONSES` always posts to `<path_prefix>/responses`.
`WireMode.CHAT_COMPLETIONS` always posts to
`<path_prefix>/chat/completions`. `WireMode.AUTO` tries Responses first and
falls back to chat only when the Responses request returns HTTP 404, 405, or
501 before any body is read. The selected wire is cached by endpoint-config
reference and captured connection revision. A 200 response, any body read or
decode failure, and every other status are never retried. Each tool result
starts a new HTTP segment rather than retrying the prior one.

The reader uses the package SSE decoder. Chat chunks pass through
`ChatStreamTranslator`; both native Responses and translated chat events then
pass through `ResponsesEventTranslator`. It emits flat provider events:

- text and reasoning become `content.delta` with `delta` and `channel`;
- completed function calls become `tool.requested` with `call_id`,
  `tool_name`, and decoded `arguments`;
- terminal usage emits one `usage.observed` per available non-negative
  integer token counter (input_tokens, output_tokens,
  input_tokens_details.cached_tokens) with a `usage` payload of
  `{run_id, session_id, registration_id, connection_id,
  provider_model_id, observed_at, units, unit_kind}` (see Identity-aware
  usage below). Missing counters are absent; no zero-fill, no cost fields;
- terminal response outcomes become exactly one engine terminal event.

## V2 profile loading and composer

`OpenAICompatibleProfile.load(path)` parses a non-secret JSON profile
document (schema_version 1) into an immutable record. The profile carries
`provider_id` (reverse-domain), `provider_name`, `connection_id` (UUID),
`provider_model_id`, `display_name`, opaque `endpoint_config_ref`,
`credential_ref`, optional `capability_snapshot_ref`, an `endpoint`
block (https `base_url`, `wire_mode` ∈ {auto, responses,
chat_completions}, `vendor_id`), a `credential_command` block
(absolute `executable`, `args` list of strings, `timeout_ms` 1..30000),
and a non-empty `billing_description`. Literal secret fields are
rejected at load time.

`compose_openai_compatible_profile(profile, *, post_stream,
endpoint_resolver, credential_resolver=None, clock=None,
request_timeout=None)` wires the profile into an
`OpenAICompatibleExecutionPort` and returns a
`ProviderRouteDefinition` whose declared capabilities are
`tools = SUPPORTED`, `parallel_tool_calls = UNSUPPORTED`, and
`execution_mode = RESPONSES`. The `capability_snapshot_ref` is passed
through unchanged.

`subprocess_credential_resolver(command)` returns a `CredentialResolver`
that runs the profile's `credential_command` with `subprocess.run`,
`shell=False`, captured stdout, the profile's bounded `timeout_ms`,
no inherited environment, and rejects empty or newline-only secrets.
Failures raise a fixed sanitized error with no command, secret, or
stderr contents exposed. `endpoint_resolver_from_records(records)`
returns an `EndpointResolver` that accepts only matching
`(endpoint_config_ref, connection_revision)` records and yields the
parsed host, port, TLS flag, path, wire mode, and vendor id.

## Identity-aware usage normalization

`ResponsesEventTranslator.__init__(run_id, clock, identity=...)` accepts
a `RunIdentity` carrying `session_id`, `registration_id`,
`connection_id`, and `provider_model_id`. Production execution wires
identity from `RunRequest.session_id` and
`RunRequest.route_snapshot.{registration_id, connection_id,
provider_model_id}`. Constructing the translator without identity
remains accepted so direct unit fixtures can target the failure path,
but any terminal provider usage envelope seen without identity raises
`OpenAICompatibleEventError` so identity gaps cannot silently slip into
production. Identity fields are validated as non-empty strings with no
CR or LF bytes.

On a terminal Responses envelope carrying a `response.usage` object, the
translator emits one `usage.observed` ProviderRunEvent per available
non-negative integer counter:

- `input_tokens` from `usage.input_tokens`;
- `output_tokens` from `usage.output_tokens`;
- `cached_tokens` from `usage.input_tokens_details.cached_tokens`.

Each event payload is `{"usage": {"run_id": ..., "session_id": ...,
"registration_id": ..., "connection_id": ..., "provider_model_id": ...,
"observed_at": ..., "units": float, "unit_kind": ...}}`. `observed_at`
comes from the injected `clock()`. `units` is the float of the
integer counter; `unit_kind` is the matching token kind. The payload
is shaped to satisfy the engine `run_event_usage_observed` schema and
flows through `RecordUsageUseCase` reconciliation. Non-integer or
negative counter values are rejected; counters absent from the wire
envelope remain absent (no zero-fill, no synthetic `total_tokens`,
no cost fields).

## Tool and lifecycle invariants

Parallel tool calls are rejected before endpoint, credential, or HTTP access.
Only one provider call may be outstanding. A matching tool result is serialized
once as canonical `function_call_output`, appended after the completed output
items retained from the prior segment, and sent in the next request. An exact
repeat is accepted idempotently without another request; a different result or
call ID is rejected.

The run emits `run.started` once across all segments. A wire
`response.completed` with an outstanding function call ends that HTTP segment
without completing the engine run. Malformed, truncated, over-budget, or
non-200 streams fail with fixed messages that do not include endpoint secrets,
request bodies, response bodies, or exception representations. Assistant
refusals, images, and mixed supported/unsupported output content are rejected
until the normalized history contract can represent them without loss.

Cancellation closes the currently owned response locally. The immediate
`CancelProviderRunResult` is unconfirmed because closing a client connection
does not prove that the remote provider stopped work. The reader publishes one
local terminal after the close resolves; a concurrent provider terminal wins
the terminal race and later events are suppressed. The configured HTTP timeout
is bounded to 3600 seconds, and every acquired response is closed on success or
failure. Cancellation is checked immediately before opening an automatic chat
fallback and again after acquiring it; a fallback acquired after cancellation
is closed without reading or publishing its content.

## Extension

Add provider-specific request behavior to the existing request mapper and add
wire-envelope normalization to `events.py`. Keep credentials, host state,
registration lookup, persistence, and engine lifecycle policy outside this
package. A new fallback condition needs evidence that the first request was
rejected before body processing and did not begin billable generation.

## Tests

From `python/`:

```sh
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.provider_openai_compatible.test_configuration \
  tests.provider_openai_compatible.test_events \
  tests.provider_openai_compatible.test_execution
```

The fixtures inject deterministic resolvers and byte streams; they make no
network calls. They cover exact request bodies, text, tools, continuation
history, identity-aware usage normalization, safe failures, cancellation,
fallback counts and cache keys, fixed wire modes, profile validation,
credential-command subprocess behavior, and injection through the real
engine composition seam.

## Limitations

The V2 profile loader validates non-secret JSON documents only; it does
not fetch, refresh, or rotate secrets. The composer relies on the caller
to inject production `EndpointResolver` and `CredentialResolver`
implementations; the bundled `subprocess_credential_resolver` is the
non-secret command runner for profile-defined credentials. Bootstrap does
not yet wire this slice into live host configuration or vendor parity,
and the bootstrap test proves only the existing provider-injection seam
with deterministic HTTP fixtures. Closing a response cannot prove remote
cancellation or prevent provider-side billing already in progress.
Provider-native encrypted reasoning continuation and compaction remain
owned by the later continuation slice.

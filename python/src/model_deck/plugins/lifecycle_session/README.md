# `model_deck.plugins.lifecycle_session`

Pure in-memory session state for one supervised plugin activation. This
package is the B18 slice responsible for the
`created -> hello_verified -> active -> draining -> inactive` flow with a
terminal `failed` state, independent of any transport or process adapter.

## Purpose

- Prepare detached `hello.params`, `activate.params`, `cancel.params` and
  `drain.params` payloads validated against the frozen
  `contracts/plugin.v1/lifecycle` schemas.
- Accept inbound `hello.result`, `activate.result` and `drain.result`
  mappings, schema-validate them, and advance the state machine.
- Bind plugin identity session-side: the hello result must report the
  exact expected plugin id and version before activation is allowed.
- Gate call admission on `active`; draining stops new admissions while
  outstanding calls are resolved or cancelled only by explicit caller
  actions, never by automatic execution.
- Keep the activation token opaque: it appears only inside the prepared
  activation request and never in `repr`, errors or events.

## Ownership

`model_deck.plugins.lifecycle_session` is owned by the B18 lifecycle
session slice. It depends only on the standard library and
`model_deck_contracts.validator`. It does not import from
`model_deck.engine.*`, `model_deck.kernel.*`, `model_deck.adapters.*`,
`model_deck.integrations.*`, `model_deck.bootstrap`, sibling plugin
slices, or any transport/process code. It performs no execution, network
access, filesystem access, clock reads or global-state access. Kernel
grant strings travel as opaque `allowed_broker_methods` entries;
authorization of broker calls belongs to the B19 brokers.

## Contracts

### Public surface

| Symbol | Notes |
| --- | --- |
| `LifecycleSession(...)` | Constructor binds expected id/version, offered API, token, broker methods, optional config revision and optional clock. |
| `prepare_hello_request(nonce)` | `created` only; returns detached `hello.params` with offered API and nonce. |
| `accept_hello_result(result)` | Schema-checks and identity-matches; `created -> hello_verified`, mismatch fails. |
| `prepare_activation_request()` | `hello_verified` only; returns detached `activate.params` with the constructor token, methods and config revision. |
| `accept_activation_result(result)` | Requires `activation_id`; enters `active` and records the invocation prefix. |
| `admit_call(operation_id)` / `check_call_allowed(operation_id)` | `active` only; returns a deterministic invocation id tracked as outstanding. |
| `resolve_call(invocation_id)` | Marks an outstanding call completed. |
| `cancel_call(invocation_id)` | Marks an outstanding call cancelled by explicit caller action. |
| `prepare_cancel_request(job_id)` | Returns detached `cancel.params` for a worker-issued job id. |
| `prepare_drain_request(deadline_ms)` | `active -> draining`; returns detached `drain.params`. |
| `accept_drain_result(result)` | Schema-checks; remains `draining`. |
| `deactivate()` | `active`/`draining -> inactive`; clears outstanding handles. |
| `fail(detail)` | Caller-observed failure; enters terminal `failed`. |
| `SessionState` | `created`, `hello_verified`, `active`, `draining`, `inactive`, `failed`. |
| `SessionError`, `SessionErrorCode` | Stable error reporting; never carries the token. |

### Error codes

| Code | Meaning |
| --- | --- |
| `schema_invalid` | A constructor argument or inbound/outbound payload fails shape or schema validation. |
| `out_of_order` | A method was called in a state that does not allow it. |
| `identity_mismatch` | The hello result reports an unexpected plugin id or version; the session fails. |
| `replay` | The hello request was already prepared. |
| `call_not_admitted` | Reserved for admission denial reporting. |
| `unknown_handle` | An invocation id is not outstanding. |
| `terminal` | The session is failed, or an action is not allowed in a terminal state. |

### Wire-shape note

`activate.params` carries only `activation_token`,
`allowed_broker_methods` and optional `config_revision`. It has no
identity fields, no generic context object and no resource-limits field,
so identity is enforced session-side from the verified hello result and
richer context must travel out-of-band. This package does not invent
wire fields to fill those gaps.

## Invariants

- All returned request payloads are detached deep copies validated against
  their frozen schema; mutating a returned mapping never affects the
  session or later payloads.
- Out-of-order local misuse raises without changing state; schema-invalid
  or identity-mismatched inbound results move the session to terminal
  `failed`.
- The injected `clock`, when provided, is stored for future deadline use
  and never called by this revision.

## Tests

Run only the owned test file:

```
PYTHONPATH=python/src /tmp/md-b18-venv/bin/python -B -m unittest \
  -v tests.plugins.test_lifecycle_session
```

## Limitations

- The session does not open transports, spawn processes, enforce
  deadlines or heartbeats, or execute cancellation and deactivation
  against a worker; those belong to the transport/process adapter.
- The session does not authorize broker calls or validate operation
  payloads beyond the lifecycle wire; that belongs to the B19 brokers
  and the kernel registry.

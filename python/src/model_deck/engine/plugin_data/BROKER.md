# Plugin data broker (B19)

Supervisor-side dispatch over settled `PluginAuthority` and the committed
`PluginDataRepository`/SQLite adapter. Wire shapes mirror frozen
`contracts/plugin.v1/broker/storage.{get,list,put,delete}.*.schema.json`.
Authenticated activations arrive via supervisor method arguments, never from
request documents.

## Authority and namespace

Every call resolves the opaque `invocation_handle` through `PluginAuthority`
against the supervisor-supplied `authenticated_activation`. The requested
`namespace` must equal `authenticated_activation.plugin_id`; a foreign
private namespace is denied even when the origin (operator) itself is
privileged, because the broker additionally passes `private_namespace` to
`authority.authorize`, which requires an exact match with the captured
activation. Handles are opaque: the broker never reads a namespace out of
worker-controlled context.

## Mutation guard and revocation

The constructor requires a supervisor-owned `mutation_guard` context-manager
factory. Each operation holds one guard acquisition across reauthorization
and the repository call, so a generation bump or expiry applied by the
supervisor cannot interleave between the check and the effect. The broker
makes no TOCTOU claim beyond this wiring: it only narrows the window to the
shared exclusion the supervisor provides.

Supervisors must apply revocation, expiry, and generation changes under the
same guard. The broker exposes `revocation_barrier()` for exactly this
purpose; acquiring it before mutating trusted authority state serializes
revocation against in-flight broker calls.

## Validation and errors

Parameters and results follow the frozen schemas. `expected_revision`
omitted means unconditional within the authorized namespace, per the accepted
repository contract; `0` means never-created. The list wire carries no
cursor token: `prefix` plus `limit` (1..200) scope a single deterministic
key-ascending query. Quota and compare-and-swap errors map to fixed safe
codes; messages never echo keys, values, namespaces, or identities.

Empty-string keys are valid: the frozen schemas bound `key` by
`maxLength` only, and the committed repository accepts empty keys, so the
broker round-trips them. Values are strictly validated as finite JSON before
any repository call: object keys must be strings, tuples/sets/bytes and
unsupported types are rejected, cycles are rejected, and non-finite floats
are rejected. Validation `TypeError` maps to `broker payload invalid`, never
to `broker invalid request`.

Guard entry or exit failure raises fixed `broker guard failed` with no
rendered cause, message, or traceback text. When the guard fails after the
repository call, the outcome is uncertain: the broker performs no automatic
retry and claims no rollback; supervisors must re-read through a fresh
guarded call. Unexpected store failures raise fixed
`broker store unavailable`, also without raw details.

Error codes: `broker invalid request`, `broker payload invalid`,
`broker data not found`, `broker revision conflict`, `broker quota exceeded`,
`broker authority denied`, `broker guard failed`, `broker store unavailable`.

## Extension and limits

The broker performs no IO beyond the injected repository, owns no schema or
quota policy, and adds no public wire surface. Quota stays configured per
repository instance. Follow-up jobs reuse `PluginAuthority` capture and
`reauthorize_captured` semantics through fresh broker calls with their own
trusted records; this broker issues no handles.

# Plugin job broker (B19)

Supervisor-side dispatch over settled `PluginAuthority` and the committed
`PluginJobRepository`/SQLite adapter. Wire shapes mirror frozen
`contracts/plugin.v1/broker/jobs.{create,progress,complete,fail,check_cancelled}.*.schema.json`.
Authenticated activations arrive via supervisor method arguments, never from
request documents.

## Activation and capture

`create(handle, authenticated_activation, operation_id, ...)` resolves the
opaque `invocation_handle` with `authority.capture` and requires the
requested `operation_id` to equal the captured `operation_id`; a mismatch is
denied with no authority upgrade. The persisted job records the captured
`invocation_id` and the exact `operation_id` plus the exact owner
(`plugin_id`, `activation_id`).

Follow-up calls (`report_progress`, `complete`, `fail`, `check_cancelled`)
take only the authenticated activation plus the frozen params (`job_id` and
the per-method payload). There is no live handle parameter on followups: the
broker loads the trusted record from the repository by `job_id`, requires the
caller activation to equal the recorded owner, then calls
`authority.reauthorize_captured` on the persisted `invocation_id` with current
generation, effects, and grants. Worker-supplied context is never forwarded.

## Mutation guard and revocation

The constructor requires a supervisor-owned `mutation_guard` context-manager
factory plus the exact grant policy for
`create/progress/complete/fail/check_cancelled`. Each operation holds one
guard acquisition across reauthorization and the repository call. The broker
exposes `revocation_barrier()` for the same guard; supervisors must apply
revocation, expiry, and generation changes under it so revocation cannot
interleave between check and effect. The guard serializes supervisor state
changes; it does not stop wall-clock time or extend an invocation deadline.

Guard creation, entry, or exit failures return `broker guard failed` without
underlying exception details. Exit failure may occur after the repository
committed: this error does not promise rollback. Callers must inspect trusted
job state before considering a retry; the broker never retries automatically.
Guards cannot suppress typed denials or repository errors. Unexpected errors
inside guarded operations return `broker operation failed`, also without a
rollback promise.

## Output and errors

`complete` accepts the frozen optional `output` generic JSON value
(`contracts/common/types.schema.json#/definitions/json_value` with string,
array, object, key, cycle, and finiteness bounds). Finite JSON integers retain
integer semantics without conversion to floating point. Output is ordinary data;
there is no attachment capability on this method. A true attachment reference
capability requires a later explicit contract and is documented as pending.

Parameters and results follow the frozen schemas. `fail` accepts the frozen
`error` object (`contracts/common/error.schema.json`): `code` must be in the
committed `FAILURE_CODES`, `retryable` is required, and optional `message` /
`request_id` must be strings when present (empty strings are valid), are
validated, and are then ignored. Only the code is persisted.
`check_cancelled` reports the `cancel_requested` flag only, never the
confirmed terminal state. Terminal jobs stay terminal: second terminal
writes conflict and no call claims, resumes, or restarts work. Errors are
fixed safe codes with no raw repository or authority detail.

Error codes: `broker invalid request`, `broker job not found`,
`broker job conflict`, `broker job terminal`, `broker authority denied`,
`broker guard failed`, `broker operation failed`.

## Scope

Root public `jobs.get` (`contracts/engine.v1/methods/jobs.get.*`) lives
outside this broker and reports `job_id`, `state` (including `interrupted`),
and `progress`; it never resumes work. This broker exposes no `get` helper.
Operator cancel APIs, explicit resume, and the runner remain out of scope:
this broker does not claim full B19.

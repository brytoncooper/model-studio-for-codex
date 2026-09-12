# Plugin invocation authority

This package owns the B19 authorization decision shared by data, job and event
brokers. It does not implement those brokers, transport authentication, token
enrollment, persistence, scheduling or plugin lifecycle.

## Public contracts

`ports.py` declares frozen `ActivationIdentity`, `ActivationState`, `OriginState`,
`OperationAuthority`, and `AuthorityContext`. Permission collections must be
`frozenset[str]`; timestamps must be timezone-aware `datetime` values. Generations
are non-negative integers. Identity binds engine instance, audience, activation,
plugin ID and plugin version. Origin and activation generations are separate.

Inject `TrustedAuthorityState.activation(identity)`, `.origin(principal_id)` and
`.operation(operation_id)` plus `TrustedContextStore.put(context)` / `.get(id)`.
Missing state returns `None`. Context storage must be trusted and insert-only;
it must preserve captured identity, scopes, generations and deadline exactly.
No worker may supply repository implementations, identity or context documents.

`PluginAuthority` also takes the expected engine instance/audience and a clock.
Optional ID/handle factories support deterministic tests; production defaults
generate UUID invocation IDs and unpredictable opaque handles.

- `issue(authenticated_activation, origin_principal_id, operation_id, *, expires_at)`
  is supervisor-only. Returns a handle and persists the original intersection.
- `authorize(handle, authenticated_activation, *, effect, resource_scope,
  capability_grant, private_namespace=None)` rechecks current authority and
  returns its narrowed immutable context, or raises `AuthorityDeniedError`.
- `capture(handle, authenticated_activation)` returns current narrowed context
  for supervisor-owned records. Jobs/subscriptions persist its `invocation_id`.
- `reauthorize_captured(invocation_id, authenticated_activation, *, effect,
  resource_scope, capability_grant, private_namespace=None)` resolves the original
  context from trusted storage and performs the same checks. It never accepts a
  deserialized worker context or renews its deadline.

## Invariants and integration

Effects, scopes and capability grants are exact intersections of origin,
operation descriptor and active callee, narrowed again against the original
capture. No wildcard, prefix, inheritance or default grant interpretation exists.
The broker supplies the exact capability grant declared by the operation and
the exact resource requested, never a generic guessed privilege. Every call
rechecks current expiry, activity, generations, grants and identity. Revocation
generation changes invalidate the capture, including after service restart.

For private storage the broker MUST pass `private_namespace`; it must equal the
actor plugin ID. Callee storage ownership does not bypass origin or operation
write restrictions. Other brokers omit this argument for non-storage resources.
Jobs and subscriptions must obtain invocation IDs from their own trusted records
and reauthorize on every call/delivery, including follow-ups with only a job or
subscription ID on the wire. Background work must receive separately approved
origin/operation state; timers and retries cannot renew foreground authority.

Errors contain a fixed message with no identity, handle or resource echo.
Live handles are process-local; persisted contexts can be reauthorized by a new
service only for the same still-active identity. This is not cross-activation
resume authorization. Integrators coordinate authorization with mutation/delivery
to avoid a revocation race between this check and the subsequent operation;
this package does not provide a cross-repository transaction or capability lease.

## Tests and extension

From `python/`: `PYTHONPATH=src /opt/homebrew/bin/python3.12 -m unittest
tests.engine.test_plugin_authority`. Tests use memory repositories and fixed
clocks only: forged handles, wrong activation/instance/audience, revocation,
expiry, read-to-write escalation, confused deputies, scoped storage and restart.

Implement the two trusted ports at composition; add no concrete storage or
transport imports here. New grant vocabulary belongs to public operation
descriptors. Persistent context serialization and atomic broker mutation remain
adapter work. This slice makes no claim that B19 brokers are implemented.

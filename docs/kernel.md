# Kernel registry and engine composition (B17)

## Purpose and ownership

Application-independent atomic composition registry for built-in feature
descriptors and generic namespaced operation dispatch. Owned files only:
`python/src/model_deck/kernel/__init__.py`,
`python/src/model_deck/kernel/registry.py`,
`python/tests/kernel/__init__.py`,
`python/tests/kernel/test_registry.py`, `docs/kernel.md`. The engine integration
owns `python/src/model_deck/engine/kernel_composition.py`; bootstrap and dispatch
consume that public seam.

## Public types and flow

Types: `KernelApiVersion(major, minimum_minor)`, `OperationDescriptor`,
`EventDescriptor`, `FeatureDescriptor`, `ComposedKernel`, plus `compose`,
`CompositionError`, `UnknownOperationError`, `GrantDeniedError`, and
`KERNEL_API_MAJOR`/`KERNEL_API_MINOR` (currently 1.0).

Flow: construct frozen descriptors, call
`compose(features, capabilities, handlers)` with a tri-state capability map
and an operation-ID to handler map, then use `kernel.invoke(operation_id,
params, grants)`. Listings: `features()`, `feature_order()`, `operations()`,
`events()`, `unavailable_optional_capabilities()`.

## Invariants and errors

Descriptors reuse frozen vocabulary shapes
(`operation_id,input_schema_id,output_schema_id,effect,required_grants?`;
effect is `read`/`write`; IDs are reverse-domain (max 256 chars); schema IDs are any Python str of length 0..256 (empty and NUL allowed) and may carry `#`-fragments; capability IDs are freeform nonempty strings up to 64 chars; versions accept the frozen prerelease suffix (`-[a-zA-Z0-9.]+`); required grants cap at 32 per descriptor with each grant any str of length 0..64 (empty, punctuation, and duplicates allowed); events carry
`event_id,schema_id,required_grants?`). Required capabilities must be exactly
`supported`; optional non-supported entries degrade without failing the
feature. Composition is atomic and deterministic: duplicates, API mismatch,
missing dependencies or handlers, non-callable handlers, duplicate/overlapping capabilities, missing required capabilities, and cycles
raise `CompositionError` with no partial registry. Dispatch checks all
required grants before calling the handler; unknown operations raise
`UnknownOperationError`. Events are metadata only.

## Add a built-in feature

Create a `FeatureDescriptor` with a reverse-domain `feature_id`, semantic
`version`, compatible `KernelApiVersion`, dependency/capability IDs, and
operation/event descriptors, then pass it with its handler map into `compose`.
No dispatch, bootstrap, engine, provider, host, storage, or transport imports
are allowed in the kernel.

## Built-in descriptors

`model_deck.engine.builtins` holds the engine's own features as descriptors:
`capabilities.py` defines the capability vocabulary, the `BuiltinAvailability`
record and `build_capability_map`; `descriptors.py` declares one descriptor per
feature group plus `select_available_builtins`, which drops groups whose
required capabilities are unsupported along with the groups that depend on them;
`handlers.py` defines `BuiltinHandlerAdapter` and the explicit
`BuiltinHandlerRegistry` that binds a feature's operations to collaborators;
`dispatch_binding.py` defines `BuiltinDispatchBinding`, which binds every
built-in operation to the `EngineDispatch` method that serves it.
A new built-in adds its capability to the vocabulary and availability record,
declares a `modeldeck.builtin.*` descriptor, names its dispatch method in
`BuiltinDispatchBinding`, and has its handler adapter registered where the
concrete ports are known, which is bootstrap.

## Routing the built-ins

Routing has migrated. Every composed operation — built-in and foreign alike —
now leaves `EngineDispatch.handle` through one kernel route; the per-operation
`if method == ...` chain is gone. Only `hello` (which runs before the
authentication gate) and that gate precede it.

A built-in's registered handler is the dispatch method that always served it, so
its behaviour is not re-implemented: the same use cases, the same projection
reconcile triggers, the same events. Two seams keep the public answers identical:

* `KernelComposition.invoke_dispatch_bound(operation_id, params, context)`
  carries a `DispatchInvocationContext` (connection, principal, request id) to
  the handler and skips the composition's own input/output validation, because
  the dispatch method validates against the same schema references.
  `dispatch_bound_features` (bootstrap passes `BUILTIN_FEATURE_IDS`) says which
  operations take that path; every other feature keeps the generic `invoke`.
* A bound handler raises `KernelPassthroughError` with the whole JSON-RPC error
  response its method produced, which dispatch returns verbatim. Domain codes
  and `-32602` params errors therefore survive; a foreign feature's failure is
  still redacted to one `internal` error.

A composed feature that is not a built-in may still name a public code by raising
`KernelDomainError(code)`, which `invoke` re-raises and dispatch turns into that
domain error. Only the code crosses that boundary: the sentence the caller reads
comes from `_COMPOSED_DOMAIN_ERROR_MESSAGES` in `engine/dispatch.py`, keyed by
code, so a feature chooses the classification but never publishes text of its
own — a handler cannot interpolate a path, an identifier or a provider response
into the answer. The evidence read operations use it for `resource_exhausted`.
Everything else stays redacted to one `internal` error.

`jobs.get` and `jobs.cancel` execute through the kernel as of this change; until
now they were composed for discovery but still ran through the dispatch chain,
as did every other built-in.

The job methods fan out over the job directories (the first-party directory and
the extension gateway) in order. A directory that does not implement the method
is skipped — `ExtensionGateway` requires `job_get` and `job_cancel` but not
`job_resume`, so a gateway may legitimately serve lookups and not resumes — and
a directory answering "not found" moves on to the next. Only when no directory
could serve at all does dispatch decide: `resume_unavailable` for `jobs.resume`,
and `not_found` for `jobs.get` and `jobs.cancel`. Not done: `ExtensionGateway`
still declares no `job_resume`, so that requirement is only stated here and in
`engine/dispatch.py`, not in the protocol implementers read.

The binding is also dispatch's own handler table: when no composed kernel holds
a built-in — the legacy `kernel_composition` escape hatch, or an `EngineDispatch`
constructed directly — dispatch calls the same registered handler without the
kernel hop, so there is one table rather than two routes.

## Tests

From the Architecture `python` directory:

```sh
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m unittest tests.kernel.test_registry
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m unittest tests.engine.test_kernel_composition
```

## Authenticated engine integration

Construct `KernelComposition(composed_kernel, operator_grants=(...))` and supply
it as the keyword-only `kernel_composition` to `build_engine_server` (or directly
to `EngineDispatch`). Omission preserves the existing static operation behavior.
The composition is active when supplied; there is no additional enable flag.

Composition resolves each operation's bundled input/output schema reference,
including JSON-pointer fragments, through public contracts APIs. Discovery is
validated against the existing `operations.list` result schema. Dispatch rejects
collisions with every reserved static operation, even an optional static operation
not enabled for this server, and rejects oversized combined discovery before
serving. Existing static operations retain their implementation and order.

After the existing enrollment authentication succeeds, registered namespaced
operations use one generic `ComposedKernel.invoke` path. Their descriptors appear
in `engine.v1.operations.list`; adding an operation needs no per-operation dispatch
branch. Inputs and outputs are validated using the descriptor's schemas. Invalid
input returns a fixed invalid-params error; missing grants return
`capability_denied`; handler failures and invalid output return a fixed internal
error without exposing handler messages or invalid values.

Handler results are copied as strict JSON values before validation, so arbitrary
objects, non-string keys and later handler mutations cannot alter the response.
Bootstrap injects the transport's frame encoder as a response preflight. Oversized
results return a fixed error while the authenticated connection remains usable.

`operator_grants` is copied at construction. It applies to the current shared
enrolled local-operator boundary, not separate plugin principals. Frames, method
params and client-selected names never supply authority. A composition requiring
per-plugin identity, content grants or revocation must use the corresponding
broker boundary rather than treating this operator seam as that authorization.
Optional unavailable capabilities remain isolated by the registry; required
capabilities still prevent composition. Capability metadata is not converted into
operator grants.

The socket fixtures prove an otherwise unknown `com.example.fixture.inspect`
operation is discoverable and invocable after authentication, schema/grant errors
cannot dispatch handlers, static reads continue to work, and failed optional
capabilities do not remove independent operations. All runtime state is temporary.

## Limits

No external process supervision, grant persistence, provider ports, process
lifecycle or service locator in the kernel. Events remain metadata only.
External schema installation and external-principal authentication are not
implemented by this integration. No global registry singleton.

Every built-in engine method is now a descriptor with a registered handler, and
routing goes through it. What is still not done: the handler bodies remain
private `EngineDispatch` methods rather than use cases the kernel could call
without dispatch, so a built-in cannot yet be composed into a kernel that has no
dispatch behind it. Built-in handlers also answer with JSON-RPC envelopes rather
than plain results, which is why the passthrough seam exists; a later slice that
moves those bodies out of dispatch would remove it.

A specialized provider port is therefore not a kernel port. It is represented in
the engine composition layer as a feature descriptor that declares the capability
IDs it provides, such as `provider.execution` and `provider.routes`, and the
operations it serves. The concrete port objects stay injected by bootstrap and the
CLI and reach the kernel through handler adapters, so the kernel still sees only
operation IDs, capability IDs and handlers.

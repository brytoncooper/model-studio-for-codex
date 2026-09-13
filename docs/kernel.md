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
lifecycle or service locator in the kernel. Events remain metadata only; existing
static features have not all been re-expressed as descriptors. External schema
installation and external-principal authentication are not implemented by this
integration. No global registry singleton.

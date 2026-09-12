# Kernel registry (B17 slice 1)

## Purpose and ownership

Application-independent atomic composition registry for built-in feature
descriptors and generic namespaced operation dispatch. Owned files only:
`python/src/model_deck/kernel/__init__.py`,
`python/src/model_deck/kernel/registry.py`,
`python/tests/kernel/__init__.py`,
`python/tests/kernel/test_registry.py`, `docs/kernel.md`.

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
```

## Limits

No external process supervision, grant persistence, provider ports, process
lifecycle, bootstrap/dispatch consumer wiring, or service locator. No global
registry singleton.

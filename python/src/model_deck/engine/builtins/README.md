# Built-in feature descriptors

The engine's built-in methods described as kernel `FeatureDescriptor` values,
and bound to the dispatch methods that serve them. The composed kernel now owns
both discovery and routing for these operations; the hardwired dispatch
if-chain is gone.

- `capabilities.py` — the capability vocabulary (one ID per injected collaborator
  group, each with a one-line meaning), the `BuiltinAvailability` record and
  `build_capability_map`, which turns injection facts into the kernel's
  `supported` / `unsupported` / `unknown` states.
- `descriptors.py` — one descriptor per built-in feature group, carrying its
  operations, dependencies, required and optional capabilities, and the kernel
  API version. `select_available_builtins` drops groups whose required
  capabilities are missing, and the groups that depend on them.
- `handlers.py` — `BuiltinHandlerAdapter` (build one feature's handler map from
  the collaborators bootstrap already has) and `BuiltinHandlerRegistry`, an
  explicit map of feature ID to adapter. There is no global registry.
- `dispatch_binding.py` — `BuiltinDispatchBinding`, one registered handler per
  built-in operation over the unchanged private `EngineDispatch` method that
  serves it, plus `builtin_handler_registry(binding)`. Bootstrap constructs the
  binding before composing the kernel, because the kernel needs its handlers;
  `EngineDispatch.__init__` then binds itself to it.
- `dispatch_adapters.py` — the provider feature's adapter, the one built-in that
  serves no operation: it checks the injected execution port and returns an
  empty handler map.

## How a built-in reaches its handler

A bound handler calls the dispatch method with the request id, the params and
the invocation context (`DispatchInvocationContext`: connection, principal,
request id), so connection-scoped operations — subscriptions, notification
queues, host settings, jobs — still know which connection asked.

Dispatch methods answer with whole JSON-RPC envelopes. A bound handler returns
the `result` on success and raises `KernelPassthroughError` carrying the whole
envelope on failure, which `EngineDispatch._invoke_kernel` returns verbatim.
That is what keeps the engine's public error vocabulary (`conflict`,
`not_found`, `unsupported_capability`, ...) and the `-32602` params errors
intact instead of collapsing into the kernel's single `internal`.

The same binding answers when no kernel composed the operation — the legacy
`kernel_composition` escape hatch, or an `EngineDispatch` built directly — so
there is one handler table, not two routes.

## Add a built-in

1. Add a capability ID and its meaning to `CAPABILITY_MEANINGS`, and a matching
   field to `BuiltinAvailability`, if the group needs a new collaborator.
2. Declare the descriptor in `descriptors.py` with a `modeldeck.builtin.*`
   feature ID, its operations as `(short_name, effect)` pairs, its dependencies
   and its capabilities, then list it in `_BUILTIN_DESCRIPTORS`.
3. Add the operation to `_dispatch_calls` in `dispatch_binding.py`, naming the
   `EngineDispatch` method that serves it. `bind` refuses a binding whose calls
   do not cover exactly `builtin_operation_ids()`, so a descriptor without a
   call fails at startup rather than at the first request.
4. Register a handler adapter for the feature ID where the concrete ports are
   known (bootstrap), not here — the built-in features already share the
   binding's adapter.
5. Extend `python/tests/engine/test_builtin_descriptors.py` and
   `python/tests/engine/test_builtin_dispatch_binding.py`.

Operation IDs and schema references follow the engine method convention:
`engine.v1.<short name>` with
`contracts/engine.v1/methods/<short name>.{params,result}.schema.json`.

## Layer rules

This package is an engine feature. It may import `model_deck.kernel` and public
engine modules only — never `model_deck.adapters`, `model_deck.integrations`, a
provider, or another engine feature's private module. Concrete ports are passed
in as collaborators; they are never imported.

# Built-in feature descriptors

The engine's built-in methods described as kernel `FeatureDescriptor` values, so
the composed kernel can serve them instead of the hardwired dispatch if-chain.

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

## Add a built-in

1. Add a capability ID and its meaning to `CAPABILITY_MEANINGS`, and a matching
   field to `BuiltinAvailability`, if the group needs a new collaborator.
2. Declare the descriptor in `descriptors.py` with a `modeldeck.builtin.*`
   feature ID, its operations as `(short_name, effect)` pairs, its dependencies
   and its capabilities, then list it in `_BUILTIN_DESCRIPTORS`.
3. Register a handler adapter for the feature ID where the concrete ports are
   known (bootstrap), not here.
4. Extend `python/tests/engine/test_builtin_descriptors.py`.

Operation IDs and schema references follow the engine method convention:
`engine.v1.<short name>` with
`contracts/engine.v1/methods/<short name>.{params,result}.schema.json`.

## Layer rules

This package is an engine feature. It may import `model_deck.kernel` and public
engine modules only — never `model_deck.adapters`, `model_deck.integrations`, a
provider, or another engine feature's private module. Concrete ports are passed
in as collaborators; they are never imported.

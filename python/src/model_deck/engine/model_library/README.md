# Model library (B02 + B07)

`ModelRepository` is the app-owned read port for registered models. `CatalogCacheRepository` is the narrow cache-only port for provider catalog rows keyed by `connection_id`.

`ListModelsUseCase` maps repository rows to `engine.v1.models.list` results:

- `collection=registered` uses `ModelRepository.list_registered` only.
- `collection=catalog` requires `connection_id`, uses an injected `CatalogCacheRepository`, sets `cache_only: true`, and never falls back to registered models.

Missing or unconfigured catalog cache surfaces `CatalogUnavailableError`. Unknown collections surface `UnsupportedCollectionError` from the use case layer.

## Registered model mutations (B07)

`ModelMutationRepository` is the write port for register, rename, and remove. Commands are immutable slotted dataclasses (`RegisterModelCommand`, `RenameModelCommand`, `RemoveModelCommand`) built only after use-case validation.

`RegisterModelUseCase`, `RenameModelUseCase`, and `RemoveModelUseCase` accept a `Mapping` of wire params, validate inputs, forward the command unchanged, and map `RegisteredModelRecord` to `{ model: { ... } }` or `{ removed: bool }` for remove.

Domain errors (subclasses of `ValueError`, not adapter types):

- `ModelRevisionConflictError` when `expected_revision` does not match current state.
- `ModelIdempotencyConflictError` when the same `idempotency_key` is reused with a different payload.
- `ModelRegistrationNotFoundError` when `registration_id` is unknown.

The SQLite consumer must implement atomic compare-and-swap and durable idempotency: same key and same payload replays the prior result; same key with changed payload conflicts; stale revision conflicts. The use case layer does not generate IDs, clocks, or storage behavior.

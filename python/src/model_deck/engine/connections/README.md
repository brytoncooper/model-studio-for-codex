# Connections (B07)

`ConnectionRepository` is the application-owned port for listing and saving connection records before any SQLite storage adapter exists.

`ListConnectionsUseCase` maps repository rows to `engine.v1.connections.list` results as `{ connections: [...] }` with an empty params object.

`SaveConnectionUseCase` validates wire params, builds `SaveConnectionCommand`, forwards a single repository call, and maps `ConnectionRecord` to `{ connection: { ... } }`. Optional opaque refs are omitted from output when unset.

Domain errors (subclasses of `ValueError`):

- `ConnectionRevisionConflictError` when `expected_revision` does not match current state.
- `ConnectionIdempotencyConflictError` when the same `idempotency_key` is reused with a different payload.

The storage consumer must implement one atomic idempotency lookup plus compare-and-swap insert/update:

- `expected_revision=0` creates a missing connection at revision `1`.
- An existing connection requires an exact current revision match and increments on success.
- Missing connection with non-zero expected revision conflicts.
- Wrong revision on an existing row conflicts.
- Exact same key and payload replays the stored result even after later state changes.
- Same key with changed payload conflicts.

The use case layer does not inspect storage state separately, infer providers, handle credentials, or perform IO.

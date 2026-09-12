# Codex projection consumer

Purpose: consume pending `registered_model.upserted` outbox events into consumer-owned projection files. This package is injected fake-materializer orchestration only; the `CodexProjectionMaterializer` is supplied by the caller and there is no real renderer, connection, or removal support here, and no live integration.

Ownership: `CodexProjectionConsumer` owns exactly the four files in this package plus the owned test. It reads engine projection contracts (`ports`, `intents`, `receipts`, `file_port`) but never edits them. No other files, live state, or providers are touched.

Contracts: frozen `CONSUMER_ID`, `CodexProjectionWrite` (with `artifact_ref` derived as `path.as_posix()`), `CodexProjectionMaterializer.materialize`, marker `CodexProjectionMaterializationError` (message never persisted), `CodexProjectionDependencyError` for malformed file receipts, frozen `CodexProjectionItemResult` / `CodexProjectionBatchResult`. Constructor injects `ProjectionOutboxReader`, `ProjectionMutationIntentJournal`, `ConditionalProjectionFiles`, `ProjectionReceiptStore`, and the materializer. Entry point `consume_pending(limit=100)` returns an ordered batch.

Invariants: only `registered_model.upserted` is supported (anything else is skipped without touching dependencies); strict payload and mutation validation; `artifact_ref` validated with `receipts.validate_artifact_ref` plus absolute/traversal rejection before journal access; every file receipt validated (type, `write` operation, path, expected hash, success reason/result/observed hash with observed matching the expected precondition and noop valid only when expected equals the desired hash, allowlisted write-conflict reason (postdelete interference excluded as write-malformed)) before any receipt-store call, and malformed receipts raise `CodexProjectionDependencyError` with no outcome recorded; `record_intent` always precedes exactly one `compare_and_write`; file `noop` is acknowledged as `applied`; file conflicts persist only `file_<allowlisted reason>`; no retries; unexpected dependency exceptions propagate with later events untouched.

Extension: to support new event kinds, renderers, or removal operations, add explicit validation and tests here first. Crash retries stay conservative: a retry reuses the recorded intent and treats unexpected file state as a conflict rather than adopting it.

Tests: `cd python && PYTHONPATH=src python3.12 -m unittest tests.integrations.hosts.codex.test_projection_consumer` using call-logged and stateful fakes only; no real files or SQLite.

Limitations: no real renderer, no connection/removal support, and no live
integration. An intent-before-file retry can apply normally while the target is
still absent. A retry after first-create or replacement acknowledgment loss
reports the unexpected file state as a conservative conflict instead of
adopting it.

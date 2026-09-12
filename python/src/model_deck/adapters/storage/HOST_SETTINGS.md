# SQLite host-settings ledgers

Durable implementations of the accepted `engine.host_settings.ports`
`PreviewStorePort` and `SaveReceiptStorePort`, backed by one injected
SQLite file. No host TOML is ever persisted here: only binding hashes
and the content-free frozen save result.

## Files

- `sqlite_host_settings.py`: `SQLitePreviewStore`,
  `SQLiteSaveReceiptStore`, `validate_save_result`,
  `ensure_host_settings_schema`.
- Tests: `python/tests/engine/test_sqlite_host_settings.py`.

## Tables

- `host_preview_tokens`: one row per admitted preview token with its
  engine-owned binding plus a `consumed` flag. Consumed rows are kept
  so re-admission of a spent token stays rejected after restart.
- `host_save_receipts`: one row per `(principal, idempotency_key,
  fingerprint)` claim carrying the save binding, an optional frozen
  result, and a `settled` flag. Unsettled rows are the durable
  uncertain claims.
- `host_preview_reservations`: global `(preview_id -> claiming key)`
  index enforcing cross-key and cross-principal single-winner claim
  uniqueness.

## Semantics

Claim outcomes mirror the port contract: `admitted` for the first
writer, `replay` with the exact stored result for the settled owner,
`in_progress` for the unsettled owner. A changed fingerprint under a
used key raises `ReceiptConflictError`, as does a different key or
principal presenting an already reserved preview. `release` deletes
only the owning unsettled claim and its reservation; settled receipts
and their preview reservations are retained permanently so a settled
preview can never be re-saved. Every mutation runs under
`BEGIN IMMEDIATE` on a short-lived dedicated connection, so
concurrent writers serialize and exactly one wins.

Result validation happens before any write: only the eight frozen
save-result fields are accepted with exact recursive types
(`application_effects` items must be frozen enum strings, `backup`
exactly `backup_id`/`display_path`, hashes `absent` or
`sha256:<64 hex>`, bounded string lengths, at most 16 effects);
unknown, raw, or non-string nested fields such as
`candidate_raw_toml` raise a fixed `ValueError`, and the row is left
untouched. The legacy `store` path is a `settle` alias and requires
an already admitted claim: an unbound `store` with no `claim` row
raises `ReceiptConflictError("no admitted claim")` and writes
nothing, so a stored key can never replay or reserve a preview
without `claim`. It never overwrites a settled receipt with a
different result and never clears an uncertain claim owned by
another fingerprint.

## Extension and recovery limits

- Recovery relies on rows, not process memory: after a crash,
  unsettled claims report `in_progress` and settled claims replay.
  Never delete or reinterpret unsettled rows on startup.
- Do not add columns for raw TOML, drafts, or snapshots. New binding
  data must be hashes or identifiers.
- Do not share one connection across threads; open one per call as
  the stores do.
- Schema changes require a versioned migration that preserves
  consumed flags, unsettled claims, and preview reservations.
  `CREATE TABLE IF NOT EXISTS` only covers first creation.
- Backup or copy the database file only while no writer holds it, or
  use the SQLite backup API, or the copy may capture a torn state.

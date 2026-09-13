# Usage records (B16)

`model_deck.engine.usage` records genuine `usage.observed` application run
events and serves bounded chronological usage queries. It owns the
`UsageRepository` port, the record/query use cases, and the public error
vocabulary. Durable storage lives in
`python/src/model_deck/adapters/storage/sqlite_usage.py`, which implements
the port against SQLite.

## Files

- `__init__.py` — public surface re-exports.
- `ports.py` — `UsageRecord`, `RecordUsageResult`, `QueryUsageResult`, the
  `UsageRepository` Protocol, typed errors, `USAGE_QUERY_MAX_RECORDS`, and
  the offset-normalizing timestamp helpers. `parse_observed_at` returns
  `(integer UTC whole seconds from an ordinal-day epoch, exact fractional
  digits)`; `normalize_observed_at` returns an internal lexical sort key,
  never a replacement wire timestamp.
- `use_cases.py` — `RecordUsageUseCase`, `QueryUsageUseCase`, and the frozen
  schema reference constants.
- `reconciliation.py` — `ReconciledUsageQueryUseCase` and
  `UsageReconciliationError`, composing existing recording/query use cases with
  the runs subsystem's public `CommittedUsageEventReader`.

## Public contracts

- `RecordUsageUseCase.record(event)` accepts only `usage.observed` events
  conforming to
  `contracts/engine.v1/vocabulary.schema.json#/definitions/run_event_usage_observed`,
  validated through the public `model_deck_contracts` validator. The
  `usage` payload must carry the same `run_id`/`session_id` as the event
  envelope, otherwise `UsageEventMismatchError`.
- Identity is `(run_id, sequence)`. The validated usage payload, including optional-field presence, is the replay
  fingerprint. An identical replay returns
  `RecordUsageResult(record, duplicate=True)` with no second row. A
  conflicting payload under the same identity raises `UsageConflictError`.
- The payload `usage.observed_at` is the usage observation time used for
  ordering, filtering, and replay. Envelope `observed_at` is a separate event
  receipt time: it must validate but need not match and is not in the usage
  fingerprint. Envelope run/session IDs must still match the payload.
- Optional `settled_amount`/`estimate_amount`/`currency` (plus
  `registration_id`/`connection_id`/`provider_model_id`) are preserved
  exactly as observed; nothing is derived and no legacy ledger is imported.
- `QueryUsageUseCase.query(since, until)` validates params against the frozen
  `usage.query` params schema, requires strict offset-carrying timestamps,
  normalizes offsets to exact UTC whole seconds plus the full fractional
  digits (no float or microsecond rounding), rejects `since` after `until` with
  `UsageQueryValidationError`, and filters chronologically.
- Results are capped at `USAGE_QUERY_MAX_RECORDS` (1000, from the frozen
  `usage.query` result schema). A query matching more raises
  `UsageResourceExhaustedError` instead of returning silent partial totals.
  One ordered SQLite SELECT fetches at most 1001 rows from one read snapshot;
  there is no separate COUNT whose result could race the returned rows.
- `UsageRecord.from_wire()` preserves absent versus explicit null optional
  fields. `UsageRecord.to_wire()` and `QueryUsageResult.to_wire()` are the
  supported complete wire projections; direct dataclass serialization is not
  the wire format. Query validates the complete projection against the frozen
  result schema. Missing nonnullable IDs are omitted; unknown observed costs
  stay null when explicitly reported as null.
- `SqliteUsageRepository(db_path)` takes an explicit fixture path only. It
  stores an internal exact UTC sort key alongside the verbatim `observed_at`,
  closes every owned connection explicitly, enforces identity in a transaction,
  and resolves concurrent duplicate
  inserts by comparing canonical JSON inside the losing transaction.
- Core imports `model_deck_contracts`, its own ports and the runs subsystem's
  public committed-usage reader port; it never imports concrete storage.

## Reconciled query composition

Import `ReconciledUsageQueryUseCase` from
`model_deck.engine.usage.reconciliation`. Construct it with keyword arguments
`reader`, `record_usage`, `query_usage` and optional `page_size` (1–256, default
256). `query(since=None, until=None)` returns the existing `QueryUsageResult`.

Each call starts a fresh reader snapshot, records every bounded page through
`RecordUsageUseCase`, then queries only after its last cursor. The high-water
mark must remain identical across pages. Repeated/cyclic cursors reject, using
constant-memory checkpoints rather than accumulating cursor history. Pages are
processed one at a time; the reader owns the public count/byte bounds. No cursor
is persisted and no subscriber is installed in the provider event path.

Only genuine committed envelopes are projected: run/session IDs, sequence,
schema version, receipt time and usage payload survive unchanged. Extra or
mixed payload fields reject. Costs and optional-field presence are preserved by
the existing recorder. Later committed events await a fresh scan. Partial
projection writes can survive failure, but that query returns no result; the
next full scan replays completed rows idempotently and finishes missing rows.

Reader/unexpected storage failures become fixed-message
`UsageReconciliationError`; existing usage conflict, mismatch, query validation
and resource errors retain their types. A public dispatcher must map all of
these to sanitized fixed messages instead of exposing exception details.

Date-bound validation stays in the existing `QueryUsageUseCase` and runs after
reconciliation; an invalid query can therefore reconcile local records before
rejecting. There is no public standalone bounds validator yet, and this wrapper
does not duplicate that logic or issue an early data query. Concurrent calls may
materialize newer rows into the shared ledger; the high-water mark guarantees a
complete source scan, not snapshot isolation of the final ledger query.

## Error mapping

`UsageEventMismatchError` and `UsageQueryValidationError` are
`invalid_argument`; `UsageConflictError` is `conflict`;
`UsageResourceExhaustedError` is `resource_exhausted`.

## Tests

Run from the Architecture `python` directory:

```sh
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m pytest tests/engine/test_usage_records.py -q
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m unittest tests.engine.test_usage_reconciliation
```

The suite uses real temporary SQLite files and covers identical
replay, conflicts, reopen persistence, time-offset filtering, unknown-cost
preservation, schema shape, strict date validation, and the 1000-record
limit.
Reconciliation tests combine a fake public snapshot reader with real SQLite
usage storage, covering multiple pages, inserts after snapshot capture,
crash/reopen/retry, conflicting replay, absent/null amounts, changed high-water
marks, cursor cycles and safe failures without partial query results.

## Limitations

- The reconciliation use case is available for composition; bootstrap and
  authenticated dispatch wiring are a separate integration slice. Its local
  fake-reader tests do not prove actual run-store/socket composition.
- No scheduler, aggregation, or cost derivation. Unknown costs stay unknown.

No migration of databases from earlier unaccepted prototype layouts is provided.

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
- Core imports only `model_deck_contracts` and its own ports; it never
  imports concrete storage.

## Error mapping

`UsageEventMismatchError` and `UsageQueryValidationError` are
`invalid_argument`; `UsageConflictError` is `conflict`;
`UsageResourceExhaustedError` is `resource_exhausted`.

## Tests

Run from the Architecture `python` directory:

```sh
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m pytest tests/engine/test_usage_records.py -q
```

The suite uses real temporary SQLite files and covers identical
replay, conflicts, reopen persistence, time-offset filtering, unknown-cost
preservation, schema shape, strict date validation, and the 1000-record
limit.

## Limitations

- No event/dispatch wiring yet: nothing publishes `usage.observed` events
  into this slice, and no wire method calls these use cases. Later
  event/dispatch work owns that wiring; this package only provides the
  recording/query seam.
- No scheduler, aggregation, or cost derivation. Unknown costs stay unknown.

No migration of databases from earlier unaccepted prototype layouts is provided.

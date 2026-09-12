# Applied projection receipts (B09)

This seam records projector outcomes against existing `projection_outbox` rows.
It does not compute desired state, parse `payload_json`, or perform artifact I/O.

## Types

- `ProjectionAppliedState`: per-consumer aggregate progress (`consumer_id`,
  `aggregate_type`, `aggregate_id`, `applied_revision`, `applied_outbox_id`,
  `artifact_ref`, `output_sha256`). `output_sha256` is `str | None`; `None`
  marks a verified applied deletion (tombstone) where no artifact bytes were
  produced. An empty-file hash is never used as a sentinel.
- `ProjectionOutboxConflictReceipt`: bounded redacted conflict detail keyed by
  `outbox_id` and `consumer_id`.
- `ProjectionReceiptStore`: `record_applied`, `record_deleted`, and
  `record_conflict`. The store is consumed by projection workers; deletion
  consumers must be wired separately and reviewed before activation.

## SQLite adapter

- Opens an existing fixture database only; missing files raise `FileNotFoundError`.
- Adds one current `projection_applied_state` row per consumer and aggregate,
  per-event `projection_outbox_applied_receipts` for exact retry validation,
  `projection_outbox_conflicts` without altering `projection_outbox` columns,
  and `projection_receipt_schema_metadata` tracking the receipt schema
  version.
- Schema version `v2` widens `output_sha256` to nullable in both
  `projection_applied_state` and `projection_outbox_applied_receipts` so
  deletion (tombstone) receipts can store `NULL` directly. `v1` (legacy)
  treats `output_sha256` as `NOT NULL`.
- Migration from `v1` to `v2` and its version stamp share one
  `BEGIN IMMEDIATE` transaction. Every DDL statement remains inside that
  transaction, so any failure rolls back schema and data together. Known
  legacy table layouts are rebuilt with existing rows, primary keys, explicit
  indexes, and triggers preserved. Unknown table layouts, unknown schema
  versions, occupied migration destinations, and incoming or outgoing foreign
  keys involving a rebuilt table are rejected before mutation. These foreign
  key layouts need a separately designed migration; they are not silently
  dropped or cascaded. This restriction applies only when a table needs rebuilding.
- Existing version `1` metadata advances to `2`; an already-current version
  retains its value. Callers inject a fixture database path; live migration
  is not qualified by this seam.
- Uses `BEGIN IMMEDIATE` transactions for all record paths
  (`record_applied`, `record_deleted`, `record_conflict`).
- Validates the supplied `ProjectionOutboxEvent` against the stored row,
  including exact `payload_json` bytes, before mutating state.
- `record_applied` marks the outbox row `applied`, keeps revision monotonic
  per consumer and aggregate, acknowledges older revisions without
  rollback, and treats equal-revision artifact/hash drift as conflict while
  leaving the row pending. A later tombstone at the same revision is a
  conflict; an older revision after a tombstone acknowledges without
  rollback and preserves the tombstone.
- `record_deleted` produces a tombstone with `output_sha256 = NULL` in the
  applied state row, marks the outbox row `applied`, and mirrors
  `record_applied`'s exact-event verification, atomic outbox ack, current
  revision, and durable idempotent-receipt semantics. Equal-revision
  replay with the same `artifact_ref` is idempotent; changed `artifact_ref`
  at the same revision is a conflict and leaves the outbox row pending.
  Older revisions after a tombstone are acknowledged without rollback so
  the tombstone is preserved and earlier events cannot resurrect the
  artifact.
- `record_conflict` marks only the targeted outbox row `conflict`, stores
  the receipt, and preserves any existing applied state.
- Exact deletion replays return the original tombstone metadata even after a
  later write or deletion; `get_applied` returns the current aggregate state.
  Existing write-receipt replay return behavior is unchanged.
- Exact replays are idempotent; changed receipt parameters raise
  `ProjectionReceiptConflictError`.
- `get_applied` reads the current revision and output hash without creating
  schema or mutating the database. A tombstone row returns
  `ProjectionAppliedState` with `output_sha256 = None`, retaining the
  `applied_revision`, `applied_outbox_id`, and `artifact_ref` that were
  recorded at the deletion event.

## Validation

Consumer, aggregate, and artifact strings must be non-empty printable ASCII
within fixed bounds. Revisions and outbox ids reject `bool`. Output hashes
must be 64-character lowercase hex when supplied (strictly enforced by
`record_applied`); `record_deleted` does not require a hash. Conflict detail
is bounded and rejects obvious secret-bearing markers or field names.

# Applied projection receipts (B09)

This seam records projector outcomes against existing `projection_outbox` rows.
It does not compute desired state, parse `payload_json`, or perform artifact I/O.

## Types

- `ProjectionAppliedState`: per-consumer aggregate progress (`consumer_id`,
  `aggregate_type`, `aggregate_id`, `applied_revision`, `applied_outbox_id`,
  `artifact_ref`, `output_sha256`).
- `ProjectionOutboxConflictReceipt`: bounded redacted conflict detail keyed by
  `outbox_id` and `consumer_id`.
- `ProjectionReceiptStore`: `record_applied` and `record_conflict`.

## SQLite adapter

- Opens an existing fixture database only; missing files raise `FileNotFoundError`.
- Adds one current `projection_applied_state` row per consumer and aggregate,
  per-event `projection_outbox_applied_receipts` for exact retry validation,
  and `projection_outbox_conflicts` without altering `projection_outbox` columns.
- Uses `BEGIN IMMEDIATE` transactions for both record paths.
- Validates the supplied `ProjectionOutboxEvent` against the stored row,
  including exact `payload_json` bytes, before mutating state.
- `record_applied` marks the outbox row `applied`, keeps revision monotonic per
  consumer and aggregate, acknowledges older revisions without rollback, and
  treats equal-revision artifact/hash drift as conflict while leaving the row
  pending.
- `record_conflict` marks only the targeted outbox row `conflict`, stores the
  receipt, and preserves any existing applied state.
- Exact replays are idempotent; changed receipt parameters raise
  `ProjectionReceiptConflictError`.
- `get_applied` reads the current revision and output hash without creating
  schema or mutating the database.

## Validation

Consumer, aggregate, and artifact strings must be non-empty printable ASCII
within fixed bounds. Revisions and outbox ids reject `bool`. Output hashes must
be 64-character lowercase hex. Conflict detail is bounded and rejects obvious
secret-bearing markers or field names.

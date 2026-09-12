# Projection outbox read seam (B09)

Read-only seam over the existing `projection_outbox` table produced by
`adapters/storage/sqlite_outbox.py`.

Purpose: let future projectors poll pending events in stable `outbox_id`
order without touching producer writes, receipts, or schema.

Invariants:

- Immutable `ProjectionOutboxEvent` values only; `payload_json` stays the
  original string and is never parsed or mutated by the reader.
- `list_pending` returns pending rows only (`state = 'pending'`),
  ordered by `outbox_id` ascending, bounded by a strict `limit` 1..1000.
- The SQLite reader opens an existing database read-only and rejects a
  missing file; it never creates files, schema, or rows.
- Connections are closed on success and failure; results are snapshot
  tuples disconnected from the cursor.

Contracts:

- `ProjectionOutboxReader.list_pending(*, limit=100)` is keyword-only and
  returns `tuple[ProjectionOutboxEvent, ...]`.
- `validate_outbox_limit` rejects `bool` and non-`int` with `TypeError`,
  out-of-range ints with `ValueError`.

Extension points:

- New projector backends implement `ProjectionOutboxReader`; claiming and
  marking rows applied/conflict belongs to a future write seam, not this
  reader.

# Projection mutation intents (B09)

This seam records each consumer's *intended* write or delete against a pending
`projection_outbox` row. It is audit-only: it never parses `payload_json`,
performs artifact I/O, or updates the outbox row. The actual compare-and-mutate
that creates or deletes a target file belongs to the file port; this journal
exists so a consumer can durably record what it *plans* to do before the first
create, and so retries after a crash can see whether a prior intent was
recorded.

## Types

- `ProjectionMutationIntent`: frozen per-(outbox row, consumer) record of the
  intended write/delete, with the aggregate fields denormalized from the outbox
  row, the exact `payload_sha256`, the consumer's `artifact_ref`, an optional
  `expected_sha256` precondition, and a `desired_sha256` that is non-`None` for
  writes and `None` for deletes.
- `ProjectionMutationIntentJournal`: `get_intent` and `record_intent`.
- Operations are the string constants `OPERATION_WRITE` ("write") and
  `OPERATION_DELETE` ("delete"). A write intent must carry a non-`None`
  `desired_sha256`; a delete intent must carry `desired_sha256=None`.

Errors live in `model_deck.engine.projections.receipts`:

- `ProjectionReceiptConflictError` — replay of an existing intent where any
  stored field (aggregate type, aggregate id, aggregate revision, event kind,
  payload hash, operation, artifact ref, expected hash, desired hash) differs
  from the supplied event or parameters. Includes the "same payload, changed
  event" case.
- `ProjectionOutboxStateError` — fresh record where the outbox row is not in
  the pending state, or the additive intent schema cannot be created because
  the producer outbox schema is missing.
- `ProjectionOutboxEventMismatchError` — supplied `ProjectionOutboxEvent`
  does not match the persisted outbox row (missing row or content mismatch).

`record_intent` first runs `validate_projection_event(event)` (covering
`outbox_id >= 1`, aggregate/revision/event bounds, payload str), then
`validate_consumer_id`, `validate_operation`, `validate_artifact_ref`, the
operation-specific `desired_sha256` invariant, and `validate_output_sha256`
for any non-`None` desired or expected hash. Only after every validator
succeeds does it compute `payload_sha256` and open the database.

## SQLite adapter

`SQLiteProjectionMutationIntentJournal` is the additive implementation:

- `ensure_projection_intent_schema(conn)` is invoked once per `record_intent`
  call **before** `BEGIN IMMEDIATE`. It verifies that `projection_outbox`
  already exists (the additive schema is never installed before its producer)
  and creates `projection_mutation_intents` with
  `PRIMARY KEY (outbox_id, consumer_id)`. Schema creation and the intent
  insert commit as separate transactions, matching the receipt store pattern.
  Callers that already created the producer outbox schema do not need to
  pre-create the intent schema.
- The journal opens an existing fixture database only; missing files raise
  `FileNotFoundError`. The `get_intent` path opens the database read-only and
  treats a missing intent schema as no record (returns `None`).
- `record_intent` uses `BEGIN IMMEDIATE`. Inside the transaction the order is
  fixed and exactly:
  1. `SELECT` the `projection_outbox` row for `event.outbox_id`.
  2. If the row is missing, raise `ProjectionOutboxEventMismatchError`
     ("outbox row not found").
  3. If `events_match_row(event, outbox_row)` is `False`, raise
     `ProjectionOutboxEventMismatchError` ("outbox event does not match
     persisted row").
  4. `SELECT` any existing intent for `(outbox_id, consumer_id)`.
  5. If an existing intent is present, compare every stored field
     (`aggregate_type`, `aggregate_id`, `aggregate_revision`, `event_kind`,
     `payload_sha256`, `operation`, `artifact_ref`, `expected_sha256`,
     `desired_sha256`) against the supplied event and parameters. An exact
     match commits and returns the stored intent unchanged **regardless of the
     current outbox state** — an exact replay can succeed even after the
     outbox row has progressed to `applied` or `conflict`. Any mismatch
     raises `ProjectionReceiptConflictError`.
  6. If no existing intent is present, require `outbox_row.state == "pending"`.
     Any other state raises `ProjectionOutboxStateError`. Otherwise insert the
     new row and commit.
  The outbox row is never updated; only the additive intent row is written.
- The payload is never stored. Only `payload_sha256`, computed as the
  lowercase hex SHA-256 of `event.payload_json.encode("utf-8")`, is persisted.
- `expected_sha256=None` strictly means the target must be absent. It is
  never an ownership proof: an unrelated writer could have created identical
  bytes after the absence check, and the recorded intent does not
  retroactively authorize adoption of an existing file.
- `get_intent` validates `outbox_id` (strict int, `>= 1`, `bool` rejected) and
  the consumer id before opening the database.

## First-create intent does not guarantee absence

The journal records the consumer's plan; it does not proved that the target
was absent at the moment of recording, and it does not prevent another writer
from creating an identical target before the file port performs its
compare-and-mutate. A consumer that recorded an `expected_sha256=None` write
intent must still treat the file port's first compare as authoritative: a
matching existing file is not, by itself, evidence of Model Deck's prior
creation, and the recovery rule remains "conflict on first create when the
target is unexpectedly present." Recording the intent is necessary for crash
recovery; it is not sufficient to waive the absent-only precondition.

## Validation

All event-shape, aggregate, consumer, and output-hash validators are reused
from `model_deck.engine.projections.receipts`:
`validate_projection_event`, `validate_consumer_id`, `validate_artifact_ref`,
`validate_output_sha256`, and `validate_strict_int`. The intent-specific
`validate_operation` lives next to the operation constants and is the only
intent-shape validator the journal defines. `bool` is rejected for `outbox_id`
and `aggregate_revision`. `compute_payload_sha256` hashes the exact UTF-8
bytes of `payload_json`; the payload itself is not retained.

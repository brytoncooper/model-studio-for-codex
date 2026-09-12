# Live run event replay

`LiveRunEventReplay` (`live_replay.py`) is the in-memory live fan-out and short
replay window for `ApplicationRunEvent` values, keyed by run. Publishers append
per-run events; subscribers attach at a cursor, drain bounded pages with credit
flow control, and ack progress. The engine owns durability and dispatch; this
adapter covers only live observers that can tolerate a bounded gap.

## Contracts

- Event shape, page/ack result types, outcome enum, and bound constants come
  from `model_deck.engine.runs.ports`. The adapter never redefines them.
- Published events are detached on entry: the payload must be a JSON value
  (finite numbers, string object keys) and each subscriber receives its own
  copy, so later caller-side mutation cannot corrupt the buffer or a page.
- Publish enforces a contiguous per-run sequence. The first event for a run
  sets the baseline, so a publisher restarting from durable state may continue
  the durable sequence; any later gap or duplicate raises `ValueError`.
- Credits and ack returns are integers in 1..256. Credit is capped at 256 on
  replenish, and acking an already-acked sequence is a no-op that returns the
  current outcome without inflating credit.

## Buffer and delivery invariants

- Each run keeps a bounded buffer: at most `RUN_LIVE_REPLAY_MAX_BYTES`
  (8 MiB) of encoded events and entries newer than
  `RUN_LIVE_REPLAY_MAX_DURATION_SECONDS` (60 s). Eviction runs on publish,
  subscribe, and read, and only evicts from the head.
- A subscribe cursor at or past the newest published sequence starts live from
  there. A cursor behind the evicted head returns `RESUME_UNAVAILABLE`
  instead of a partial history; that state is sticky for the subscription.
- Each subscription has an independent pending queue bounded by
  `SUBSCRIBER_QUEUE_MAX_EVENTS` (256) and `SUBSCRIBER_QUEUE_MAX_BYTES`
  (1 MiB). Breaching either latches `SLOW_READER`, which is sticky: acking
  afterwards still reports `SLOW_READER`.
- Delivery is credit-gated per page. Ack advances `last_acked_sequence` up to
  the delivered frontier only; acking beyond delivered sequences raises
  `ValueError`. Unknown handles raise `KeyError`.
- All state is guarded by one lock, so publish, subscribe, read, ack, and
  unsubscribe are safe across threads. `clock` and `id_factory` are
  injectable for deterministic tests.

## Extension

There is nothing to subclass for new behavior. New event kinds flow through
unchanged as long as they satisfy the port types; new bounds or outcomes
belong to `engine.runs.ports`, and this adapter picks them up by import.

## Testing

Focused suite: `python/tests/engine/test_live_run_replay.py`, covering the
publish/subscribe/read/ack flow, credit exhaustion and replenish, duplicate
acks, durable-sequence continuation after restart, eviction-driven
`RESUME_UNAVAILABLE`, and the slow-reader latch.

## Limits

- Memory only: a process restart drops every buffer, and the replay window is
  minutes and megabytes, not history.
- No persistence, no cross-process fan-out, and no redelivery after
  unsubscribe. Consumers needing durable history use the engine projection,
  not this adapter.

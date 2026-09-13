# Committed usage-event reader

This internal port supplies committed `usage.observed` events to usage reconciliation.
The run repository owns the event data; consumers use `CommittedUsageEventReader`
and never inspect its SQLite tables. See [run subsystem](README.md) and
[port](usage_events.py).

Start each reconciliation with `read_committed_usage_events(cursor=None)`. Process
all returned events, then pass `next_cursor` unchanged until it is `None`. Each
query starts fresh; the usage ledger must deduplicate original run/sequence
identities. The reader does not persist reconciliation progress or acknowledge
usage events. Public transport schemas are unchanged.

The SQLite adapter captures the initial rowid high-water and first page in one
read transaction. Later pages enumerate only usage events through that cutoff in
rowid order. Concurrent inserts beyond the cutoff appear on the next fresh query.
Run ID, session ID, sequence, schema version, timestamp and full stored JSON
payload are preserved. Events/pages are immutable; accessing `payload` produces a
fresh detached JSON value.

Pages accept an integer limit of 1–256 and contain at most 1 MiB of stored payload
JSON bytes. Metadata is constrained by the owning run contracts. Iteration reads
at most one additional bounded payload; it never materializes the whole snapshot.
An oversized, missing or nonfinite/corrupt stored usage payload fails explicitly
rather than silently skipping the event. SQL NULL is invalid usage payload data.
A byte-limited page may contain fewer than the requested number of events.

Cursors are opaque, authenticated and bound to one repository instance. Do not
persist them, inspect them, or transfer them between instances. Restart begins a
fresh reconciliation. Reusing a valid cursor can repeat its page: consumers remain
responsible for idempotency. This snapshot model requires the owner's append-only
event table: it currently performs no deletion or VACUUM. Cursors are invalid
across maintenance, database replacement or rebuilding; this implementation does
not detect such out-of-contract maintenance. No schema or index migration was
introduced. Filtering can scan non-usage rows, although page memory stays bounded.

To extend the reader, preserve these identity, cutoff and boundedness guarantees
and implement the separate Protocol. Changing event retention or rebuilding the
SQLite table requires revisiting the ephemeral snapshot contract first.

Focused verification: `python.tests.engine.test_committed_usage_events` covers
actual SQLite pagination, full payload detachment, concurrent later insertion,
restart/foreign cursor rejection, malformed inputs, byte bounds and corrupt data.

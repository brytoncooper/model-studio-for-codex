# Conditional projection files (B09)

`ConditionalProjectionFiles` is the consumer-owned compare-before-mutate seam
for projection artifacts. It does not discover host paths, decide file
ownership, render content, read credentials, or record outbox state.

## Calls

- `compare_and_write(path, data, expected_sha256)` conditionally replaces one
  owned file.
- `compare_and_delete(path, expected_sha256)` conditionally deletes one owned
  file.

`path` is an explicit fixture-relative path supplied by the consumer. Concrete
adapters must reject absolute paths, traversal, symlinks, and targets outside
their injected root. A lowercase 64-character SHA-256 value means the current
owned file must have exactly that hash. `None` strictly means the target must
be absent; it never authorizes adoption or replacement of an existing file.

## Receipt

Every call returns a frozen, content-free `ProjectionFileReceipt` with:

- `operation`: `write` or `delete`
- `path`: the caller-supplied fixture-relative path
- `outcome`: `applied`, `noop`, or `conflict`
- `expected_sha256`: the caller's precondition
- `observed_sha256`: the hash seen during the locked precondition read, or
  `None` when absent
- `result_sha256`: the hash seen during immediate post-mutation verification,
  or `None` when absent
- `reason`: `None` on success, otherwise one stable reason code

Stable conflict reasons are `missing`, `unexpected_existing`, `hash_mismatch`,
`symlink`, `foreign_owner`, `postwrite_interference`, and
`postdelete_interference`. Receipts never contain file bytes, parsed content,
free-form diagnostics, credentials, or timestamps.

For a write, matching current bytes and requested bytes is `noop`; a successful
replacement is `applied`. For a delete, an absent target with an absent-only
precondition is `noop`; a successful removal is `applied`. A precondition,
symlink, or ownership failure is `conflict` and performs no mutation.

## Crash recovery before the first create

Matching desired bytes and a managed marker do not prove that Model Deck
previously owned an existing file. The consumer must durably record a
per-outbox mutation intent before the first create. That intent binds the event,
fixture-relative target, desired output hash, and a confirmed-absent
precondition, but it proves only what Model Deck intended to create. It does not
prove that Model Deck created the file now present; an unrelated writer could
have created identical bytes after the absence check.

The conservative recovery rule is therefore `conflict` whenever
`expected_sha256=None` encounters an existing target, including a byte-for-byte
match and including a recorded intent. A future recovery design may adopt an
existing file only after it adds durable identity or exclusivity evidence that
distinguishes Model Deck's creation from an identical foreign creation. The
applied-state receipt is created too late to prove ownership for this crash
window. The future B09 consumer/journal owns the intent; the file port does not
create it or weaken the absent-only precondition.

## Serialization and external writers

A concrete mechanism must serialize Model Deck writers with a process-level
path lock, or run under the engine's proven exclusive single-writer instance.
A thread lock alone is insufficient. While holding that ownership, the adapter
performs lstat/read/precondition checks, writes a same-directory temporary file,
fsyncs, atomically replaces or unlinks, fsyncs the directory, and immediately
checks the target again.

If that postcheck differs from the requested result, the adapter returns
`conflict` with `postwrite_interference` or `postdelete_interference`. It reports
the precondition hash in `observed_sha256` and the immediate postcheck hash in
`result_sha256`, preserves what is present, and does not retry or restore
blindly.

This is not filesystem compare-and-swap against arbitrary noncooperating
processes. An external edit after the method returns is outside the receipt's
claim and is detected by the next expected-hash operation. Fixture acceptance
must cover external changes before mutation and interference observed during
the postcheck window without claiming stronger filesystem guarantees.

# SQLite extension lifecycle repository

[`sqlite_extension_lifecycle.py`](sqlite_extension_lifecycle.py) implements the
engine's [`ExtensionLifecycleRepository`](../../engine/extensions/ports.py).
It owns durable lifecycle claims, phase evidence, selected installation records,
rollback intent, and exact idempotency receipts in one SQLite database.

## Boundary

The repository records facts supplied through the engine port. It does not read
archives, copy or migrate extension data, run extension processes, issue or map
authority, quiesce work, freeze writes, or open admission. Those effects belong
to the artifact, data, activation, and authority adapters and must finish outside
the repository transaction. Their bounded opaque evidence is then committed by
`advance`, `abort`, or `rollback`.

The database contains opaque supervisor references only. It has no bearer token,
credential, filesystem path, arbitrary workflow payload, or extension-owned SQL.

## Durable contract

- `claim` starts with the settled receipt lookup, followed by the matching pending
  key lookup, before it evaluates extension revision CAS. The key identity is
  principal, action, and idempotency key. A different request digest conflicts.
- One pending operation owns an extension. A retry of that operation returns
  `IN_PROGRESS`; it does not grant a second execution owner.
- Install requires absence or a retained `REMOVED` tombstone. Other actions require
  a non-removed record. Tombstone revisions remain part of admission CAS.
- Requests and captured previous records never change. `advance` accepts one legal
  phase edge and requires the next phase revision. Frozen, staged, and activation
  evidence can only appear in the phase that owns it and cannot later be replaced.
- `switch` alone changes selected executable, data, scopes, status, generations,
  and extension revision. It writes that record and the exact intended `APPLIED`
  receipt in the same transaction.
- Install starts with no approved scopes. Update keeps the ordered intersection of
  prior approvals and the new artifact's requested scopes. Grant changes use only
  the scopes bound into the request.
- Activation generation advances whenever serving authority must be replaced or
  revoked. Grant generation advances when the executable permission surface,
  approved scopes, or retained removal authority changes. Rollback advances both
  generations beyond the switched record; it never restores old authority.
- `abort` and `rollback` persist `RESTORING` and an exact intended receipt while
  retaining the claim. When a freeze completed but the `QUIESCED` write failed,
  `abort` persists that matching prior-data freeze in the same transaction as
  `RESTORING`; it cannot replace different persisted evidence. `settle` publishes
  the receipt and releases the claim only after the coordinator has restored data
  and admission state.
- A post-switch data revision different from the switch baseline commits
  `RESOLUTION_REQUIRED` before raising `LifecycleResolutionRequiredError`. Once
  recorded, explicit resolution remains mandatory. Rollback freeze and resolution
  references are retained in typed columns for restart diagnosis.
- `recover` returns only pending operations, ordered by operation UUID, in pages of
  at most 256. It performs no effect, retry, or ownership transfer.

SQLite mutations use `BEGIN IMMEDIATE`, so receipt lookup, CAS, selection changes,
phase changes, and claim release are serialized within one database state.
Composition must still hold the engine's exclusive instance lease. Phase CAS is
not a distributed execution lease.

## Adding behavior

Change the immutable engine port and its phase graph first, with the affected
coordinator and adapter owners reviewing the contract. Then add an explicit column
for any new durable proof and a discriminative repository test. Do not add generic
JSON effect payloads or infer external success from a persisted activation ref.

Schema evolution needs an explicit migration before this adapter is used with a
previously released database. The current schema is created lazily for a new,
explicit database path.

## Tests and limitations

Run the focused suite from `python/`:

```sh
PYTHONPATH=src /tmp/md-b18-venv/bin/python -m unittest \
  tests.engine.test_sqlite_extension_lifecycle -v
```

The suite covers durable replay, claim exclusion, phase and evidence CAS, atomic
switches, monotonic rollback, sticky data-resolution handling, recovery paging,
and the absence of authority-token columns. It does not execute external effects,
prove cross-process service ownership, migrate a released schema, or qualify a
live Model Deck installation.

# Extension lifecycle service

`service.py` is the B20 coordinator between the immutable lifecycle records in
`ports.py`, durable lifecycle storage, plugin data migration, and supervised
activation. It accepts only an already authenticated and inspected
`LifecycleRequest`. Archive paths, bearer tokens, database handles, permission
interpretation, and inferred grants are outside this service.

## Composition contract

Construct `ExtensionLifecycleService` with implementations of
`ExtensionLifecycleRepository`, `ExtensionDataLifecycle`, and
`ExtensionActivationLifecycle`, plus a composition-owned
`HeldExclusiveEngineLease`.

The lease implementation must raise from `assert_held_for(repository)` unless
the existing exclusive engine instance lease for that repository's database is
currently held. The service calls this assertion immediately before every
repository operation and every data or activation effect. It does not inspect a
database path, discover a lock, acquire a second lock, or infer lease ownership.
Composition must acquire the lease before constructing or using the service.

The lease also provides `execution_owner(repository, operation_id)`. Its owner
registry must be shared across every service wrapper for the same database under
that lease. Entering must reject an operation ID that is already executing, before
the second caller reaches `claim`, validation, revocation, or any other effect.
An instance-local service lock is insufficient. Repository claim ownership remains
the durable cross-request guard, while lease execution ownership fences external
effects that a phase CAS cannot fence.

## Public API

- `execute(request)` claims a trusted request. An exact replay returns its
  original `LifecycleReceipt` without effects. An in-progress claim returns its
  current `LifecycleOperation` without effects. Only a newly admitted claim is
  executed.
- `list_pending(after_operation_id=None, limit=256)` reads bounded durable
  pending operations without executing them.
- `recover_operation(operation, resolution_ref=None)` explicitly resumes one
  operation. The supplied object identifies the operation only. The service
  locates it through the repository and calls `claim` again to reload and verify
  the current durable request and phase before any effect. Recovery never starts
  a missing operation and never retries provider work as a side effect of
  startup or listing.

`recover_operation` returns a receipt after settlement or the durable operation
when it remains `RESOLUTION_REQUIRED` and no explicit resolution reference was
provided. A resolution reference is accepted only for that phase. It is an
opaque, trusted supervisor decision; the service does not interpret it.

## Forward execution

All effects are synchronous and serialized for the operation. Each completed
boundary is persisted before the next phase:

| Action and prior state | Ordered path |
| --- | --- |
| Install | claim, quiesced marker, stage new data, switch to `INSTALLED`, keep admission closed, settle |
| Update while enabled | quiesce, freeze old data, stage migrated copy, validate non-serving candidate, switch, admit, settle |
| Update while installed or disabled | quiesce, freeze, stage, switch without candidate execution, keep admission closed, settle |
| Enable | quiesce, freeze, validate non-serving selected version, switch, admit, settle |
| Change grants while enabled | quiesce, freeze, validate non-serving selection with new grants, switch, admit, settle |
| Change grants while non-serving | quiesce, freeze, switch without candidate execution, keep admission closed, settle |
| Disable or remove | quiesce, freeze, switch, keep admission closed, settle |

Install starts with no old-data freeze, including reinstall over a retained
removed tombstone. Install grants remain empty. Update keeps only previously
approved scopes that the new artifact still requests. Grant changes use exactly
the scopes already carried by the trusted request. The service never runs plugin
migration hooks, network installers, or package managers; the injected data
adapter stages and validates data under its own contract.

Persisted activation references are evidence, not reusable sessions. Explicit
recovery from `ACTIVATION_VALIDATED` revalidates before switch. Recovery from a
switched enabled selection or restoration of an enabled prior selection also
creates a fresh non-serving validation before admission.

## Failure and recovery

A failure before `SWITCHED` triggers candidate revocation, durable `RESTORING`
abort intent, conditional thaw of frozen prior data, synchronization of the
prior record, and settlement to `ABORTED`. If revocation or restoration fails,
the exception is returned to the caller and the claim remains held at its last
durable phase. If data freezing succeeded but persisting `QUIESCED` failed, the
service passes that completed freeze to `abort`; the repository persists it
atomically with `RESTORING`, so reconstruction cannot skip the required thaw.

A failure while synchronizing a switched selection freezes its current data,
revokes candidate authority, and asks the repository to roll back. The
repository enforces the post-switch data-loss guard. Changed data leaves the
claim `RESOLUTION_REQUIRED` until a supervisor supplies an explicit resolution
reference. A successful rollback remains `RESTORING` while prior data is thawed
and prior admission is synchronized, then settles to `ROLLED_BACK`. A failed
first-install rollback keeps the repository's removed tombstone and does not
reopen its data.

Settlement failure after successful admission does not roll back the selected
record. The durable `SWITCHED` claim remains pending, and an explicit recovery
repeats idempotent synchronization before settling the original receipt.

## Verification

Run the focused contract suite from `python/`:

```sh
PYTHONPATH=src /tmp/md-b18-venv/bin/python -m unittest \
  tests.engine.test_extension_lifecycle_service
```

The fake boundaries require a fresh lease assertion before every callback. The
suite covers replay and pending suppression, non-serving actions, scope
retention, exact effect ordering, explicit durable-phase reload, resolution,
same-operation concurrency, and injected faults across repository, data, and
activation boundaries. A real SQLite reconstruction case covers the completed
freeze/failed-QUIESCED-write crash boundary. A second real SQLite case uses two
repository wrappers for the same database and proves shared lease ownership
allows only one recovery effect chain. Broader adapter durability and composition
of the actual engine lease remain integration responsibilities.

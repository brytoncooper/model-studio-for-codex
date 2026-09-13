# Versioned SQLite plugin data

[`sqlite_versioned_plugin_data.py`](sqlite_versioned_plugin_data.py) implements
the storage-owned [`VersionedPluginDataStore`](../../engine/plugin_data/versioning.py)
contract. It keeps plugin data generations separate while reusing the existing
SQLite plugin-data validation, canonical JSON, quota, prefix, and CAS rules from
[`sqlite_plugin_data.py`](sqlite_plugin_data.py).

## Ownership and composition

Construct `SQLiteVersionedPluginDataStore` with an explicit SQLite path, optional
`PluginDataQuota`, and an optional mapping from inspected artifact hash to trusted
`PluginDataMigration` callback. The mapping is supplied by engine composition;
neither a plugin request nor lifecycle payload can provide a callback, namespace,
data reference, SQL statement, or migration implementation.

`repository_for(PluginDataBinding)` returns the existing `PluginDataRepository`
surface. The repository retains its namespace, data reference, and activation
generation and rechecks all three inside every read or mutation transaction. It
never follows a later selected pointer. A stale binding fails before any entry or
dataset revision changes.

The store owns one reentrant mutation barrier. Broker authorization must hold this
same store instance's barrier around the bound repository call. The Python barrier
is instance-local; composition must not wrap broker authorization with a different
store object for the same database. Bound writes, freeze, thaw, stage bookkeeping,
and selection activation also hold it. Every SQLite mutation then
uses `BEGIN IMMEDIATE`, so another store instance or process racing through the
same database either commits before freeze and is included in its final revision,
or observes the frozen or stale binding and performs no write.

## Generations and revisions

Each generation has an opaque data reference, one namespace and artifact identity,
a dataset revision, serving state, activation generation, and write/freeze flags.
Entries retain the existing per-key revisions and tombstones. Each successful put
and each live-entry delete advances the dataset revision exactly once. Reads and
no-change deletes do not.

`freeze(operation_id, selected)` seals the exact data reference and activation
generation in the same transaction that captures the final dataset revision. Its
receipt binds the complete immutable selection and whether it froze the selected
generation or a sealed staged candidate. Replay first revalidates that the supplied
selection is still current for that generation, then returns the original
`FrozenData`. An old receipt cannot describe a newer activation. One lifecycle
operation may freeze its old and candidate data references independently. A sealed
pre-switch candidate must have been staged by that same operation; another
operation cannot claim its data reference. Disabled selections can be frozen even
though they were already non-writable. Freeze replay succeeds only while the exact
generation remains frozen at the receipt revision; thaw or a later write consumes
that proof. The generation also stores the active freeze reference. Freeze replay,
thaw, and stage-copy validation must match it, so a fresh freeze invalidates every
older proof even when the dataset revision is unchanged.

`stage(operation_id, candidate, frozen)` uses a deterministic operation-bound data
reference. Install creates an empty generation. Update copies the exact frozen
source revision, including live rows, tombstones, values, and per-key revisions,
without changing the source. The copied dataset revision starts at the frozen
revision.

If the candidate hash selects a trusted migration, the callback receives CRUD
bound only to that staging generation. The callback runs outside every SQLite
transaction. Its individual CRUD operations still use the common validation,
quota, CAS, barrier, and transactions. The stage becomes selectable only after the
callback returns and the store seals it with an immutable `StagedData` revision and
migration receipt.

A callback failure leaves a non-serving partial stage. Retrying the same operation
validates its candidate and source identity, removes only that partial destination,
recreates it from the unchanged frozen source with a newer persisted staging
incarnation, and reruns the trusted callback. Every staging CRUD call and the final
seal compare that incarnation. A callback from an older concurrent attempt fails
instead of writing into or sealing the replacement stage. Completed stage replay
returns its original sealed revision even if selected data later changes.

## Selection, disable, removal, and restoration

`activate_selected` checks the exact data reference, artifact, dataset revision,
and monotonic activation generation. A durable receipt binds the operation, full
selection, expected dataset revision, and enabled state. Exact replay is a no-op;
reuse with changed fields conflicts. A state change from a different operation
requires a newer activation generation, preventing disable and re-enable at an old
generation from reviving a captured repository.

The restoration path is deliberately narrower. After one operation freezes and
then thaws the exact previous selection, that same operation may reopen its
unchanged generation. The store records the thaw operation on the generation and
consumes it when activation succeeds. A historical freeze, foreign thaw, or
different operation cannot use this path. This matches lifecycle abort restoration;
post-switch rollback selects the previous data with a newer generation.

Activation revokes older bindings for the namespace before making the exact
selection current. `enabled=True` opens writes only if the generation is not
frozen. `enabled=False` keeps it sealed while retaining every row and tombstone;
this covers install, disable, and remove without deleting user data. Storage
binding restoration does not restore plugin authority: process and session
revocation and admission remain required composition steps.

Old update generations remain retained but their captured bindings become stale.
`thaw` verifies the original freeze receipt and only clears lifecycle freeze state;
it does not open writes. Restored serving writes require a later exact
`activate_selected(..., enabled=True)` with the permitted activation generation described above; thaw alone
cannot revive an old binding. Exact thaw replay is idempotent before reactivation;
after activation consumes the thaw marker, historical replay conflicts.

## Tests and limitations

With your development virtual environment active, run the focused adapter and
legacy-regression suites from `python/`:

```sh
PYTHONPATH=src python -m unittest \
  tests.engine.test_sqlite_versioned_plugin_data \
  tests.engine.test_sqlite_plugin_data \
  tests.engine.test_plugin_data_versioning_contract -v
```

The adapter tests use temporary real SQLite databases. They cover cross-instance
write/freeze ordering, current-binding freeze replay, activation receipts, stale
and disabled bindings, exact same-operation restoration, quota, namespace and
data-reference isolation, tombstone copying, dataset revisions, failed migration
recreation, concurrent stage-incarnation fencing, immutable stage replay, and
thaw/activation ordering.
They do not select trusted migrations for production artifacts, authorize broker
requests, exercise a live application database, or delete retained user data.

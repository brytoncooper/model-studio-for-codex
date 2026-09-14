# Versioned plugin data contract

`versioning.py` is the storage-owned seam between the B19 plugin data broker and
the B20 extension data lifecycle. It adds no wire method and does not authorize a
worker. The supervisor captures a `PluginDataBinding` from trusted selected
installation state and asks the store for a generation-bound implementation of
the existing `PluginDataRepository` CRUD contract.

## Binding and mutation barrier

A binding contains the plugin namespace, opaque selected `data_ref`, and selected
activation generation. None of these values comes from plugin request JSON. A
bound repository checks the complete binding on every operation. It must not
resolve the current pointer once and then keep using it after a switch.

The concrete store owns one mutation barrier. Broker authorization and its
repository call hold that barrier together. Freeze, activation-generation
changes, and data lifecycle mutations use the same barrier. SQLite implementations
also serialize each mutation with `BEGIN IMMEDIATE` in the same state database.

This gives a racing write two valid outcomes: it commits before freeze and is
included in the frozen dataset revision, or it observes the frozen/stale binding
and fails without mutation. A repository captured by an old activation remains
bound to its old data reference and activation generation after a pointer switch.
It cannot retarget the new generation. Frozen data may be readable while trusted
authority still permits the read; `put` and `delete` always fail while frozen.

Freeze is idempotent for the same operation, including a selected generation that
was already non-writable because the extension was disabled. Repeating it returns
the original freeze evidence and final dataset revision. A different operation or
stale activation generation conflicts rather than borrowing that evidence.

There is one narrow second-freeze case for rollback. An operation may first freeze
its sealed staged candidate for validation, thaw that proof, and select the exact
candidate through its own activation receipt. If the same operation later freezes
that now-selected generation for rollback, the store issues a new durable freeze
incarnation. Replay returns the new proof while it remains current. The consumed
validation proof can no longer thaw the generation or serve as a stage source,
even when no intervening data write changed the dataset revision. A selected freeze
that was merely thawed and reactivated does not qualify for this transition.

## Revisions and staging

Wire-visible entry revisions remain per key and retain their current compare-and-
swap meaning. Each data generation additionally owns a dataset revision. Every
successful semantic `put` or `delete` advances it exactly once. Reads and
no-change deletes do not. `FrozenData.final_revision`,
`StagedData.initial_revision`, and activation validation use this dataset revision.

Stage creates a fresh opaque data reference. Update copies the frozen source at
exactly its final dataset revision, including live entries, deleted-entry
tombstones, and their per-key revisions. Install starts a new empty generation.
The source is never modified. Staged data is non-serving and cannot be obtained
through `repository_for`.

If a trusted migration is required, the store supplies it an existing
`PluginDataRepository` implementation already bound to that one staged generation
and namespace. The callback can use the established JSON validation, per-key CAS,
quota, and tombstone behavior, but cannot select a serving or retained generation.
The artifact ID already pins the trusted migration definition; this contract adds
no second checksum mechanism. Migration callbacks are engine-selected data
adapters, never arbitrary plugin lifecycle hooks.

No SQLite transaction spans the callback. A failed or interrupted callback leaves
only non-serving staged state. Recovery with the stable operation ID discards or
recreates that stage from the unchanged frozen source, reruns only the staged
migration, and returns the original completed receipt once sealed. Partial staged
state is never selected.

## Activation, disable, removal, and recovery

`activate_selected` validates the expected dataset revision and exact selected
data reference and activation generation. `enabled=True` may make that exact
generation writable only while authority generation synchronization occurs under
the same barrier. This prevents an old captured activation from entering between
authority replacement and data unfreeze.

`enabled=False` is explicit for install, disable, remove, and any non-serving
selection. It must keep data sealed, frozen, or retained and must never open
writes. Remove retains the selected generation and all entry tombstones; it does
not delete user data. Old generations also remain retained after update. Deletion
or export of retained user data is a separate future operation outside this port.

The existing `ExtensionDataLifecycle.thaw` restores prior lifecycle eligibility
after durable restoration intent. Writable admission still requires
`activate_selected(..., enabled=True)` for the exact restored selection under the
shared barrier, so thaw alone cannot resurrect an old activation token.

The external-host acceptance now exercises this contract with packaged Notebook
versions. A successful update copies the frozen dataset into the candidate
generation, retains entry revisions through serving admission and restart, and
exports the retained content. A later candidate-startup failure restores the
selected working generation, which remains readable and writable. The
switched-boundary recovery case also proves that a prior-engine authority row is
revoked before a fresh activation identity admits the durable selected
generation.

## Implementation proof still required

The SQLite owner must prove the freeze/write race, same-operation freeze replay,
stale-generation rejection, frozen-read/mutation behavior, exact dataset snapshot,
tombstone copying, migration crash recreation, non-serving install, retained
remove data, and disabled activation behavior against the real adapter. The
contract tests cover shared-type validation and the required structural surface;
they do not substitute a fake store for those persistence tests.

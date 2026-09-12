# Fixture conditional projection files

`FixtureConditionalProjectionFiles` implements `ConditionalProjectionFiles` over an
explicit on-disk fixture root for tests and isolated qualification.

## Constructor

- `root`: existing directory path supplied by the caller. The adapter rejects
  symlinks in the root path and never chooses a home directory or other default
  root. The root device and inode are recorded for later verification.
- `owned_prefix`: nonempty bytes marker that every write payload must start with
  and every owned on-disk file must contain for replace/delete.

## Directory-descriptor confinement

Every operation walks the fixture root component by component from its absolute
anchor, opening each level as a no-follow directory descriptor relative to its
verified parent and closing intermediates, then verifies the final device and
inode against the construction identity before doing anything else. A symlinked,
missing, or non-directory ancestor is rejected without traversal. The lock file, parent traversal, target reads, temp files,
link/replace/unlink, and parent fsyncs all run relative to verified directory
descriptors, never through the mutable root pathname.

A root that was renamed or replaced before the call is rejected with
`ValueError`, and no lock or target is created under the replacement path. A
swap racing the call stays confined to the verified directory for the same
reason. Symlink parents and targets are rejected without being followed.
Parent descriptors that fail partway are closed before returning, and a second
process holding the lock blocks other compares until it releases.

The lock open retries a bounded number of transient missing-file failures
observed with concurrent directory-relative creation on some platforms, then
re-raises. Lock files that are symlinks or non-regular files are rejected.

## Side effects

The adapter creates a private lock file named
`.model_deck_conditional_projection.lock` under `root` and uses `fcntl.flock` for
cross-process writer serialization. A thread lock alone is not used. Target
files are opened through no-follow file descriptors and must be regular files.

Same-directory temporary files named `.model_deck_conditional_projection.tmp.*`
are allocated with exclusive create, written, fsynced, and removed after
publish or failure.

## Initial create (`expected_sha256=None`)

Absent targets are created with a no-clobber publish: write a temp file, fsync,
hard-link to the final name (fails if the target appeared), fsync the parent
directory, then postcheck the hash. `os.replace` is not used for this path.

An existing file, including a byte-identical one, is `unexpected_existing`.

## Replace and delete

Replace uses temp + `os.replace` with parent fsync. Delete unlinks and fsyncs the
parent. Both paths recheck inode identity immediately before mutation and run an
immediate post-mutation hash/absence check.

Postcheck failures return `postwrite_interference` or `postdelete_interference`,
report the precondition and postcheck hashes, preserve what is on disk, and
never retry or restore blindly.

## Receipt meanings

Receipts are content-free and match `ProjectionFileReceipt` in
`model_deck.engine.projections.file_port`.

## Limits

This mechanism serializes cooperating Model Deck writers. It is not filesystem
compare-and-swap against arbitrary noncooperating external editors. The lock
does not prevent an external process that ignores it from changing a target
between the final precondition check and the mutation.

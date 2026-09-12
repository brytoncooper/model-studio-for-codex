# Archive artifact store

B20 stages inspected ZIP bytes into an extracted directory named by the SHA-256
of the complete archive. It performs no manifest trust, activation, installation,
or entrypoint-pointer changes. Those remain separate gates.

## Ownership and public contracts

The package owns `store.py`, `publication.py`, these exports/docs, and
`python/tests/plugins/test_artifact_store.py`. It depends on standard-library
facilities and the public archive inspector only.

`stage_archive(archive_bytes, *, store_root, limits=None, publisher=None)` returns
a frozen `StageResult` with `artifact_id`, `artifact_path`, `total_entries`,
`total_uncompressed_size`, `already_present`, and `ok`.

- `store_root` must be an explicitly supplied absolute existing directory.
  Every supplied ancestor is opened with `O_DIRECTORY | O_NOFOLLOW`; paths are
  never resolved to hide symlink ancestors. On macOS, callers may resolve a
  known `/var` fixture alias before supplying their test root.
- `limits` configures the public archive inspector. Inspection of the immutable
  source copy completes before filesystem writes.
- `publisher` is a trusted `DirectoryPublisher` callable taking a directory
  descriptor plus source and destination basenames. It must atomically move
  the source without replacing any existing destination and raise
  `FileExistsError` on collision. It must never report success before the move.
- The default adapter uses macOS `renameatx_np(RENAME_EXCL)`. Other platforms
  explicitly fail with `unsupported_platform`; there is no unsafe ordinary
  rename fallback. Platform-specific code lives only in `publication.py`.

`ArtifactStoreError` has stable `code` and content-free `detail` fields.
`ArtifactCorruptedError` denotes a preexisting artifact that fails verification.
Archive inspection errors propagate unchanged. Filesystem failures map to
`stage_failed`; specific guards include `invalid_store_root`, `root_changed`,
`staging_changed`, `corrupted_artifact`, `publish_failed`, `write_failed`,
`tree_limit`, and `unsupported_platform`.

## Confinement and integrity

The root's ancestor descriptors remain open and their identities are rechecked.
Extraction, verification, publication, and cleanup use descriptor-relative
operations. Files use exclusive creation and no-follow opens; directories are
also opened no-follow. Replacing a pathname with a symlink cannot redirect an
operation through that symlink. An observed root/ancestor replacement fails the
call. Returned paths describe the verified root at completion; callers must
revalidate when accessing them later.

Every file has mode `0600`; every artifact directory, including implicit parent
directories, has mode `0700`. All staged files and directories are fsynced before
publication, followed by a root-directory fsync after publication.

Existing artifacts are checked against the entire expected tree, including
implicit directories. Extra or missing entries, symlinks, non-regular files,
wrong modes, and multiply linked files are refused. File contents are compared
byte-for-byte with the inspected archive; CRC equality alone is never trusted.
Verification checks descriptor/path identities and observes file metadata changes.

Publication never overwrites a competing target, including an empty directory.
Concurrent identical publications either publish once or verify the same target.
Only recorded objects under the call's original staging directory are removed
on a prepublication failure. Unexpected replacement objects are not recursively
deleted. A published target is never removed by cleanup. If final directory
fsync fails after publication, the call reports failure but preserves the target;
a later call verifies it normally.

## Bounds and limitations

Extraction and byte comparison use 64 KiB chunks. Implied directory trees add a
store-specific limit of 64 path components and 16,384 total tree nodes, preventing
small ZIP metadata from creating unbounded implicit directory work. Directory
descriptors stay pinned while working; OS resource exhaustion fails staging.

The store is immutable by publication policy, not a tamper-proof filesystem:
the owning user can subsequently modify files or rename directories. Every reuse
revalidates contents and tree shape. No userspace check can freeze a returned
path against changes after that check. Concurrent tampering may leave the owned
staging directory for diagnosis when deleting it would require removing
unrecognized replacement objects. No crash-orphan recovery or garbage collection
is implemented here.

Directory creation cannot atomically return an open descriptor on this platform.
If opening or identifying a newly created directory fails, an empty staging
directory (or empty child within staging) may remain: cleanup cannot safely
claim that pathname. Once descriptor identity is known, a failed pathname
verification attempts cleanup only when the name still matches that identity.
Failure to recheck identity also leaves the directory in place.

Only archives accepted by the archive inspector are read. ZIP comments containing
end-record signatures are normalized solely for the standard-library reader;
entry data and archive identity remain unchanged. No network, child process,
credential lookup, home discovery, or live configuration access occurs.

## Extension and tests

A future platform supplies an equivalent atomic publisher; do not replace the
exclusive publication guarantee with check-then-rename or a cooperating lock.
New trust/activation/install-pointer behavior belongs behind separate contracts.

From the Architecture `python/` directory:

```sh
PYTHONPATH=src python -B -m unittest discover -s tests/plugins -p test_artifact_store.py
```

Tests use only explicit temporary roots and injected failures. They cover full
inspection before output, exact-byte staging/reuse, concurrent publication,
root/ancestor swaps, existing CRC collisions/extra files/symlink parents/modes,
competing empty targets, zero-progress writes, and pre/postpublication failures.

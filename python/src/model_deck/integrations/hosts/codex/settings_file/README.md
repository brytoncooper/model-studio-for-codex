# Codex settings file

`CodexSettingsFile` implements the engine's `SettingsDocumentPort` using the
public [settings document](../settings_document/README.md) parser and editor.
It owns filesystem persistence only. The engine owns authorization, preview
tokens and idempotency receipts. No paths, host defaults or credentials are
discovered, and constructing the adapter does not access files.

Supply explicit absolute `config_file` and `backup_dir` paths and a
`context_specs(source: bytes, exists: bool) -> SettingsFileSpecs` callback.
`SettingsFileSpecs` contains `DocumentContext`, `sections` and `fields`. The
callback must describe those exact bytes/existence, provide stable host and
document identities, and change `context_revision` whenever protection,
descriptors, precedence or inherited context changes. All parent directories
must already exist and every component must be a real directory, without
symlinks. Callers needing a canonical fixture path can resolve it before
construction. The adapter itself never resolves away symlinks.

`read`, `validate`, `preview` and `save` use the engine port's unchanged
signatures. Read returns the snapshot directly; preview omits the engine-owned
token. Save checks identity, present content hash or `absent`, context revision,
candidate hash, writability and protected/known-field constraints under a
cooperating-writer lock, and repeats validation immediately before publication.
Missing and existing-empty files have distinct revisions.

Every operation acquires a persistent `.NAME.model-deck.lock` sidecar. Other
cooperating writers must use that same file and must never replace or delete
it. Directories stay open from root to leaf, file operations are relative to
their descriptors, and symlinks and nonregular targets are rejected. Directory
identity checks detect ancestor swaps; even a swap after the preflight check
cannot redirect descriptor-relative writes through a new symlink. A detected
post-write swap reports failure and does not attempt restoration.

Changed existing files receive a unique recoverable backup with mode `0600`.
Backup contents and their directory are fsynced before publication. Candidate
contents are fsynced in an exclusive temporary file, then atomically replace
the existing target. New targets use exclusive hard-link publication, so a
concurrently created file is never overwritten. Existing target permission
bits are retained; new files use `0600`. A no-op preserves exact bytes and mode,
creates no backup, and does not replace the file. Comments and unknown values
are preserved by the document module's structured edit output.

Failure cleanup only removes temporary files whose identity still matches the
adapter's open descriptor. Completed backups remain recoverable if a later
step fails. No automatic restore or retry runs. A failure after publication
(including directory fsync) has an uncertain outcome; the engine must retain
the uncertain receipt and reconcile by reading. Fixed public errors do not
render raw values, paths or exception causes. Backup display paths are explicit
successful receipt metadata, not error text.

This is not an OS-level compare-and-swap against noncooperating editors.
An external editor can change an existing target after the last comparison
and before replacement. The adapter detects earlier changes and never claims
to eliminate that final race. It also does not freeze external context sources;
the trusted callback's revision discipline is required. Live configuration
discovery, installation, recovery UI and end-to-end host activation are outside
this package.

Focused verification from `python/` with the isolated project dependencies:
`PYTHONPATH=src python -B -m unittest tests.integrations.hosts.codex.test_settings_file`.
The tests use real temporary files plus injected boundary failures, with no
installed app or live data access. Run the document suite and engine settings
suite separately for integration acceptance.

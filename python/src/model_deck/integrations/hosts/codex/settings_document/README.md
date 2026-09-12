# Codex settings document

This pure host module turns caller-supplied TOML bytes, schema descriptors and
context into read, validation and preview results. It owns parsing, lossless
structured edits, known-field validation and protected-field checks. It never
finds or writes files, discovers host defaults, reads credentials or issues
preview tokens. The [host settings service](../../../../engine/host_settings/README.md)
owns authorization and preview admission; the frozen wire contract is
[SETTINGS-API.md](../../../../../../../docs/plans/plugin-architecture/SETTINGS-API.md).

## Adapter contract

Call `read_snapshot`, `validate_candidate` or `preview_candidate` with exact source
bytes, `DocumentContext`, sections and fields. Context includes existence,
content revision, context revision, schema profile and inherited values. Missing
source uses empty bytes and revision `absent`; an existing empty file uses its
SHA-256 revision. Inconsistent context is rejected.

`FieldSpec.toml_path` is an absolute tuple of string keys and optional nonnegative
array indexes. `EntrySpec` gives an explicit stable entry ID, label and child
fields; its child paths remain absolute. The adapter supplies these identities;
the document module does not derive them from positions or values. Structured
changes identify a field plus its optional entry ID. Edit individual entry child
fields; adding/removing entire entry containers is currently unsupported.

Unknown TOML keys remain available to the authorized raw editor. Structured
changes target only injected fields, preserve comments and unknown keys, and
preserve exact bytes for no-ops. Known-field types and declared constraints apply
to both modes. Noneditable fields and protected paths reject changes, including
removal and protected subtree reformatting. No default values are invented.

Secret descriptors expose configured state without values, defaults, effective
values or child entries. Diagnostics use fixed text and optional one-based syntax
locations. If any secret descriptor exists, a changed preview suppresses the
whole display diff with `<redacted>` and `diff_truncated=true`, including unchanged
multiline secret context. Authorized candidate raw TOML still contains exact
bytes; callers must keep that separate from diagnostics and display diffs.

Public read and validation results are checked against their frozen schemas.
Preview results omit the engine-issued `preview_id`; all other fields are
checked using a temporary token in a detached validation copy. The engine must
add its real token before sending a successful preview result.

## Bounds and errors

Source/candidate bytes are limited to 256 KiB, display diff to 128 KiB and result
JSON to the 1 MiB frame budget with envelope reserve. Descriptor and entry counts
are each limited to 256, entry depth to four, sections to 64, changed IDs to 64
and diagnostics to 32. Malformed candidate TOML returns safe validation
diagnostics. Malformed base TOML raises a safe `DocumentError`; recovery editing
of an already malformed source is not implemented by this module.

`assert_save_allowed` checks size and candidate hash only. It is not a save
permission check. A filesystem adapter must repeat context and protection
validation and implement locking, confinement, compare-and-swap, backups and
atomic writing. These guarantees are outside this module.

## Extending and checking

Add host-owned fields and stable entries through the descriptor inputs, with
explicit constraints and sensitivity. Extend the focused test matrix for both
raw and structured edits and validate positive results against frozen schemas.
Do not widen the wire contract from this package.

From `python/`, with `tomlkit` and the contracts dependencies available in an
isolated interpreter, run `PYTHONPATH=src python -B -m unittest
tests.integrations.hosts.codex.test_settings_document`. The tests cover schema
parity, secret omission, protection, lossless edits, absent source, constraints,
entry identities, metadata detachment and bounds. Filesystem integration and
full host settings behavior require separate acceptance.

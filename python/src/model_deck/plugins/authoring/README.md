# Plugin authoring

## Purpose and ownership

This package implements the local author-side `plugin validate` and `plugin
pack` behavior. It validates archive and manifest contracts, reads a bounded
project tree, creates deterministic ZIP bytes, validates the finished archive,
and publishes it without overwriting an existing path. It does not install,
activate, execute, sign, or trust a plugin and does not import the engine.

## Public contracts

- `validate_project_archive(archive_bytes)` returns a `ValidationReport` with
  the archive and manifest inspection results. `report.ok` is true only when
  both inspectors succeed and the declared entrypoint is present.
- `entrypoint_present(report)` exposes the same entrypoint verdict separately.
- `pack_project_archive(project_root, output_path=..., limits=...)` returns a
  `PackResult` only after the published bytes pass the public validator.
- `PackLimits` bounds regular files, traversed directories, decoded input bytes,
  and finished archive bytes. The default values align with archive inspection.
- Host-detectable failures use `AuthoringError` and stable
  `AuthoringErrorCode` values. Manifest inspection exceptions are translated
  to `manifest_read_failed`; the stable inspector code remains in the safe
  authoring error detail.

## Validation result fields

In addition to the archive and manifest inspection results, `ValidationReport`
exposes:

- `schema_bundle_ok`: `True` only when every declared operation schema
  resource is present, readable, valid JSON Schema, and every `$ref` it
  carries resolves inside the bundle. `None` for empty contributions.
- `panels`: a tuple of `PanelValidationFinding` records, one per declared
  panel contribution. Each finding reports whether the resource exists,
  parses, matches the `ui.panel.v1` tree schema, and satisfies the
  semantic rules.

`report.ok` requires all of: archive inspection, manifest inspection,
entrypoint presence, a non-false `schema_bundle_ok`, and every panel
finding. Empty-contributions archives remain valid (`schema_bundle_ok` is
`True`, `panels` is empty).

## Stable error codes

Operation schema failures surface as `OPERATION_SCHEMA_RESOURCE_INVALID`
(missing/unreadable/non-JSON-Schema resource) or
`OPERATION_SCHEMA_REFERENCE_INVALID` (a `$ref` that does not resolve to a
resource in the same bundle).

Panel failures surface under stable codes: `PANEL_RESOURCE_MISSING`,
`PANEL_RESOURCE_NOT_READABLE`, `PANEL_SCHEMA_INVALID`,
`PANEL_STATE_READY_MISSING_ROOT`, `PANEL_DEPTH_EXCEEDED`,
`PANEL_NODES_EXCEEDED`, `PANEL_DUPLICATE_NODE_ID`,
`PANEL_BINDING_NOT_TEXT_INPUT`, `PANEL_PARAMS_BINDINGS_COLLISION`,
`PANEL_ID_MISMATCH`, and `PANEL_OPERATION_UNKNOWN`.

The semantic validator lives in the dedicated
`model_deck.plugins.panel_validation` package so its rules can be reused
without the archive inspector.

## Invariants

Traversal is incremental. The walker retains at most the configured number of
files and subdirectories and stops on the first cap-plus-one entry. Symlinks and
special files are rejected. Directories and files are opened without following
their final path component, their identities and metadata are checked around
the actual read, and each file is read only up to the remaining byte budget plus
one byte. The archive is built from those detached bytes, so later source-tree
changes cannot alter the output.

Entries are sorted by POSIX path. `manifest.json` is first, timestamps and mode
bits are fixed, and every entry uses deterministic DEFLATE settings. The
finished bytes are inspected for path, type, size, compression-ratio, CRC,
manifest, and entrypoint validity before any output is visible.

Publication writes and syncs a temporary file in the destination directory,
then creates the requested path with an atomic hard link. A competing path wins
without being replaced. Temporary files are removed after success or failure.
Directory creation, temporary-file creation, writes, and publication errors are
reported through the authoring error boundary.

The CLI reads at most the archive byte limit plus one byte before validation.
Importing the CLI does not import engine modules. Engine composition is loaded
only by `engine serve`, and the deterministic provider is loaded only by the
run fixture command.

## Extending

Add new archive or manifest rules to their owning inspectors first, then keep
packing's finished-archive validation on the same public pipeline. Extend
`PackLimits` only when the public inspector has a matching bounded guarantee.
Do not add installation or activation behavior here.

## Tests

From `python/`:

```sh
PYTHONPATH=src .venv/bin/python -B -m unittest \
  tests.plugins.test_cli_plugin_authoring \
  tests.plugins.test_schema_bundle \
  tests.plugins.authoring.test_panel_validation \
  tests.plugins.authoring.test_archive_validation
```

The tests use temporary directories and in-memory ZIP fixtures. They cover
determinism, hostile archives, missing entrypoints, bounded reads and traversal,
source and publication races, compression-ratio rejection, no-overwrite
publication, filesystem error normalization, temporary cleanup, and lazy CLI
engine imports.

## Limitations

The packer holds the bounded source payloads and finished ZIP in memory. The
publication link requires the temporary file and destination to share a
filesystem, which the implementation guarantees by creating the temporary file
in the destination directory. Validation does not sign artifacts or prove the
identity of an author.

# `model_deck.plugins.manifest_inspection`

Pure in-memory inspection of plugin manifests. This package is the B18
slice responsible for validating a decoded manifest against the frozen
`contracts/plugin.v1/manifest.schema.json` and exposing a detached,
immutable summary plus stable error codes.

## Purpose

- Validate the schema, entrypoint path shape, contribution identities, and
  plugin API compatibility of an already-decoded manifest mapping.
- Produce a detached `InspectionResult` that holds no reference to the
  input mapping and can be safely shared across threads or retained as a
  long-lived value.
- Reject or surface defects via stable `InspectionError` codes that callers
  can branch on without parsing free-form messages.

## Ownership

`model_deck.plugins.manifest_inspection` is owned by the B18 manifest
inspection slice. It depends only on:

- Standard library (`collections.abc`, `dataclasses`, `typing`).
- `model_deck_contracts.validator` (the bundled schema validator).

It does **not** import from `model_deck.engine.*`, `model_deck.kernel.*`,
`model_deck.adapters.*`, `model_deck.integrations.*`, or
`model_deck.bootstrap`. It does not access the filesystem, spawn
processes, make network calls, or perform plugin discovery. It does not
resolve entrypoint paths against any on-disk location.

## Contracts

### Public surface

| Symbol | Notes |
| --- | --- |
| `inspect_manifest(raw, *, caller_plugin_api_major, caller_plugin_api_minor)` | Schema-validate, perform additional structural checks, return `InspectionResult`. Raises `InspectionError` on the first schema or compatibility defect. |
| `inspect_entrypoint_path(path)` | Pure structural check; raises `InspectionError` on defects, returns the path string on success. |
| `InspectionResult` | Frozen dataclass; `ok` is true iff `errors` is empty. |
| `Identity`, `Api`, `Entrypoint`, `Contributions`, `OperationContribution`, `PanelContribution`, `ProviderContribution` | Frozen dataclasses; no mutable views. |
| `InspectionFailure` | Frozen dataclass; one entry in `InspectionResult.errors`. Carries `code`, `field`, `detail`, `kind`, `id`, and `index`. All entries are themselves frozen so the result is fully immutable. |
| `InspectionError`, `InspectionErrorCode` | Stable error reporting. |

### Error codes

| Code | Meaning |
| --- | --- |
| `schema_invalid` | The document fails schema validation, is not a mapping, or contains a structural defect (e.g. non-string id on a contribution). The `detail` is the fixed string `"manifest failed schema validation"`; the `field` is the schema path reported by the validator when available, otherwise `None`. Caller-supplied failing values are never echoed. |
| `entrypoint_path_invalid` | The entrypoint path is empty, absolute, contains a NUL, contains a Windows drive letter / embedded colon, or contains `.` / `..` / empty segments. |
| `duplicate_contribution` | A contribution id appears more than once within the same section (operations, panels, or providers). Reported via `InspectionResult.errors`, not raised. |
| `incompatible_api_version` | The manifest's `plugin_api.major` does not match `caller_plugin_api_major`, or the caller's minor is below the manifest's declared minimum. |

### Compatibility rule

Compatibility follows the existing `evaluate_api_version` shape:

- `manifest.plugin_api.major == caller_plugin_api_major`
- `caller_plugin_api_minor >= manifest.plugin_api.minimum_minor`

If either fails, `inspect_manifest` raises `InspectionError(code=
"incompatible_api_version")`.

### Path check rule

The schema regex already rejects leading `/` or `\\` and embedded NUL
bytes. The inspector additionally rejects:

- Empty strings.
- Empty path segments (which the schema allows).
- `.` and `..` segments.
- Paths longer than 256 characters.
- Paths containing `:` (Windows drive letters and any other colon
  usage). POSIX entrypoint paths never legitimately carry a colon and the
  schema regex does not forbid it.
- Paths containing `\\` anywhere (already covered by the schema regex
  for paths starting with `\\`, but the inspector also rejects
  embedded backslashes inside a relative path).
- UNC paths (`\\\\server\\share\\...`) are rejected by the
  leading-backslash rule.

The inspector never touches the filesystem, so it makes no claim about
whether the resolved path is a symlink.

### Duplicate contribution rule

Within `contributes.operations`, `contributes.panels`, and
`contributes.providers`, each id must be unique. Duplicates do not raise;
they are accumulated into `InspectionResult.errors` as frozen
`InspectionFailure` instances carrying:

- `code`: always `"duplicate_contribution"`.
- `field`: the JSON path of the offending entry, e.g.
  `contributes.operations[1].id`.
- `detail`: a short human-readable description of the duplicate that does
  **not** interpolate the caller-supplied id (it only states the prior
  occurrence index).
- `kind`: `"operation"`, `"panel"`, or `"provider"`.
- `id`: the duplicate caller-supplied id (structured, never interpolated
  into `detail`).
- `index`: zero-based index of the offending entry in its section.

The caller can report every conflict in one pass; the structured `id`
attribute is the safe place to recover the offending value.

## Invariants

- `InspectionResult` is fully immutable (`frozen=True`) and every
  collection it exposes is a `tuple`. Each entry in `errors` is itself a
  frozen `InspectionFailure`, so the result is safe to share between
  threads and to retain as a long-lived value.
- `InspectionError.detail` for `schema_invalid` is always the fixed
  string `"manifest failed schema validation"`; the failing caller-
  supplied value is never echoed through any field.
- `InspectionFailure.detail` for `duplicate_contribution` never echoes
  the caller-supplied contribution id; the structured `id` attribute
  carries it.
- The inspector never raises non-`InspectionError` exceptions; the bundled
  schema validator's `SchemaValidationError` is wrapped into
  `InspectionError(code="schema_invalid")` at the boundary.
- The schema-boundary raise uses `from None` so the chained
  `SchemaValidationError` is suppressed in Python's standard
  `traceback.format_exception` output. A caller that introspects the
  formatted traceback cannot recover the validator's raw message (and
  the failing caller-supplied value embedded in it). The original
  exception remains reachable via `__context__` for debugging, but is
  never rendered.
- The inspector never reads the host filesystem, never spawns subprocesses,
  and never opens a network socket.

## Extension recipe

1. Add new fields to the frozen manifest schema in `contracts/plugin.v1/`.
2. Add corresponding fields to `models.py` as frozen dataclass attributes.
3. Populate them inside `inspect_manifest` from the validated mapping.
4. Cover them in `python/tests/plugins/manifest_inspection/`.

Public-surface changes are intentionally rare. The package exposes only
the minimum required to validate, summarise, and report defects.

## Tests

Run only the owned test file:

```
PYTHONPATH=python/src /opt/homebrew/bin/python3.12 -B -m unittest \
  -v tests.plugins.manifest_inspection.test_manifest_inspection
```

The fixture used by the positive-path test lives at
`python/src/model_deck_contracts/schemas/fixtures/valid/manifest_minimal.json`
and is loaded through `model_deck_contracts.paths.fixtures_root`, so the
test never reaches into the file system by path string.

## Limitations

- The inspector does not discover, download, or unpack plugins.
- The inspector does not check that the entrypoint file actually exists;
  that is the host's responsibility once a plugin is being loaded.
- The inspector does not evaluate granted permissions; it only preserves
  the raw permission tokens declared by the manifest.
- Compatibility checks compare only the plugin API claim against the
  caller's declared version. Kernel API compatibility is a separate concern
  owned by `model_deck.kernel.registry`.
- `InspectionError.field` for `schema_invalid` may be `None` when the
  bundled validator cannot recover the schema path. This is a property
  of the validator's wrapping, not of the inspector.

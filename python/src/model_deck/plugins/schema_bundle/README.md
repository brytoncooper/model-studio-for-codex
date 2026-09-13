# Plugin schema bundles

This package validates operation input and output schemas shipped inside one
external plugin artifact. Registration code supplies the archive resources as
an explicit mapping of relative paths to bytes. The bundle never reads files,
home directories, URLs, or other plugin archives.

## Public contract

`PluginSchemaBundle.from_resources(resources)` parses, checks, detaches, and
freezes the supplied schemas. `check_reference(reference)` confirms that a
manifest operation schema reference selects a schema in that bundle.
`validate(reference, instance)` validates bounded JSON data and returns a
detached copy.

An invalid resource set or reference raises `PluginSchemaBundleError` with the
fixed message `plugin schema bundle is invalid`. Invalid operation data raises
`PluginSchemaDataError` with the fixed message
`plugin schema data is invalid`. Input details are never included in errors.

## Invariants

- Resource paths and references are relative, local, and traversal-free.
- Every reference resolves to a schema location in the supplied bundle. Local
  cross-file references and JSON Pointer fragments are supported; URI fetching
  has no fallback.
- Schemas use Draft 2020-12 and the keyword and format subset in
  `contracts/schema-subset.json`. `additionalProperties: true` is forbidden.
- A root `$id`, when present, equals its resource path. Nested `$id` values are
  rejected so they cannot change local reference bases.
- Schema resources and validation data have fixed byte, count, shape, and depth
  bounds. Parsed resources and returned data are detached from caller-owned
  objects.

## Extension and integration

The plugin registration boundary should construct one bundle from the verified
archive resources, then check every operation contribution's `input_schema` and
`output_schema`. Invocation code can inject that bundle to validate operation
data. This package does not register manifests or alter the engine contract
schema resolver.

Add a schema keyword or format only after adding it to the canonical contract
subset and confirming that every contract implementation supports it. Keep new
reference forms local and deterministic.

## Tests and limits

Run `PYTHONPATH=src python -m unittest tests.plugins.test_schema_bundle` from the
`python` directory. Tests cover cross-file references, JSON Pointers, strict
paths and references, malformed schemas, fixed errors, detachment, cycles, and
resource and data bounds. Recursive schema graphs are accepted by Draft 2020-12,
but validation that exceeds the runtime recursion bound fails closed as invalid
operation data.

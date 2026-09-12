# Legacy import preview (fixture-only)

Deterministic, redacted, read-only preview of Codex host legacy routing state for migration planning.

## Purpose

Build an in-memory import plan from synthetic fixture directories. The preview inventories legacy JSON and managed TOML sources, derives proposed engine connection/model identities, compares them against caller-supplied repository snapshots, and classifies drift without writing files or executing migration.

## Invariants

- Requires an explicit fixture root path; never reads `~/.codex`, Application Support, Keychain, environment credentials, or implicit defaults.
- Reads each regular file once as bytes, hashes original bytes with SHA-256, then parses.
- Never follows symbolic links; a symlinked fixture-root component is rejected and symlink sources are classified as `symlink` without target reads.
- Never emits raw source documents or secret field values; secret-like field names produce generic malformed diagnostics only.
- Produces stable, sorted collections for repeatable preview output.
- Writes nothing and constructs no mutation commands.

## Public entrypoint

`preview_legacy_import_from_fixture_root(fixture_root, existing_connections=..., existing_models=...)`

Returns `LegacyImportPreviewPlan` with:

- `sources`: inventory rows (`path`, `classification`, `sha256`, `store_version`, optional `detail`)
- `proposed_connections` / `proposed_models`: derived identities with classifications
- `import_ready_*`: subsets whose classification is `managed_match`
- `diagnostics`: sorted free-form notes

Use `LegacyImportPreviewPlan.to_sortable_dict()` for deterministic serialization in tests.

## Classifications

- `managed_match`: source or derived item aligns with legacy rules and existing engine snapshot
- `drift`: same stable identity with differing content or cross-source disagreement
- `foreign`: unrelated or non-managed legacy file retained in inventory
- `malformed`: invalid encoding/parse/secret-field/policy violation
- `symlink`: symlink refused
- `collision`: duplicate model/account routing conflict
- `missing`: expected fixture file absent
- `orphan`: managed item lacks ownership evidence in preferences/endpoints

## Fixture layout

```text
fixture-root/
  preferences.json
  endpoints.json
  display-names.json
  agents/
    openrouter_<role>.toml
```

Managed TOML must include the first-line marker `# Managed by OpenRouter Settings native-agent registration v1`, use `model_provider = "openrouter-settings"`, and contain only the managed provider definition with optional `--token` account UUID auth args (no literal secrets).

## B09 handoff boundary

B08 stops at the preview plan. B09 owns projection/outbox writes that materialize managed TOML from committed desired state with hash preconditions. B08 must not apply changes, launch bootstrap, or invoke credential commands.

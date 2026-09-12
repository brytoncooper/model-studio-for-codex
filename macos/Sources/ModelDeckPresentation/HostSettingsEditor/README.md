# HostSettingsEditor

Pure `@MainActor` presenter/state model for the native host settings editor.

## Purpose

Read a `HostSettingsSnapshot`, edit raw TOML or structured changes, then
validate, preview, and save through the settled `HostSettingsServing`
four-method boundary (read, validate, preview, save, plus cancel).

## Public surface (early, for the AppKit view)

- `HostSettingsEditorState`, `HostSettingsEditorPhase`,
  `HostSettingsEditorMode`, `HostSettingsEditorRow`,
  `HostSettingsPreviewSummary`, `HostSettingsEditorConflict`,
  `HostSettingsEditorError`, `HostSettingsEditorOutcome`
  (`HostSettingsEditorState.swift`).
- `HostSettingsEditorPresenter` methods: `start`, `refresh`,
  `cancelEditing`, `cancel`, `setMode`, `editRaw`,
  `setStructuredChanges`, `setFieldValue`, `unsetField`,
  `setSecretValue`, `clearSecret`, `search`, `requestValidation`,
  `requestPreview`, `beginSave`, `retrySave`, `rawEditorText()->String` (authorized UI access to the canonical raw draft for the AppKit raw editor).

## Invariants

- Server values (base/content hashes, context revision, preview token,
  idempotency key inputs) pass through verbatim; the presenter never
  synthesizes them and never retries a failed call.
- Any draft change invalidates the preview; stale async responses are
  suppressed by generation and report `applied: false`.
- Save is enabled only for a current valid preview with changed intent.
- Dirty cancel restores the last saved snapshot; conflicts preserve the draft.
- Full TOML and secret values stay private; errors carry codes and field IDs.
- Server candidates are authoritative when syncing structured/raw views.
- Unset/inherit (`valueState`) is distinct from set; unknown raw keys ride
  through raw TOML and surface via `unrepresentedPaths`.

## Extend

Add rows or filters against `HostSettingsEditorRow`; add error cases with
safe codes only. New server operations need a frozen contract first.

## Limits

No filesystem or settings-name knowledge; no auto-save retry; unchanged
previews cannot save. Tests use a fake `HostSettingsServing`.

## Repair notes

- Error codes are fixed strings mapped from known `EngineClientError` cases
  (`unknown` otherwise). Server or transport text is never filtered into
  codes, so secret values cannot leak through error states.
- In-flight operations own busy flags separately from the draft generation:
  a stale completion releases its own flags without applying stale data and
  never clears a newer operation's flags.
- A successful save commits the returned revision as the new base snapshot
  (revision, raw TOML, structured, context): cancel and later validate calls
  build on the saved revision, and the editor leaves dirty state.
- The canonical draft always follows the server candidate, in both modes, so
  switching to raw after a structured validate sends candidate TOML.
- A retry success always advances the known base to the saved revision, but an intervening edit keeps its draft: the draft stays dirty and the preview is invalidated, so the next validate builds on the new base.
- `beginSave()` freezes the exact `HostSettingsSaveParams`; `retrySave()`
  resends the stored params verbatim instead of rebuilding from mutable state.

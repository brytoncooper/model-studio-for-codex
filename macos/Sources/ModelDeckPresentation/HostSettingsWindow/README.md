# HostSettingsWindow

Native AppKit settings editor view. Owns its controls, layout, focus and window shortcuts; settings policy remains in the presenter and client.

## Contracts

- Renders `HostSettingsEditorPresenter.state`; every presenter call is
  followed by `render()` (sync calls immediately, async in completions).
- Raw TOML text comes only from `presenter.rawEditorText()`.
- Preview diff is read-only; diagnostics show line/col + code + message.
- Apply/save requires `state.canSave`; the window handles Cmd+S, commits an active field, then chains preview and save. Invalid local numeric/list input blocks this path.
- Scalar controls use plain values; string lists use JSON array text so commas, empty strings and whitespace round-trip without loss. Numbers must be finite, and integer fields reject fractions.
- Unchanged rows retain their controls across rendering. Secret Change/Cancel stays local; submitted secret text is cleared and never reflected from presenter state.
- Raw typing calls `editRaw` only: never validates, previews, saves, or
  restarts anything.
- No hardcoded provider keys or settings defaults; controls derive from the
  supplied row label/editability/type/status. `.entries` rows and any future
  nested entry groups fall back to an explicit "Edit in TOML" button until
  nested controls are specified.
- Unsaved drafts are retained across renders; server-synced candidate text
  never clobbers an in-progress raw edit while the raw view holds focus.

## Limits

- No field-metadata extension yet: rows render one control per row; nested
  entry-group editing is TOML-only.
- Public enum choices use a native picker with their original string/number/Boolean values. Without choices, the text control accepts explicit JSON scalars (strings require quotes); arrays, objects and null are rejected. Unknown existing choice values remain visible until the user selects a supported choice.
- Staged libraries only: no app build, install, live hosts, or real settings.

## UI test

`HostSettingsWindowTests` instantiates views headless with a fake service and
verifies action bindings, disabled save, search filtering, raw text sync, and
accessibility labels, typed round-trips, secret edit lifecycle, focused typing and actual window key equivalents. An offscreen window test checks visible row/raw editor dimensions and writes a PNG; it does not run the installed app. A best-effort additional snapshot writes to the
system temp dir when bitmap rendering is available; it is not claimed as
visual review.

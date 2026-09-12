# ModelDeckPresentation

Pure catalog, saved-connection preferences, and usage aggregation types used by the AppKit settings UI and CLI self-tests.

## Ownership

B04.values owns `Models/` and `ModelDeckPresentationTests/Models/`.

## Public surface

- `CatalogModel`, `ModelBrowserState` — model browser filtering, selection, and generation guards for stale catalog responses.
- `ProviderPreset` — bundled presets via `bundled()` (SwiftPM module bundle) or `bundledFromMainBundle()` for the legacy app bundle.
- `SavedAccount`, `SavedPreferences`, `SettingsError` — persisted settings shapes shared with Usage dashboard.
- `UsageValues`, `UsageRecord`, `UsageTotals`, `UsageProvider`, `UsageFiltering` — usage tab calculations.
- `UsageModelSelfTest.run()` — parity with `--self-test-usage`.

## Pitfalls

- Call `ProviderPreset.bundledFromMainBundle()` from the monolithic app target; use `ProviderPreset.bundled()` inside SwiftPM tests.
- `SavedAccount` must stay public before UsageDashboard splits further.
## Model catalog presenter

`ModelCatalogPresenter` is `@MainActor`. `load` returns models in `ModelCatalogPresenterOutcome` so AppKit owns `ModelBrowserState` and applies `receive(_:generation:)` on the main thread. Stale completions set `applied` to `false` so spinners and status text stay tied to the current load.

# ModelDeckApp (SwiftPM executable)

Symlinks to repository-root `OpenRouterSettings.swift` and `UsageDashboard.swift` preserve a single owner for the AppKit monolith while compiling through SwiftPM.

## CLI self-tests (unchanged flags)

- `--self-test-keychain`
- `--self-test-companion`
- `--self-test-model-browser`
- `--self-test-usage`

Normal launch still ends in `NSApplication.run()` with `OpenRouterSettingsApp`.

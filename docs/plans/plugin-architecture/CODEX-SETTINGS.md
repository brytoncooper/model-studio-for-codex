# Codex configuration editor

User-requested addition to B10 host settings and B25 native interface; required before B26 completion.

## Experience

Provide a polished, searchable native settings screen for Codex config.toml. Group supported settings into understandable sections, including agent concurrency and nesting, model/reasoning defaults, profiles, tools, and other supported configuration. Explain settings with units, valid ranges, defaults, and their effective scope. Do not assume that a concurrency limit means lifetime total agents or that the root is excluded: labels and validation must match the supported Codex version.

Offer synchronized structured controls and a complete raw TOML editor with syntax/error locations. Preserve settings not yet represented by controls, comments, and unrelated formatting. Support nested tables and arrays rather than a fixed shortlist of editable keys. Distinguish unset/inherited from explicitly configured values, and show the target file and configuration precedence. Managed restrictions remain visible and cannot be bypassed by this editor.

## Boundary and contracts

Codex-specific keys, schema/version discovery, precedence, TOML parsing and writing belong to the host adapter. The provider-independent engine must not gain Codex settings vocabulary. Define and review a versioned host-settings operation contract before native integration. Use a supported-version schema/reference; unknown versions retain raw editing with honest validation limits rather than invented defaults.

## Save behavior and invariants

Read a versioned document snapshot; preview the diff before saving. Validate TOML and known settings, use expected-content hashes to detect external edits, and save atomically with a recoverable backup. Never overwrite external changes silently, expose secrets in diagnostics, or rewrite unrelated managed model projections. Define ownership between user-edited configuration and B09 generated projections. Show whether a setting applies to future sessions or requires restart; saving never silently restarts Codex or Model Deck.

All development and acceptance use explicitly injected fixture paths. This addition does not authorize modifying the running toolchain's configuration during the refactor.

## Acceptance and parallel work

Adapter/parser tests cover lossless no-op round trips, unknown keys/comments, nested values, invalid TOML, schema/version limits, inherited values, external-edit conflicts, failed writes, backup/recovery, and secret-safe errors. Native tests cover form/raw synchronization, search, validation, dirty/cancel/save states and keyboard/accessibility behavior. Demonstrate changing a supported agent concurrency setting and an arbitrary raw setting through the GUI against fixtures.

The adapter can be authored independently of sessions/provider work after its host-settings contract is settled. The native editor consumes that implemented contract; one owner per package, one finalizer for acceptance. Document the subsystem and link it from the final README/settings catalog.

Reference: https://developers.openai.com/codex/config-reference

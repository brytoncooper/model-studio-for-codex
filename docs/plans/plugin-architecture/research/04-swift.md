> Non-normative research input. Read [the canonical plan](../PLAN.md), [boundary scope](../BOUNDARIES.md), and [review corrections](../REVIEW.md) before implementing. Recommendations in this report may have been rejected or superseded.

# Swift / AppKit UI decomposition and plugin contributions (research 04)

**Author:** plan_swift subagent (Composer 2.5; Fast mode unverified)  
**Date:** 2026-09-12  
**Scope:** Planning only. Read-only inspection of `OpenRouterSettings.swift`, `UsageDashboard.swift`, `build.sh`. Aligns with research `01-kernel.md` (`ui.native` contribution kind).

## Executive recommendation

Keep **AppKit** as the primary UI stack. Split the monolith into **SwiftPM targets** with a **typed engine client** (versioned JSON envelopes to Python), **feature presenters** that own screen state, and a thin **shell** (`ModelDeckApp`) for window lifecycle, navigation chrome, and composition-root registration of `ui.native` contributions. Extract **window attachment** behind a **`HostWindowAttachmentPort`** so AX logic (`HostWindowTracker`, `WindowReservation`, `CompanionPanelController` at `OpenRouterSettings.swift` ~534–1025, ~778–1025) can evolve or be stubbed in tests without dragging settings code. **Reject** a single `UIOrchestrator` god object, mandatory SwiftUI rewrite, and per-feature `Process()` launch patterns duplicated in the delegate (~2011–2055, ~3067–3133, ~3157–3185).

## Current facts (grounded)

| Concern | Evidence |
|---------|----------|
| God delegate | `OpenRouterSettingsApp` (~1029–3488) holds accounts, catalog, library, usage, companion, tracking, preferences, and all table delegates. |
| Duplicated IPC | `Process()` + `JSONSerialization` for `model_catalog.py` (~2014–2047), `provider_usage.py` (~3070–3100), `codex_settings.py` (~3157–3185); timeouts/kill logic copied. |
| Good isolated state | `ModelBrowserState` (~48–74): route + generation stale-response guard; worth promoting to presenter pattern. |
| Usage UI boundary | `UsageDashboardView` (~354–878): `update(...)` + callbacks; parses ledger-shaped dicts locally; `selfTest()` (~848). |
| Build topology | `build.sh` (~56–57): `swiftc` compiles `UsageDashboard.swift` + copied `OpenRouterSettings.swift` as `main.swift` into one binary; helper is separate `swiftc` target (~27–34). |
| Local ledger read | `routerLedgerEntries` (~2497–2513): Swift reads `Application Support/Model Deck/router-ledger.jsonl` directly—not via engine port yet. |
| Companion / attachment | `trackingQueue` (~1122) off main; UI updates via `DispatchQueue.main.async` (~1004, ~1206, ~1399); permission copy at ~1352. |

## Proposed package targets (SwiftPM)

| Target | Responsibility |
|--------|----------------|
| `ModelDeckSchemas` | Generated or hand-maintained Codable types mirroring `schemas/` envelopes (`schemaId`, `payload`); no AppKit. |
| `ModelDeckEngineClient` | Single subprocess/session policy: spawn Python bridge entrypoint (proposed: `engine_bridge.py` or existing scripts behind one dispatcher), cancellation, byte limits (`UsageOutputBuffer` pattern ~425), decode to typed results. |
| `ModelDeckDesign` | `StudioPalette`, cards, `SearchablePicker`, shared layout helpers (~5–400). |
| `ModelDeckHostAttachment` | `HostWindowAttachmentPort`, `WindowReservation`, `HostWindowTracker`, `CompanionGeometry`; platform implementation `MacAXHostWindowAttachment`. |
| `ModelDeckFeaturesCore` | Presenters: `ConnectionsPresenter`, `ModelLibraryPresenter`, `CatalogPresenter`, `UsagePresenter`, `CodexDefaultsPresenter`; pure state + engine calls. |
| `ModelDeckShell` | `NSApplicationDelegate`, main window, sidebar/companion wiring, navigation registry, loads first-party `ui.native` manifests. |
| `ModelDeck` (executable) | `@main` or `main.swift` shim; links Shell + Features; keeps `--self-test-*` entry points. |
| `OpenRouterCredentialHelper` | Unchanged standalone helper (already separate in `build.sh`). |

**Rejected:** One target per Swift file (too granular); SwiftUI package (no gain for dense settings UI); embedding Python in-process.

## Typed engine client and ownership

**Contract:** UI never imports `local_router` or provider SDKs. All product mutations go through **`EngineClient`** methods mapped to engine use cases, e.g. `fetchModelInventory()`, `applyCodexConfiguration(_:)`, `refreshProviderUsage(accountId:)`, `listEndpointModels(request:)`, with responses as `Result<T, EngineError>` where `T` conforms to shared schemas.

**Ownership:**

- **EngineClient** (actor or serial queue): owns subprocess lifetime, in-flight request IDs, and timeout policy.
- **Presenters** (per feature): own `SavedAccount` selection scope, `ModelBrowserState`, usage account picker independence (see comment ~3049–3050), and map engine DTOs → view models.
- **Views** (`UsageDashboardView`, table cells, companion `contentContainer`): render only; no `Process()`.
- **Shell**: wires presenter callbacks to AppKit actions; registers plugin panels into `StudioPageDocument` slots.

**Migration of `configure`:** Replace `([String: Any]) -> Void` (~3157) with typed `CodexSettingsCommand` enum and envelope encoder; keep Python script names as adapter detail behind bridge.

## Main-thread rules

1. **All AppKit mutation** on main queue; presenters expose `func apply(_ viewState: X)` called only from main.
2. **Engine I/O** on `DispatchQueue.global(qos: .userInitiated)` or `EngineClient` internal queue; completion handlers hop to main before touching `NSControl`/`NSTableView`.
3. **Window attachment** (`HostWindowTracker.update`, `release`): run on dedicated `trackingQueue` (~1122); publish `AttachmentSnapshot` to main for status labels only (~attachmentStatus in companion).
4. **Timers** (`usagePollTimer`, `trackingTimer`): fire on main, trigger async work without blocking main.
5. **Stale async guard:** retain generation/token pattern from `ModelBrowserState` (~62–66) and catalog `generation` check (~2994) in every presenter.

## Host window attachment port

```text
HostWindowAttachmentPort
  requestPermissionExplanation() -> AttributedString
  update(context: AttachmentContext) -> AttachmentSnapshot  // off main
  release() -> ReleaseOutcome
  desiredReservedWidth(collapsed: Bool) -> CGFloat
```

`MacAXHostWindowAttachment` moves `HostWindowTracker`, `WindowReservationRecovery`, and `CompanionPanelController.desiredWidth` contract. **Shell** decides when to enable tracking (`attachedTrackingEnabled`, `CompanionGeometry.shouldTrack` ~3488 area). External plugins do not implement AX; they may contribute **companion content** only.

**Edge cases to preserve:** unresolved cleanup blocks polling (~728–731); manual resize relinquishes ownership (~recovery tests ~3470+); new host window requires release of prior reservation (~734–741).

## Generic plugin UI: commands, forms, panels (no god object)

Kernel contribution kind **`ui.native`** (per 01-kernel) registers:

| Registration | Purpose |
|--------------|--------|
| `NavigationItem` | id, title, symbol, sort order → shell builds sidebar/rail buttons (~navigation ~2595). |
| `PanelFactory` | `(PanelContext) -> NSView` hosted in `StudioPageDocument` / scrollable page slot. |
| `SettingsFormSection` | labeled rows bound to engine commands via presenter-provided `FormBinding`. |
| `Command` | menu/shortcut metadata + `EngineClient` command id (e.g. refresh usage). |

**Composition root** (`ModelDeckShell`) holds `[ContributionID: PanelFactory]` only—no central switch on feature names. First-party features register the same APIs as external bundles (signed `.bundle` in `Extensions/` proposed later).

**Rejected:** Plugin-direct Keychain access; plugins spawning their own Python; shared mutable singleton for “current account.”

## Five-state UX contract (all async surfaces)

Standardize presenter → view phases (usage, catalog, library inventory, Codex status, attachment):

| Phase | User-visible behavior | Catalog example (~2961+) |
|-------|----------------------|---------------------------|
| **Idle** | Prompt to act; controls enabled | No connection selected |
| **Working** | Progress indicator; cancel only if engine supports | `catalogLoading`, progress animating |
| **Ready** | Data shown; secondary warnings allowed | Models listed; orange note if `suggested` |
| **Empty** | Explicit zero state; CTA | Zero models after successful load |
| **Failed** | Error copy + recovery actions | Retry / Authorize buttons visible |

**Degraded/stale:** Usage keeps prior totals when refresh fails (~3124–3128)—surface as **Ready** with banner, not **Failed**, unless no prior data.

## Migration map

| Phase | Move | Risk |
|-------|------|------|
| M0 | Add SwiftPM package; `build.sh` invokes `swift build` or keeps `swiftc` with multiple sources per target | Low; no behavior change |
| M1 | Extract `ModelDeckDesign` + `UsageDashboard` unchanged | Visual regression |
| M2 | Introduce `ModelDeckEngineClient` + schemas; wrap `codex_settings.py` only | Settings save/load |
| M3 | `CatalogPresenter` + `ModelBrowserState`; delegate calls presenter | Catalog race bugs if generation dropped |
| M4 | `UsagePresenter`; ledger read via engine port or documented dual-read shim | Account mismatch |
| M5 | `ModelDeckHostAttachment` target; companion callbacks unchanged | AX permission regressions |
| M6 | Shell navigation registry; split `studioPages` (~1093) into registered panels | Navigation order |
| M7 | First external `ui.native` sample (read-only panel) | Signing/distribution TBD |

## Atomic implementation slices (4–8)

1. **SwiftPM skeleton + Design extraction** — Targets compile; app binary unchanged. *Acceptance:* proposed `swift build` succeeds; `--self-test-usage` still passes.
2. **EngineClient + Codex settings envelope** — Replace one `configure` call site. *Acceptance:* golden JSON round-trip test in Swift; status line matches prior `displayStatus` (~3187).
3. **CatalogPresenter** — Own `ModelBrowserState` + load/retry/authorize. *Acceptance:* `--self-test-model-browser` unchanged; manual catalog load not required in CI.
4. **Model library inventory path** — Typed `fetchModelInventory()` replaces inline `model_catalog.py` block (~2011). *Acceptance:* filter table populates from fixture JSON unit test.
5. **UsagePresenter + dashboard boundary** — `performUsageRefresh` logic moves; dashboard stays dumb. *Acceptance:* `UsageDashboardView.selfTest()` pass.
6. **Host attachment port** — Move tracker classes; shell injects port. *Acceptance:* existing `--self-test` reservation block (~3430+) pass without AppKit launch.
7. **Navigation registry** — Register Overview/Connections/Library/Usage pages via table. *Acceptance:* preview args `--render-preview` show same page count.
8. **ui.native stub plugin** — Second panel in manifest registers duplicate “About” read-only. *Acceptance:* kernel manifest parse + panel appears in rail without editing shell switch.

## Failure and edge cases

- **Preview modes** (`--render-preview`, `--preview-attached-companion` ~1099–1100): engine client returns structured errors; presenters short-circuit to **Idle** copy.
- **Account switch during in-flight usage** (~3108–3112): must re-fetch or discard; presenter owns guard.
- **Registration batch** (`registerBatch`): stays on main-coordinated presenter; engine owns atomicity.
- **Python missing / wrong version** (`build.sh` ~6–9): engine client surfaces install error once at launch.
- **Ledger tail truncation** (~2510–2512): document as engine/ledger port behavior; UI should not reimplement tail parsing after M4.

## Dependencies and open questions

- **Depends on:** schema/envelope report (bridge JSON), kernel registry for `ui.native`, Python engine port boundaries (01-kernel).
- **Open:** Single bridge script vs multiple legacy scripts; Swift codegen from `schemas/`; whether companion embeds same `sharedBody` views or duplicate hierarchy; plugin code signing; `@MainActor` on presenters vs manual `DispatchQueue.main` (recommend presenters as `@MainActor` classes with `nonisolated` engine callbacks).
- **Explicit non-goals in this slice:** Changing AX permission strings; rewriting `UsageDashboard` graphics; SwiftUI settings; running `build.sh` during planning.


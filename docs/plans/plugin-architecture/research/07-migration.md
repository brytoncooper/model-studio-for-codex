> Non-normative research input. Read [the canonical plan](../PLAN.md), [boundary scope](../BOUNDARIES.md), and [review corrections](../REVIEW.md) before implementing. Recommendations in this report may have been rejected or superseded.

# Storage ownership and safe migration (research 07)

**Author:** plan_migration subagent (Composer 2.5; Fast mode unverified)  
**Date:** 2026-09-12  
**Scope:** Planning only. Read-only inspection of Model Deck source; no runtime, build, or live data mutation.

## Recommendation

Treat **Model Deck Application Support** as the host authority for product state, **Keychain** as the sole secret store, and **`~/.codex/agents/openrouter_*.toml`** as a **managed Codex compatibility projection** of the model library—not a second library. Introduce a single **MigrationCoordinator** (engine port) that runs **detect → snapshot → validate → apply** transactions with **no automatic deletes**, **no writes to paths in use by a running router**, and **explicit user consent** for anything that removes or rewrites user data. Reject dual-write between Swift UI and Python without a versioned manifest; reject plugin direct writes to Codex agents, Keychain, or host JSON.

Aligns with kernel report 01 (one authoritative store per concern) and plugins report 03 (`plugin://` storage broker, host-owned catalog).

## Grounded current state

| Concern | Location | Writers today | Notes |
|--------|----------|---------------|--------|
| Support root | `routing_registry.support_directory()` → `~/Library/Application Support/Model Deck` | Python + Swift | One-time rename from `Codex OpenRouter`; log/ledger absorb (`routing_registry.py` ~58–86). |
| Connections UI state | `preferences.json` (`OpenRouterSettings.swift` ~1145, ~2714) | Swift | Accounts, selected account/model, favorites; **no keys in file**. |
| Endpoint wire metadata | `endpoints.json` (`routing_registry.py` ~245–268; Swift ~2716–2720) | Swift | Keyed by account UUID or base URL; router reads via registry. |
| Model library (runtime) | `~/.codex/agents/openrouter_*.toml` | `codex_settings.write_managed_agent` (~173–212), registry read (~329–371) | Marker `# Managed by OpenRouter Settings…`; `reject_secret_fields` (~224–229). |
| Picker selection | `picker-selection.json` | `RoutingRegistry.select` (~396–414) | Temp file + `os.replace` + fsync. |
| Display names | `display-names.json` | `set_display_name` (~303–316) | Bad file ignored on read. |
| Codex global config | `~/.codex/config.toml` + `state.json` + `original-config.toml` | `codex_settings.transaction` (~103–113, ~382–442) | `fcntl` lock; rollback on failure. |
| Provider continuation | `provider-continuation.sqlite` beside ledger (`local_router.py` ~1186) | `ProviderContinuationStore` | SQLite; scope by endpoint+account+model (`provider_continuation.py` ~25–28). |
| Usage / audit | `router-ledger.jsonl` (append) | `LocalRouter` | Not authoritative for routing. |
| Secrets | Keychain (`KeychainCredentials`, account id) | Swift helper subprocess | Python never persists bearer tokens in TOML/JSON. |

**Import / conflict behavior today:** Unowned agent TOML blocks overwrite (`codex_settings.py` ~181–183). Same model on another connection is rejected when `preserve_registered_route=True` (~191–199). Concurrent edit: byte-compare before write (~209–211). External Codex config edits: `touched(config) != state["last"]` blocks apply/restore (~414–415). Registry load skips non-managed agents but **fails closed** on invalid managed files (~341–348).

**Gap for plugin architecture:** Model library authority is split across Swift preferences, `endpoints.json`, and Codex TOML; plugin catalog SQLite is proposed in 03 but not present in tree. No unified migration version field across host files.

## One source of truth (target)

1. **ModelLibrary** (engine): canonical records `{modelId, role, endpointRef, effort defaults, managed marker version}`.  
2. **ConnectionCatalog**: canonical connection rows; `endpoints.json` becomes a projection or is merged into one `connections.json` with schema version.  
3. **CredentialRefs**: Keychain items keyed by stable `connectionId` (UUID); files hold refs only.  
4. **CodexAgentsProjection**: export/import TOML from ModelLibrary; Codex may hold copies but host reconciles on activate.  
5. **PickerState**: `picker-selection.json` remains host-owned (small, UX-local).  
6. **ContinuationStore** + **UsageLedger**: host-owned; plugins get no write access without `storage` capability scoped to `plugin://`.  
7. **CodexGlobalSettings**: remain user/Codex-owned; Model Deck only transactional overlay for OpenRouter provider table (`codex_settings`).

**Rejected:** Letting plugins or MCP paths write `openrouter_*.toml`; mirroring full preferences into `~/.codex`; automatic purge of continuation SQLite on upgrade; silent merge when TOML and library diverge.

## Transaction strategy

**Pattern A — Host JSON/TOML (existing):** `mkstemp` → write → `fsync` → `os.replace`; symlink rejection; `0o600` on secrets-adjacent files (`routing_registry.py` ~303–316, `codex_settings.atomic_write` ~55–68).

**Pattern B — Multi-file logical transaction (extend `codex_settings.transaction`):** Compare-and-swap on all participants before commit; on any failure, restore prior bytes for each participant; surface `SettingsError` if rollback incomplete (~103–113).

**Pattern C — Migration batch (new, proposed):**  
- `migrations/manifest.json` in support dir: `{schemaVersion, pending[], applied[], lastSnapshotId}`.  
- **Snapshot:** copy metadata listing + optional tar of support files (exclude active `router.log` tail if router running—copy at rest or skip with warning).  
- **Apply:** each step is idempotent; steps record `checksum_before` / `checksum_after`.  
- **Runtime rule:** migration runner refuses to modify files opened for append by live process (ledger) unless operator passes `--offline` or router stopped—planning default is **refuse while router active**.

**Codex TOML projection sync:** On library change, enqueue `ProjectionStep` that writes only managed files; import from disk runs **ImportScan** that classifies: `managed_match`, `managed_drift`, `foreign_owned`, `orphan_projection`. Only `managed_drift` gets auto-repair when drift is marker-safe; `foreign_owned` and conflicts require UI confirmation.

## Backup, rollback, active runtime

- **Settings rollback:** Keep `original-config.toml` + `state.json` semantics; extend with `snapshots/<id>/` for host JSON bundles before schema bumps.  
- **No touch active runtime:** Planning constraint from BRIEF carries into implementation: migration code must not kill router/Codex; reads may be stale; writes target quiescent files or deferred queue until next host restart.  
- **Credential safety:** Snapshots never include Keychain; restore rebinds by `connectionId` only.  
- **Continuation:** Backup SQLite with WAL checkpoint (proposed `sqlite3 .backup`) during offline window; restore is user-initiated because it affects in-flight tasks (`provider_continuation.py` ~130–142).

## Failure and edge cases

- Dual support folders (`Model Deck` + legacy): continue absorb-then-rmdir; migration should not delete legacy until absorb succeeds.  
- User hand-edits managed TOML: next registration may fail identity check—surface repair path (remove/re-add), not silent overwrite.  
- Partial migration after crash: manifest `pending` step reruns or rolls back using snapshot id; never leave half-renamed schema without manifest entry.  
- Plugin storage quota exceeded: block activate, not host catalog writes.  
- Import from Codex agents dir with secrets: `reject_secret_fields` remains mandatory at projection boundary.

## Atomic implementation slices (7)

1. **Storage inventory schema** — Add `support/storage-manifest.v1.json` describing authoritative paths and roles (read-only generator CLI proposed).  
   **Acceptance:** Manifest lists all seven host concerns above with `authority` vs `projection`; generator exits 0 on dev machine; no files modified.

2. **ModelLibrary port without format change** — Engine API wraps `RoutingRegistry.load_models` / remove / display names; Swift still writes TOML via existing paths.  
   **Acceptance:** Existing `test_routing_registry.py` / display-name tests pass unchanged (proposed gate).

3. **Projection reconciler** — `ImportScan` + `ProjectionExport` with conflict classes; no auto-delete foreign TOML.  
   **Acceptance:** Fixture dir with foreign + managed + drift → CLI report JSON matches expected classes; zero files deleted by default.

4. **Unified write transaction wrapper** — Extract compare-and-swap helper shared by registry JSON and settings paths.  
   **Acceptance:** Simulated concurrent write test raises before replace; successful write leaves `0o600` and no temp files.

5. **Migration manifest + snapshot** — Create snapshot id, copy listed JSON/SQLite; append `applied` record.  
   **Acceptance:** Two runs with same schema version second run no-op; snapshot restorable in dry-run verify mode.

6. **Connections consolidation plan** — Schema v2 merging `preferences.accounts` + `endpoints.json` with migration step that **imports** into new file and keeps old files read-only aliases for one release.  
   **Acceptance:** Migration step on sample preferences produces byte-equivalent routing when loaded through adapter; old files untouched until user confirms promote.

7. **Plugin storage boundary** — Enforce `plugin://` writes only under `plugins/<id>/data/`; deny `agents_dir`, Keychain, ledger paths in broker policy table.  
   **Acceptance:** Policy unit tests (proposed) reject path escapes; host catalog writes still succeed.

Worker Editing-check: **NONE** (per BRIEF). Proposed finalizer gates: registry tests, import-scan fixtures, migration dry-run.

## Dependencies and open questions

- **01-kernel / 03-plugins:** ModelLibrary port ownership and plugin catalog SQLite timing.  
- **Swift UI:** Whether preferences migration runs in app launch or Python-only engine.  
- **Codex:** Whether future Codex versions change agent file layout—projection version field must be explicit.  
- Unresolved: Single vs dual composition roots writing host files; whether ledger remains non-migratable append-only forever; hot migration while router idle vs hard offline requirement.

**Stop boundary:** This report does not implement migrations, alter live Application Support, or restart processes.

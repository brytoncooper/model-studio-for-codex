> Non-normative research input. Read [the canonical plan](../PLAN.md), [boundary scope](../BOUNDARIES.md), and [review corrections](../REVIEW.md) before implementing. Recommendations in this report may have been rejected or superseded.

# External plugin platform (research 03)

Date: 2026-09-12. Planning only. Owns third-party plugin runtime and SDK acceptance path.

## Firm recommendation

Introduce a **kernel** (identity, discovery, lifecycle, capability authorization, protocol compatibility) and require **first-party features to register as modular builtins** using the same manifest and ports. **Third-party logic runs out-of-process only**, supervised by the Python engine over newline-delimited JSON-RPC (consistent with `OpenRouterSettings.swift` stdin/stdout JSON ~3165–3169 and `model_deck_mcp.py` stdio MCP). AppKit does not load foreign native code for plugin behavior in v1.

Agent-facing tools stay on the **Codex/Cursor boundary**: the router already flattens Codex-supplied function tools and rejects freeform/custom tools on OpenRouter paths (`local_router.py` ~118–125). Plugins expose **host operations** and UI; they do not inject tools into Codex unless a future explicit, user-approved port exists. `cursor_agent.py` (~5–6) disables Cursor's file, shell, and MCP tools—preserve that separation.

Rejected: unsigned in-process `import`; one universal `Plugin` interface; unrestricted event bus; plugin writes to `~/.codex/agents` or Keychain; duplicate authoritative stores beside `routing_registry.py` / `support_directory()` (~57–69).

## Modular builtin vs dynamic third-party

**Builtins** ship in the bundle (`builtin: true`), load via host-controlled Python/Swift composition roots, and track `host_api_version` with the app release. They must use the same capability tokens as external plugins so contracts are real.

**Third-party** plugins install under proposed `~/Library/Application Support/Model Deck/plugins/<id>/<version>/` with `modeldeck.plugin.json`. Enable means **spawn child** + handshake. UI is **declarative** (panel schema + host rendering), not arbitrary AppKit plugins.

## Sandbox guarantees (honest)

| Topic | v1 behavior |
|-------|-------------|
| Crash isolation | Child process; supervisor disables on crash loop |
| Filesystem | Host maps `plugin://<id>/` to `plugins/<id>/data/`; deny `~/.codex`, Keychain, `endpoints.json` by default |
| Network | Default deny; optional `network:outbound` via host proxy |
| Secrets | Host-mediated only (credential helper pattern in `model_deck_mcp.py` ~44–47, 147–149) |
| macOS sandbox | **Not claimed** until Seatbelt/entitlements exist; v1 = process isolation + permission manifest |

## Manifest, lifecycle, negotiation

Proposed `modeldeck.plugin.json` (`plugin_manifest@1`): `id`, semver `version`, `host_api_version` range, `entrypoint`, `capabilities` (operations, events, ui_panels, jobs), `permissions` (default deny), acyclic `dependencies`.

Lifecycle: discover → validate (schema + DAG) → install (atomic) → enable (handshake timeout ~10s) → update (side-by-side version + migration hook) → remove. Failures: handshake timeout or >3 crashes/5min → disabled until user reset.

## Storage, dependencies, jobs

Storage: host broker under per-plugin data dir; quota (proposed 50MB). Registry and model library remain host-authoritative.

Dependencies: cycles rejected at install with edge list; runtime uses topological order only.

Jobs: host `JobSupervisor` for `job.submit` / cancel / progress events; plugin crash marks running jobs failed; idempotent tokens for retry.

## Operations, events, UI

- **Operations**: typed `operation.<ns>.<name>` with JSON Schema; callable from Swift/settings, not auto-exported as Codex tools.
- **Events**: namespaced `plugin.<id>.<topic>`; schema-checked; no global firehose.
- **UI**: `ui_panels[]` with placement + schema version; actions via operation RPC with confirmation for destructive work.

MCP (`model_deck_mcp.py`) remains a **builtin Codex bridge**, distinct from the plugin control protocol (per BRIEF).

## Runtime and SDK quickstart

Kernel extracted from router/MCP duplication; `local_router.py` (~1,392 lines) and `provider_bridge.py` subprocess bridge (~377–378) keep routing/provider roles. Proposed dev CLI `mdk plugin init` + `mdk plugin test` (schema lint, handshake, echo operation). **SDK acceptance**: new author completes quickstart in <15 minutes against a stub host.

WASM third-party runtime deferred (signing/FFI cost). Shared worker pool vs one-process-per-plugin: **unresolved**.

## Migration

0) Label MCP as builtin `catalog.mcp_bridge` (no behavior change). 1) Kernel catalog + supervisor. 2) User plugin dir + install UI with dev warning. 3) SDK + optional signing.

## Atomic slices (8)

1. **Manifest schema + validator** — reject bad fields; detect dependency cycles; semver range check.  
2. **Catalog + state machine** — install/enable/disable/remove under `support_directory()/plugins/` (proposed).  
3. **Supervisor + stdio handshake** — child spawn; kill -9 does not kill router.  
4. **Storage broker** — path traversal and secret access fail closed.  
5. **Operations + event bus** — schema-validated invoke/publish/subscribe.  
6. **Job supervisor** — submit/cancel/progress; crash → failed job.  
7. **Swift plugin UI** — list plugins, errors, enable toggle via engine JSON command.  
8. **SDK quickstart + conformance CLI** — template + `mdk plugin test` for external authors.

Slice dependencies: 1 → 2 → 3 → {4,5,6}; 7 on 2–3; 8 on 1,3,5.

## Unresolved

Code signing before non-dev enable; kernel package boundary vs `local_router.py`; UI panels Swift-only vs constrained WKWebView; Swift DADS-style boundary enforcement (TS suffix tooling does not apply).

## Evidence limits

Read-only inspection only; no tests/builds/provider calls. Proposed commands labeled proposed. Worker Editing-check: NONE.

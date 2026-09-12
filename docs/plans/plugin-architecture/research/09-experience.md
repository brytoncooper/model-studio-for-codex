> Non-normative research input. Read [the canonical plan](../PLAN.md), [boundary scope](../BOUNDARIES.md), and [review corrections](../REVIEW.md) before implementing. Recommendations in this report may have been rejected or superseded.

# Experience layer: novel feature plugins (research 09)

Date: 2026-09-12. Planning only. Owns product/developer journeys and extensibility acceptance for features beyond inference/catalog.

## Firm recommendation

Use **Session Notebook** as the reference **external feature plugin** that proves layered novelty: a user-owned scratchpad tied to Codex threads (metadata only by default), with **commands**, a **native panel**, **plugin-scoped storage**, **background jobs**, and **narrow engine reads**—plus a **CLI consumer** of the public `engine.v1` API. Success means shipping this feature **without** editing `local_router.py` routing branches, `OpenRouterSettings.swift` feature switches, or shell navigation `switch` tables (per research `04-swift.md` navigation registry ~M6–M8). First-party Notebook ships as `builtin: true` with the **same manifest** as third-party authors.

Rejected: notebook as MCP-only tools inside Codex (blurs approval boundary per `02-api.md` ~84); notebook reading chat transcripts without explicit `content.thread_snapshot` grant; in-process Swift plugin code for v1 (`03-plugins.md`); storing notes in `~/.codex` or router ledger (`routing_registry.py` / `support_directory()` remain host-authoritative per `03-plugins.md`).

## Reference feature: Session Notebook (what it is)

| Surface | Behavior |
|---------|----------|
| **Panel** | `ui.native` contribution: rail item "Notebook", `PanelFactory` renders list + editor in `StudioPageDocument` slot (`04-swift.md`). |
| **Commands** | `notebook.create`, `notebook.append`, `notebook.link_thread` registered as engine operations callable from menus and panel actions—not auto-exported as Codex tools (`03-plugins.md`). |
| **Data** | Host storage broker: `plugin://com.example.session-notebook/` → SQLite or JSONL under `Application Support/Model Deck/plugins/.../data/`; quota 50MB (`03-plugins.md`). |
| **Jobs** | `job.submit` for "export notebook to Markdown" and "reindex tags"; progress events `plugin.com.example.session-notebook.job.progress` (`03-plugins.md`). |
| **Engine access (scoped)** | `engine.v1` **read-only**: `catalog.list` (model labels for display), `health.ping`, optional `usage.refresh` only if user grants `capability.usage.read` in manifest. **No** `inference.respond`, **no** app-server pipe (`02-api.md` ~98). |
| **CLI consumer** | Proposed `mdk notebook list --json` calls loopback `engine.v1` operations exposed for automation authors; plugin implements server side via child JSON-RPC (`03-plugins.md` SDK). |

**Default deny on content:** Notebook stores user-typed text and **opaque thread/turn ids** the user explicitly links via "Attach current thread" (host passes ids from turn metadata already visible to AppKit—`local_router.py` `x-codex-turn-metadata` ~639–646—not full message bodies). Reading message text requires manifest permission `content.thread_snapshot` with one-time consent UI in shell; denied → command returns `error.code=capability_denied`.

## Product journey (end user)

1. **Discover** — Settings → Extensions lists `Session Notebook` with version, requested permissions summary, and source (builtin vs user folder `~/Library/Application Support/Model Deck/plugins/` per `03-plugins.md`).
2. **Install** — Builtin preinstalled; external path: user drops signed bundle or runs `mdk plugin install ./session-notebook` (proposed); kernel validates manifest DAG and schema.
3. **Enable** — Toggle on; supervisor spawns child, handshake ≤10s; rail shows Notebook icon from manifest `NavigationItem` without shell code change.
4. **Use** — User opens Notebook panel (**Idle** → **Working** while loading notes → **Ready**). Creates note, links thread id from companion context menu. Optional: run "Export" job (**Working** on job row → **Ready** with file path or **Failed** with retry).
5. **Disable / remove** — Disable stops child and hides rail item; data retained. Remove deletes plugin dir after confirmation; storage broker wipes `data/` atomically.

**Five-state contract** (`04-swift.md`): Plugin panels receive `PanelState` from host presenter driven by plugin RPC: `idle | working | ready | empty | failed`. Host renders standard progress, empty copy, and error recovery; plugin supplies strings and optional CTA operation ids only—no custom spinner chrome required.

## Developer journey (external author)

1. `mdk plugin init --template session-notebook` (proposed) scaffolds `modeldeck.plugin.json`, Python entrypoint, and schema stubs.
2. Author declares capabilities: `operations`, `ui_panels`, `jobs`, `storage`, permissions `[]` until they add `network:outbound` or `content.thread_snapshot`.
3. `mdk plugin test` runs manifest lint, handshake against stub supervisor, invokes echo operation, validates panel schema (`03-plugins.md` slice 8).
4. Local dev loop: install to user plugins dir, enable in app, iterate without rebuilding Model Deck binary (child process only).
5. Publish: document permissions in README; optional signing gate before non-dev enable (unresolved in `03-plugins.md`).

**Docs DX:** Single quickstart page: manifest fields → first operation → first panel field → `mdk plugin test` green. Conformance checklist mirrors kernel acceptance: no imports of `local_router`, `provider_bridge`, or Keychain.

## Extensibility acceptance criteria ("no core edits")

A novel feature plugin **passes** when all hold:

| # | Criterion | Verification (proposed) |
|---|-----------|---------------------------|
| E1 | New rail page + settings section from manifest only | Add third-party test manifest; UI appears with zero `OpenRouterSettings.swift` edits |
| E2 | New engine operations routable to plugin child | `operation.notebook.*` invoke returns schema-valid payload |
| E3 | Persistence isolated under broker path | Grep host data dirs: no writes outside `plugins/<id>/` |
| E4 | Jobs survive plugin restart policy | Crash marks job failed; idempotent resubmit documented |
| E5 | 5-state panel driven by plugin RPC | UI test fixture: forced `failed` shows Retry wired to operation id |
| E6 | Disable/remove lifecycle | Disable hides UI; remove deletes data per broker API |
| E7 | CLI uses public `engine.v1` only | `mdk` binary links no private `plugin.v1` supervisor symbols |

Failure of E1–E2 implies kernel/shell gaps—not "just add another switch."

## Permissions and persistence semantics

- **Permissions** are manifest-declared, host-enforced at RPC boundary; runtime grant audit log (proposed sqlite table `capability_grants`).
- **Persistence:** plugin-owned blobs versioned with `storage.schema_version` in manifest; migrations via `plugin.migrate` hook on enable (`03-plugins.md`). Host never mirrors notebook rows in engine catalog.
- **Secrets:** none in notebook v1; export job writes to user-picked path via host file picker port (future `capability.file.export`).

## Failure and edge cases

- Handshake timeout → plugin row **Failed** in Extensions UI with "Reset & retry".
- User denies `content.thread_snapshot` → link stores id only; panel shows "Metadata only" badge.
- Engine unreachable → panel **Failed**; CLI exits non-zero with `engine.unavailable`.
- Dependency plugin disabled → notebook disable cascade or read-only mode (recommend **disable cascade** with explicit message).
- Quota exceeded → append operation returns `storage.quota_exceeded`; UI **Ready** with banner.

## Migration implications

Phase 0: Document Session Notebook as **target acceptance fixture**, not shipped product. Phase 1: After kernel slices 1–3 (`03-plugins.md`), land builtin notebook as manifest-only first-party. Phase 2: External author beta with dev signing waiver. No change to Codex `openai_base_url` or MCP catalog until notebook proven.

## Atomic implementation slices (6)

1. **Notebook manifest + builtin registration** — `modeldeck.plugin.json` with operations/panel/job declarations; kernel catalog lists it. *Acceptance:* `plugin.validate` passes; no new Swift `switch` on page id.
2. **Child process + storage broker CRUD** — `notebook.create/list` RPC; data under broker path only. *Acceptance:* integration test writes 100 notes, enforces quota.
3. **Swift panel host binding** — Declarative panel schema → AppKit list/editor; 5-state wiring. *Acceptance:* fixture JSON drives **Empty** and **Failed** screenshots (proposed snapshot test).
4. **Thread link command (metadata-only default)** — Host supplies turn/thread ids; no transcript without grant. *Acceptance:* denied grant returns stable error code.
5. **Job supervisor export** — Submit Markdown export job, progress events, cancel. *Acceptance:* kill child mid-job → job **failed** surfaced in panel.
6. **CLI `mdk notebook`** — List/create via `engine.v1` HTTP on loopback. *Acceptance:* scriptable JSON round-trip without launching AppKit.

Dependencies: slices 1–2 require research `03-plugins.md` slices 1–4; slice 3 requires `04-swift.md` M6 navigation registry; slice 6 requires `02-api.md` engine HTTP surface.

## Unresolved questions

- Whether thread ids alone are considered "content" under enterprise policy.
- WKWebView for rich notebook editor vs AppKit-only (`03-plugins.md`).
- Should CLI be Python-only or thin Swift wrapper in bundle.
- Sync/export to iCloud—out of v1.

## Evidence limits

Read-only inspection per BRIEF; no builds, tests, or provider calls. Worker Editing-check: NONE. Proposed commands labeled proposed.

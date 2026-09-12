> Non-normative research input. Read [the canonical plan](../PLAN.md), [boundary scope](../BOUNDARIES.md), and [review corrections](../REVIEW.md) before implementing. Recommendations in this report may have been rejected or superseded.

# Host independence and delivery adapters (research 06)

**Author:** plan_hosts subagent (Composer 2.5; Fast mode unverified)  
**Date:** 2026-09-12  
**Scope:** Planning only. Grounded in read-only inspection of Model Deck source; coordinates with kernel report `01-kernel.md` without owning persistence or registry on-disk design.

## Recommendation

Treat **hosts** as replaceable **delivery surfaces** that expose Model Deck’s engine to an outer runtime (Codex desktop today; headless CLI, MCP-only, or another IDE later). The product keeps a **single authoritative model library and routing policy** in the engine (`ModelLibrary` / `RoutingRegistry` semantics). Codex-specific artifacts (`~/.codex/agents/openrouter_*.toml`, per-process `-c` overrides, app-server JSON-RPC) become **compatibility projections** maintained by a **`host.integration` adapter**, not a second library. Avoid patching the host bundle, global `~/.codex/config.toml`, or Codex internals; all integration stays in Model Deck-owned launchers and loopback overrides, matching current behavior in README and `provider_bridge.py`.

## Grounded current state

**Codex desktop host.** `codex_runtime.discover_runtime()` (`codex_runtime.py:9–24`) accepts only `com.openai.codex` under `/Applications/ChatGPT.app` or `Codex.app`. The AppKit app launches it with `CODEX_CLI_PATH` set to the bundled `CodexProviderBridge` (`OpenRouterSettings.swift:3270–3272`), not by replacing the host binary.

**Bridge + loopback delivery.** `provider_bridge.py:1` documents the split: native desktop JSON-RPC on stdin/stdout, loopback model router behind Codex’s OpenAI connection. When argv lacks `app-server`, the bridge re-execs the discovered Codex executable (`provider_bridge.py:407–409`). In app-server mode it wraps `ProviderBridge` with `RoutingRegistry()` and asyncio JSON lines (`provider_bridge.py:412–414`). Router overrides are injected via `LocalRouter.codex_arguments()` (`local_router.py:1229–1231`): `openai_base_url` pointed at `http://127.0.0.1:{port}/…` and `features.enable_request_compression=false`. The router thread serves Codex HTTP while the bridge translates app-server methods (`local_router.py:1233–1244`, handler from ~647+ per BRIEF).

**Registry coupling today.** `routing_registry.py:1` reads “managed native agents” from `~/.codex/agents/openrouter_*.toml` (`load_models` ~337+). Registration flows through Swift → JSON → `codex_settings.register_agent` (~216+) which **writes** TOML with marker `# Managed by OpenRouter Settings…` (`routing_registry.py:13–14`). UI copy still tells users agents live under `~/.codex/agents` (`OpenRouterSettings.swift:2388`). Engine, bridge, and MCP all construct their own `RoutingRegistry()` instances (`provider_bridge.py:414`, `model_deck_mcp.py:69`, `local_router` constructor paths), so behavior depends on shared files rather than a declared port.

**MCP delivery.** `model_deck_mcp.py:1–4` is stdio JSON-RPC for Codex agents; it registers models and reads catalogs without holding keys (Keychain via `ModelDeck --token`). The bridge injects MCP only for the launched Codex process via `mcp_server_arguments()` (`provider_bridge.py:74–81`, merged in `backend_arguments` ~368). Instructions duplicate routing policy also embedded in bridge text (`provider_bridge.py:18–52` vs `model_deck_mcp.py:26–33`).

**Headless / CLI-shaped entrypoints (partial).** `codex_settings.handle()` (`codex_settings.py:321+`) is already a JSON action dispatcher (register, runtime_info, cursor SDK, endpoint_models) invoked from Swift with stdin JSON (`OpenRouterSettings.swift:3157–3169`). `provider_bridge` and `model_deck_mcp` are invokable as Python modules without AppKit if paths and env are set. There is no unified “Model Deck CLI” facade yet; delivery logic is duplicated across bridge, MCP, and settings scripts.

**Cursor as execution host (not Codex replacement).** Cursor SDK runs in isolated subprocesses (`cursor_sdk_runtime.py:1–13`, broker ~256+). Codex remains the conversation owner; the router adapts tool namespaces and streaming (`CURSOR-INTEGRATION.md`, `local_router.py:991+`). This is a **provider wire** concern for routing, but it constrains host adapters: only Codex today supplies task permissions, spawn_agent, and MCP approval boundaries.

## Host capability model

Define a versioned **`HostCapabilities`** record each adapter publishes at activate time:

| Capability | Codex desktop (current) | Headless CLI (target) | Other app (future) |
|------------|-------------------------|------------------------|---------------------|
| Process launch / attach | Yes (`CODEX_CLI_PATH`, `open`) | N/A (engine only) | App-specific |
| App-server or equivalent IPC | JSON-RPC lines (`ProviderBridge`) | Engine HTTP or stdin API | TBD contract |
| Per-host config overrides | `-c openai_base_url`, MCP table | Env / flags | Must not write global host config |
| Model picker / catalog injection | `/models`, `model/list` via router | List command | Host-native list API |
| Tool approval boundary | Codex task permissions | Explicit policy in CLI | Host-defined |
| Usage / subscription reads | app-server usage paths | Optional omit | Adapter-specific |
| Multi-agent spawn | Native spawn_agent + bridge rewrite | Same engine port, different wire | Only if host supports |

**Constraints (non-negotiable):** no host bundle patches; no silent fallback when model id unknown (bridge routing text ~26–27); symlink rejection aligned with registry (`routing_registry.reject_symlinks` ~114); WebSocket refusal → SSE fallback documented in README (~69); bridge protocol tied to installed app-server (reverify after Codex updates). Host adapters may declare **`requiresProtocolVersion`**; kernel refuses activation on mismatch (per `01-kernel.md` compatibility section).

## Compatibility adapter vs invasive host change

**Adopt:** `CodexDesktopHostAdapter` as first-party `host.integration` plugin implementing:

1. **Runtime discovery** — wrap `discover_runtime`.
2. **Launcher composition** — env `CODEX_CLI_PATH`, bridge path, optional companion UI hooks (Swift calls engine port instead of embedding bridge paths).
3. **Bridge session** — owns `ProviderBridge` lifecycle: router start/stop (`provider_bridge.run` finally calls `router.stop()` ~402), pending catalog wait (`local_router.wait_for_catalog` ~1336+).
4. **Projection sync** — engine emits “managed agent” snapshots; adapter writes TOML projection and rolls back on failure (transaction pattern like `codex_settings.transaction` ~102+).
5. **MCP bundle registration** — declarative MCP server spec (today’s `mcp_server_arguments`) keyed off engine catalog fingerprint (`local_router.catalog_fingerprint` ~1378+).

**Reject:** editing Codex’s reserved collaboration schema without bridge translation (documented in `CURSOR-INTEGRATION.md`); teaching Swift about OpenRouter HTTP details; second registry in MCP or bridge.

**Public API paths (engine-owned, host-agnostic):**

- `ModelLibrary` — list/register/remove models, display names, endpoint metadata (replaces direct `RoutingRegistry` imports in adapters).
- `InferenceRouting` — route by model id, translate streams (router handler).
- `HostBridgePort` — start/stop loopback, `codex_arguments()` equivalent, catalog readiness.
- `SettingsPort` — transactional host-adjacent edits (today `codex_settings`) without exposing TOML paths to plugins.

Host adapters call these ports; they do not import `local_router` handler internals. **Authoritative store** for “what models exist” lives in engine persistence (future); **Codex TOML** becomes a projection refreshed from engine events (aligns with BRIEF “Model Deck owns model library”). Persistence format and migration timing belong to registry/storage research—not expanded here beyond stating **one write path** through engine and **one projection writer** in the Codex adapter.

## Failure and edge cases

- **Codex already running** before integrated launch: companion opens but router state unknown (`OpenRouterSettings.swift:3265–3268`); adapter should surface explicit “quit and relaunch through Model Deck” rather than silently attaching.
- **Stale router in live host** after app update: README warns installed build does not replace loaded router (`CURSOR-INTEGRATION.md` end); host adapter must expose health/fingerprint mismatch.
- **Catalog race**: spawning before `/models` served causes “unknown model” (`local_router.wait_for_catalog` ~1336–1353); adapter coordinates with bridge hold.
- **Encrypted collaboration fields**: bridge rejects unsupported encrypted agent assignments (`CURSOR-INTEGRATION.md`); host adapter version must track Codex plaintext contract.
- **MCP + bridge duplication**: conflicting instructions if engine roster and MCP text diverge; single `routing_instructions(registry, …)` source (`provider_bridge.py:41–52`) should move to engine and feed both delivery surfaces.
- **Headless without Keychain UI**: MCP and settings already use `--token` subprocess to app executable; headless host must require explicit credential helper path or fail closed.

## Migration implications (conceptual)

1. Introduce `HostBridgePort` without changing launch UX — shim `LocalRouter.codex_arguments` and router start/stop behind port.
2. Move `routing_instructions` and model roster formatting to engine; bridge and MCP consume the same string.
3. Split `ProviderBridge` into generic **JSON-RPC duplex adapter** + Codex method mapping table (versioned).
4. Swift **Launch Codex** calls engine “prepare host session” returning launcher env dict (bridge path, support dir, fingerprint).
5. Register first-party Codex adapter via kernel manifest; `provider_bridge.main` becomes thin composition root.
6. Later: add `HeadlessHostAdapter` exposing proposed `model-deck serve` (HTTP + optional stdio) for CI and automation—no AppKit, same `ModelLibrary`.
7. Registry projection: engine commit triggers adapter `syncProjection(hostKind=codex)`; stop dual writes from `register_agent` and `RoutingRegistry` without engine coordination (coordinate with registry report).

## Atomic implementation slices (8)

1. **`HostCapabilities` schema v1** — JSON Schema + validator (proposed `validate-host-manifest`); documents Codex desktop profile. *Acceptance:* fixture Codex profile validates; unknown fields rejected.
2. **`HostBridgePort` extraction** — wrap `LocalRouter.start/stop/base_url/codex_arguments/wait_for_catalog` without handler split. *Acceptance:* `test_local_router.py` unchanged; bridge still starts router.
3. **Single routing instruction builder** — engine function consumed by `provider_bridge.routing_instructions` and MCP `SERVER_INSTRUCTIONS`. *Acceptance:* golden text snapshot test (proposed); `test_model_deck_mcp` tool list unchanged.
4. **`CodexDesktopHostAdapter` skeleton** — manifest kind `host.integration`; wraps discovery + env builder only. *Acceptance:* `runtime_info` action returns same paths as today via port.
5. **Bridge composition root** — `provider_bridge.main` loads kernel + Codex adapter instead of hard-coded `RoutingRegistry()`. *Acceptance:* `test_provider_bridge` green (proposed run in implementation phase).
6. **Launch handshake API** — Swift calls Python `prepare_codex_launch` JSON action replacing scattered `configure` paths for launch only. *Acceptance:* manual checklist: Launch Codex sets same env as before (no automated UI in planning phase).
7. **Projection sync hook (read-only first)** — engine computes desired agent set; adapter diffs against TOML without writing; logs drift. *Acceptance:* diff detects manual TOML edit.
8. **Headless adapter spike** — proposed CLI entry `python -m model_deck_host serve --stdio` documented only; routes to same `HostBridgePort` without `discover_runtime`. *Acceptance:* design doc + stub module importing engine ports only (no second registry).

**Worker test commands:** NONE (BRIEF). **Proposed finalizer checks:** existing `test_provider_bridge.py`, `test_codex_runtime.py`, `test_model_deck_mcp.py`, `test_routing_registry.py` after implementation.

## Dependencies and unresolved questions

- **Kernel (`01-kernel`):** `host.integration` kind registration, protocol compatibility gates.
- **Registry/persistence report:** owns canonical model store layout and TOML projection policy.
- **Provider wire report:** Cursor/OpenRouter branches remain in `provider.wire`, not host adapter.
- **Swift UI report:** companion window, settings pages calling engine JSON actions.

**Open questions:** Should MCP remain Codex-only or also serve other hosts? Is dual composition root (Swift UI plugins vs Python host plugins) acceptable? How to version app-server method mapping when Codex adds methods—central compatibility matrix vs feature detection? Extension signing for third-party host adapters? Whether usage dashboard’s app-server calls (`UsageDashboard.swift`) move under host adapter or separate `datasource.usage` plugin?

**Stop boundary:** This report does not specify SQLite/JSON engine store, TOML field-by-field mapping, or MCP security sandbox—only delivery boundaries and adapter contracts.

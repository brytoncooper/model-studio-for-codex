> Non-normative research input. Read [the canonical plan](../PLAN.md), [boundary scope](../BOUNDARIES.md), and [review corrections](../REVIEW.md) before implementing. Recommendations in this report may have been rejected or superseded.

# Public engine API vs plugin control API

Date: 2026-09-12. Scope: API contracts for Model Deck extensible architecture. Read-only grounding in current Swift/Python/process boundaries.

## Recommendation summary

Split contracts into three layers, not two monoliths:

1. **Engine public API** — stable, versioned operations the AppKit shell and first-party features call to run turns, read catalog/usage, and observe engine health. Transport may stay HTTP Responses-shaped on loopback for Codex compatibility, but the *contract* is schema-owned by Model Deck (`engine.v1`), not ad hoc dict keys in Swift.
2. **Plugin control API** — kernel-only JSON-RPC (stdio or UNIX socket) for install/enable/disable, capability grants, manifest validation, and protocol compatibility checks. No inference, no tool execution, no Keychain writes except through existing credential ports.
3. **Host integration bridges** (existing, gradually narrowed) — Codex `app-server` proxy (`provider_bridge.py`), MCP catalog server (`model_deck_mcp.py`), and Cursor SDK broker (`cursor_sdk_runtime.py`). These remain *adapters* behind engine ports; external plugin authors never implement them directly.

Reject: a single "Plugin" interface covering UI + routing + MCP; duplicating Responses event shapes inside every extension; letting extensions subscribe to a global event bus without capability tokens.

## Current boundary inventory (grounded)

### Swift UI → Python batch JSON (control plane today)

`OpenRouterSettings.swift` spawns Python with stdin JSON and reads stdout JSON:

- **Model library** — `model_catalog.py`, `{}` on stdin, 29s timeout with terminate/kill (`OpenRouterSettings.swift` ~2014–2044).
- **Usage** — `provider_usage.py`, `{"account_id": ...}` (`OpenRouterSettings.swift` ~3068–3105).
- **Codex disk settings** — `codex_settings.py`, `configure(["action": ...])` (`OpenRouterSettings.swift` ~3155–3175; handler `codex_settings.py` 321–373: `status`, `apply`, `restore`, `register_agent`, `endpoint_models`, `cursor_status`, etc.).

Envelope pattern: `{"ok": bool, "error": string?, ...}` (`codex_settings.py` 466–476). No request id, no streaming, no cancellation mid-flight except process kill.

### Codex runtime → loopback Responses HTTP (engine data plane)

`LocalRouter` sets `openai_base_url` to `http://127.0.0.1:{port}{CODEX_BACKEND_PREFIX}` (`local_router.py` 1248–1250). `RouterHandler._handle` (`local_router.py` 686–716) routes by registered model to OpenAI passthrough or `_route_endpoint`.

- **Primary path**: `POST .../responses` with JSON body; **streaming** via SSE (`SseParser`, `encode_event`, `local_router.py` 468–505, 812–822) or chunked passthrough.
- **WebSocket upgrade** explicitly refused (426) so Codex falls back to HTTP streaming (`local_router.py` 687–689).
- **Errors**: `RouterError` → failed handler; SSE `failed_event` with `invalid_prompt` / `server_error` (`local_router.py` 428–434, 973–977).
- **Wire transforms**: `prepare_request` / `restore_event` for agent messages (`agent_message_wire.py` 44–114); chat fallback `chat_wire.py` for non-Responses endpoints (`local_router.py` 29, `routing_registry.py` 134 `WIRE_FORMATS`).

Turn context arrives on headers: `x-codex-turn-metadata` JSON (`local_router.py` 639–646).

### Codex app-server JSON-RPC (host bridge)

`ProviderBridge` (`provider_bridge.py` 95–120) newline-delimited JSON to Codex backend. Model Deck intercepts `thread/start`, `turn/start`, `turn/interrupt`, `config/*`, `model/list` (`provider_bridge.py` 238–365).

- **Cancellation**: `turn/interrupt` also calls `router.cancel_cursor_turn(thread_id, turn_id)` (`provider_bridge.py` 238–245; `local_router.py` 1215–1219).
- **Concurrency**: per-thread `asyncio.Lock` for `thread/settings/update`, `thread/resume`, `turn/start` so approvals/interrupts are not queued behind unrelated work (`provider_bridge.py` 348–352).
- **Errors**: `BridgeError` → JSON-RPC `-32000` (`provider_bridge.py` 358–361).

Comment at `local_router.py` 48: Codex owns **execution, approvals, and tool availability**; the router owns routing and wire repair only.

### MCP (feature plugin prototype, not kernel)

`model_deck_mcp.py` stdio JSON-RPC (`serve`, 473–488). Protocol versions pinned (`PROTOCOL_VERSIONS` 23–24). Tool results are JSON text in `content` (`handle_message` 451–458). **Approval boundary** documented in `SERVER_INSTRUCTIONS` (line 30): Codex remains approval gate; MCP config sets `default_tools_approval_mode` / per-tool overrides via `mcp_server_arguments` (`provider_bridge.py` 69–76).

`idempotentHint` on tool annotations (`model_deck_mcp.py` 432) is documentation for Codex, not engine-level idempotency keys.

### Cursor SDK subprocess broker

`CursorSdkProcess` uses newline JSON commands: `start`, `tool_result`, `cancel` (`cursor_sdk_runtime.py` 506–516). Event queue `maxsize=1024` with blocking put loop (`cursor_sdk_runtime.py` ~177–183) — implicit **backpressure** (drops not applied; producer blocks). Tool results from Codex forwarded via `tool_result` command.

## Target contracts

### Engine public API (`engine.v1`)

**Ownership**: Python engine module (future `engine/` package) exposes operations; Swift UI and plugins consume via generated or hand-maintained stubs. JSON Schema in `schemas/engine/v1/`.

| Operation family | Purpose | Streaming | Cancel token |
|-----------------|---------|-----------|--------------|
| `catalog.list` | Registered models, prices, benchmarks snapshot | no | n/a |
| `usage.refresh` | Account-scoped usage (replaces provider_usage stdin) | no | yes (abort job) |
| `inference.respond` | Codex-compatible Responses request against router | SSE event stream | `inference.cancel` |
| `settings.read` / `settings.write` | Narrow app prefs, not full Codex TOML | no | n/a |
| `health.ping` | Router listening, catalog served flags | no | n/a |

**Streaming/backpressure**: Standardize on SSE-framed `engine.v1` events that are a superset mapping to OpenAI Responses events (adapter layer keeps Codex working). For subprocess bridges, require bounded queues with defined policy: `drop_oldest` for telemetry, `block_with_timeout` for tool callbacks (align with Cursor broker today).

**Error model** (uniform envelope):

```json
{"ok": false, "error": {"code": "model_unregistered", "message": "...", "retryable": false, "details": {}}}
```

Map existing: `RouterError` → `routing.*`, `SettingsError` → `settings.*`, `DeckError` → `catalog.*`, `BridgeError` → `host.*`.

**Idempotency / reconnect**: Engine operations that mutate registry (`add_model` class) accept optional `idempotency_key` (UUID) stored 24h in sqlite; replay returns same result. Inference reconnect is **out of scope for v1** — document that Codex owns turn state; engine exposes `inference.cancel` only.

**Tool result approval ownership**: Unchanged principle — **host (Codex) approves tool execution**; engine and plugins supply tool definitions and results. Plugin control API may declare `required_approval_mode` in manifest; enforcement stays in host config injection (`mcp_server_arguments` pattern), not in engine inference path.

### Plugin control API (`plugin.v1`)

**Ownership**: Kernel process or in-engine supervisor thread. JSON-RPC 2.0, newline-framed, max line 1MB (match `model_deck_mcp.py` MAX_LINE).

| Method | Behavior |
|--------|----------|
| `plugin.list` | Installed manifests, versions, capabilities |
| `plugin.enable` / `plugin.disable` | Lifecycle; disable does not uninstall |
| `plugin.validate` | Schema check manifest + requested capabilities |
| `capability.grant` | Host-mediated; records audit entry |
| `protocol.negotiate` | Returns supported `engine.v1` / MCP / Responses adapter versions |

External authors ship a **manifest** (JSON Schema) + entry binary/script; they do **not** receive loopback Responses or app-server pipes unless granted `capability.engine_proxy` (first-party only initially).

### Language-neutral external author SDK

Publish **OpenAPI 3.1** for `engine.v1` HTTP (loopback) and **JSON Schema** for `plugin.v1` RPC. Codegen targets: TypeScript, Python, Swift (for in-app plugins). MCP remains optional adapter for Codex-shaped tools; document that MCP is not the plugin kernel interface.

## Failure and edge cases

- **Subprocess hang**: Swift kills after timeout (catalog 29s, usage 34s); engine API should expose explicit `deadline_ms` and structured `timeout` errors instead of orphan processes.
- **Partial SSE**: `SseParser` incremental parse; endpoint routes emit `failed_event` if provider ends early (`local_router.py` 977).
- **Encrypted agent history**: `reject_encrypted_agent_messages` before external providers (`local_router.py` path via `agent_message_wire.py` 116+).
- **Catalog race**: `wait_for_catalog` blocks `thread/start` up to 8s (`provider_bridge.py` 328–331) — engine API should surface `catalog.not_ready` to UI.
- **Config version mismatch**: `expectedVersion` on batchWrite (`provider_bridge.py` 222–224) — plugin settings must use same optimistic concurrency.

## Migration implications

Phase 1: Extract stdin actions from `codex_settings.py` into documented `engine.v1` ops without moving files. Phase 2: Swift `configure()` calls engine HTTP or unified Python module instead of per-script processes. Phase 3: Register MCP and Cursor broker as internal plugins with manifests; routing registry becomes `catalog` port implementation. Codex `openai_base_url` override remains until Codex supports explicit engine port.

## Atomic implementation slices

1. **Schema package** — Add `schemas/engine/v1/*.json` and `schemas/plugin/v1/manifest.json`; CI check references only (no runtime). *Check*: `rg` validates all documented ops have schemas.
2. **Error envelope helper** — Python `engine_errors.py` mapping exceptions to codes; wrap `codex_settings` responses. *Check*: unit tests for map table only (proposed).
3. **Catalog operation** — Unify `model_catalog.py` output shape under `catalog.list` response schema; Swift consumes new keys behind feature flag. *Check*: diff golden JSON from current catalog stdout.
4. **Inference adapter interface** — Formalize `translate_request` / `ChatStreamTranslator` as `InferenceWireAdapter` port with `responses` and `chat` implementations (`local_router.py`, `chat_wire.py`). *Check*: existing router tests pass unchanged (proposed gate).
5. **Cancel registry** — Centralize `cancel_cursor_turn` + future inference handles behind `inference.cancel`. *Check*: interrupt path still invokes cursor cancel (manual/scripted).
6. **Plugin manifest validator** — Standalone CLI reading manifest JSON; no load/enable. *Check*: rejects missing capability declarations.
7. **Approval metadata in manifest** — Extend manifest with `tools[].approval_mode`; kernel validates but host still enforces. *Check*: validator fixtures for approve/prompt/none.
8. **SDK publish stub** — `docs/sdk/README` + exported OpenAPI for `catalog.list` and `health.ping` only. *Check*: openapi lint (proposed).

## Dependencies

- Registry/schema work (`routing_registry.py` model entries) blocks catalog op uniformity.
- Kernel lifecycle design (parallel research) blocks `plugin.enable` semantics.
- DADS port naming should align consumer-owned narrow ports; avoid TypeScript-only suffix rules in Swift/Python.

## Unresolved questions

- Should `engine.v1` HTTP share the router port or a separate supervisor socket?
- Whether external plugins ever get `inference.respond` proxy or only MCP/tool surfaces.
- How `idempotency_key` interacts with Codex-side tool call IDs on retry.
- Opt-in streaming buffer policy when queue is full (block vs drop) for non-tool events.

## Alternatives rejected

- **gRPC everywhere** — Codex and MCP are JSON newline/stdio today; migration cost outweighs benefit for v1.
- **Single Swift Plugin protocol** — violates independent evolution; UI plugins need AppKit, engine plugins need Python.
- **Engine-owned tool approval** — contradicts established Codex boundary (`local_router.py` 48, `model_deck_mcp.py` 30).

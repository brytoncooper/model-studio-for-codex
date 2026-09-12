> Non-normative research input. Read [the canonical plan](../PLAN.md), [boundary scope](../BOUNDARIES.md), and [review corrections](../REVIEW.md) before implementing. Recommendations in this report may have been rejected or superseded.

# Provider and session engine extraction (research 05)

**Author:** plan_providers subagent (Composer 2.5; Fast mode unverified)  
**Date:** 2026-09-12  
**Scope:** Planning only. Read-only inspection of Model Deck Python router, bridge, and wire modules.

## Recommendation

Extract a **session routing engine** (model selection, turn metadata, ledger, credentials, continuation store) from HTTP transport, then plug in **four first-party provider wires** with the smallest shared contract: *accept a Codex Responses-shaped turn and yield Codex Responses SSE events*. Do **not** unify Cursor SDK agent semantics, OpenAI subscription passthrough, OpenRouter translation, and chat-completions fallback behind one interchangeable "LLM client." Host-owned tool execution stays in Codex; provider wires may only **request** tool calls and must pause until Codex supplies `function_call_output` (Cursor path today at `cursor_agent.py` ~248–310).

Register wires as engine contributions (`provider.wire` in research 01), selected by registry endpoint metadata (`wire`, `openrouter`, `cursor`, `base_url`) plus `route_for_model` (`local_router.py` ~76–84).

## Grounded current state

| Concern | Where it lives today | Notes |
|--------|----------------------|-------|
| HTTP + routing dispatch | `RouterHandler._handle` ~686–714 | Chooses `openai` vs `endpoint`; compaction detected early |
| OpenAI / ChatGPT passthrough | `_route_openai` ~718–843 | Agent-message rewrite, catalog inject, reasoning heal, spawn tools |
| OpenRouter / generic HTTP endpoint | `_route_endpoint` ~847–988 | `translate_request`, wire `responses` vs `chat`, auto-fallback 404/405/501 |
| Cursor SDK harness | `_route_cursor` ~990–1055, `cursor_agent.py`, `cursor_sdk_runtime.py` | Subprocess broker; tools = Codex callbacks only (module docstring ~1–7) |
| Request/response translation | `local_router.py` ~118–345, `chat_wire.py` | Tool flatten/mangle, `translate_event`, foreign-safe items for later OpenAI turns |
| Continuation / provider-local IDs | `provider_continuation.py` | SQLite metadata for reasoning fields OpenRouter returns (~1–90) |
| Context compaction (non-OpenAI backends) | `context_compaction.py`, `_compaction_mode` ~1057–1104 | Router-run summarization turn; router-owned `encrypted_content` marker |
| Codex app-server integration | `provider_bridge.py` `ProviderBridge` ~89–403 | Forces `openai` provider + loopback router; virtual model picker writes |
| Endpoint presets & catalog fetch | `provider_connections.py` | Capability parsing ~159–188; no cross-provider suggestion bleed |
| Accounting | `LocalRouter.record` ~1355–1357 | JSONL ledger: route, usage, cursor cost, Codex plan headers |

`LocalRouter` (~1167+) also owns pricing/benchmark side threads, cursor catalog cache for Fast gating (`cursor_fast_models` ~1252–1268), and thread `cwd` memory for Cursor prompts.

## Smallest provider contracts (proposed)

These are **consumer-owned engine ports**, not a plugin LCD interface.

1. **`ProviderWire` (required)**  
   - **Input:** normalized `CodexResponsesRequest` (instructions, input[], tools[], reasoning, service_tier, stream flag) plus `TurnContext` (thread_id, turn_id, agent_name, cwd).  
   - **Output:** iterator of Responses API event dicts (same types Codex already consumes).  
   - **Errors:** map to `response.failed` SSE or JSON for `/responses/compact` (mirror `_send_failed` ~1088–1094).  
   - **Must not:** execute tools locally (except translating call events).

2. **`WireCapabilities` (declared, not inferred)**  
   Per wire id: supports `responses_upstream` | `chat_completions_only` | `cursor_agent`; `compaction`: `none` | `router_summarization`; `tool_host`: `codex` | `passthrough_openai`; `catalog_injection`: bool; `billing_class`: subscription | openrouter_credits | cursor_subscription | unknown_local.

3. **`ContinuationBackend` (optional port)**  
   Only wires that emit provider-native item ids needing round-trip metadata implement `capture`/`restore` (`ProviderContinuationStore` API). OpenAI passthrough and Cursor use different rules; Cursor rejects remote `compaction_trigger` (~234–236 `cursor_agent.py`).

4. **`CompactionDelegate` (optional)**  
   When `compaction == router_summarization`, engine calls `summarization_request` then runs the **same** wire without tools; finishes with `context_compaction.compaction_item` (~1106–1134 `local_router.py`).

5. **`UsageSink` (engine-owned)**  
   Wires return usage blobs; engine writes ledger entries. Wires do not append to `router-ledger.jsonl` directly.

**Rejected:** a single `complete(prompt) -> text` adapter; pretending Cursor is "just another Responses endpoint"; auto-executing MCP/tools inside provider subprocesses.

## Semantic preservation (non-equivalence)

| Path | Distinct behavior to keep explicit |
|------|-----------------------------------|
| **openai** | Upstream owns real compaction/reasoning encryption; router sanitizes foreign reasoning (~347+) and heals rejected encrypted items (~393+). |
| **openrouter** | Service-tier → model variant (`openrouter_model_for_request` ~228+); tool namespace mangling (`flatten_tools` ~118+); items made foreign-safe before Codex stores them (~286+). |
| **endpoint chat** | `ChatStreamTranslator` rebuilds Responses events; `wire_overrides` persist chat fallback per `base_url` (~1190, ~911–917). |
| **endpoint responses** | Native continuation capture on completed items (`translate_event` ~305+). |
| **cursor** | Prompt envelope + role preservation (`_prompt_message` ~24–70); session resume at tool boundary; interrupt cancels matching turn (`ProviderBridge.dispatch` ~238+); cost from SDK `charged_cents` (~371+ `cursor_sdk_runtime.py`). |

Conformance cases should be **per wire**, derived from existing tests (not rerun here): `test_local_router.py` (routing, compaction, catalog, OpenRouter, Cursor), `test_cursor_agent.py` (tool boundary, identity, interrupt), `test_provider_bridge.py` (openai transport, legacy routes), `test_provider_connections.py` (catalog capabilities, no cross-provider fallback).

## Host-owned tool execution

Codex remains the **only** tool executor for routed models:

- Endpoint wires forward `function_call` events with restored namespaces; execution happens on Codex's next request with outputs in `input`.  
- Cursor wire registers only tools from the Codex request (`flatten_tools`); on `tool_call`, stream pauses (`cursor_agent.py` ~272–275) until matching `function_call_output` arrives (~186–207).  
- Compaction summarization explicitly strips tools (`context_compaction.summarization_request` ~42–55) because a model-side tool call could not be executed mid-summary.

The engine should expose a **`ToolHostCallback` port** to the bridge layer only—not to provider wires—so future plugins cannot bypass approvals.

## Failure and edge cases

- **Unregistered `provider/model`:** fail before network (`route == unregistered` ~698–699).  
- **Missing Cursor key/account:** clear router error (~1002–1003).  
- **Wire auto-fallback:** only once per base_url for responses→chat; not for Cursor/OpenAI.  
- **Stale Cursor tool callback:** expired `cursor-call-*` tail rejected (~214–217).  
- **Foreign compaction item on routed model:** `message_for_item` → FOREIGN_NOTE (`context_compaction.py` ~92–95).  
- **Continuation store symlink / permissions:** `ContinuationError` (~provider_continuation.py` store init).  
- **Bridge provider mismatch:** thread/start must stay on `openai` provider when router applied (`provider_bridge.py` ~268–270).  
- **Legacy openrouter-bridge-* tasks:** stricter model switch validation (~198–206).

## Migration implications

1. **Phase A:** Move `route_for_model`, `translate_*`, `sanitize_openai_input`, compaction helpers into `engine/session/` package; `RouterHandler` becomes thin HTTP adapter calling `SessionEngine.handle_http(...)`.  
2. **Phase B:** One module per wire implementing `ProviderWire`; `RouterHandler._route_*` delegates to registry lookup.  
3. **Phase C:** `ProviderBridge` depends on `SessionEngine` ports (`ModelLibrary`, `CatalogInjection`, `wait_for_catalog`) instead of concrete `LocalRouter` methods.  
4. **Phase D:** Manifest-register first-party wires; no behavior change, only import boundaries.  
5. Keep `routing_registry.py` validation for cursor URLs and wire format (`validate_cursor_route` ~142–153) on the **library** side; wires read resolved endpoint dicts only.

## Implementation slices (7)

1. **Define `ProviderWire` + `WireCapabilities` schemas** in `schemas/provider-wire-v1.json` (proposed). Map each existing endpoint type to a wire id. **Acceptance:** static table documents openai, openrouter, generic_endpoint_responses, generic_endpoint_chat, cursor_sdk with non-overlapping capability flags.

2. **Extract `SessionEngine` skeleton** from `LocalRouter` (registry, keys, continuation, ledger, thread cwd) without changing `RouterHandler` behavior. **Acceptance:** `LocalRouter` delegates to engine; existing `test_local_router.py` cases remain the parity target (proposed, not run in planning).

3. **OpenAI passthrough wire** — owns `_route_openai` logic including catalog inject and agent-message path. **Acceptance:** parity with tests named `test_openai_*`, `test_catalog_*`, `test_gzip_native_*` in `test_local_router.py`.

4. **OpenRouter + generic endpoint wire** — owns translation, SSE parse loop, chat fallback, continuation capture. **Acceptance:** parity with `test_registered_model_goes_to_openrouter_*`, `test_keyless_local_endpoint_falls_back_to_chat_completions`, compaction tests ~956–1004.

5. **Cursor wire package** — `CursorAgentManager` + SDK runtime behind `ProviderWire`; engine handles `cancel_cursor_turn`. **Acceptance:** parity with `test_cursor_*` in `test_local_router.py` and `test_cursor_agent.py` tool-boundary cases.

6. **Provider conformance harness (proposed)** — table-driven fixtures per wire: minimal request → expected event sequence shape (tool pause, failed_event code, compaction item). **Acceptance:** harness runs in CI separately from HTTP integration tests; documents unsupported combinations (e.g., cursor + compaction_trigger).

7. **Bridge port alignment** — `ProviderBridge` uses `SessionEngine` for `codex_arguments`, `wait_for_catalog`, `price_lines`, `benchmark_lines`, interrupt hook. **Acceptance:** `test_provider_bridge.py` parity list unchanged.

## Dependencies and unresolved questions

- **Depends on:** research 01 kernel/registry kinds; schema envelope work; Swift UI still spawns Python—bridge contract must stay versioned.  
- **Open:** Should `wire_overrides` and continuation SQLite move to per-endpoint plugin state vs engine global?  
- **Open:** Is OpenRouter upstream override (`openrouter_upstream`) a test-only seam or a first-class `HostEnvironment` port?  
- **Open:** Catalog injection split—does each wire contribute catalog rows, or only OpenAI+cursor?  
- **Open:** External third-party wires (corporate gateway)—minimum capability surface without encouraging unsafe tool execution.  
- **Unknown without live calls:** Composer 2.5 Fast behavior; mixed billing proof called out in MEMORY as still unverified—out of scope for this document.

**Boundary:** Session engine = *which wire serves this Codex turn and how events/usage return*. Provider wires = *provider-specific translation and pause semantics*. Host bridge = *Codex process lifecycle and tool approvals*, never provider inference.

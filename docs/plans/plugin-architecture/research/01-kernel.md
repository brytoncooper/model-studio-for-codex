> Non-normative research input. Read [the canonical plan](../PLAN.md), [boundary scope](../BOUNDARIES.md), and [review corrections](../REVIEW.md) before implementing. Recommendations in this report may have been rejected or superseded.

# Kernel vs engine vs plugins (research 01)

**Author:** plan_kernel subagent (Composer 2.5; Fast mode unverified)  
**Date:** 2026-09-12  
**Scope:** Planning only. Grounded in read-only inspection of Model Deck source and DADS `docs/principles.md`.

## Recommendation

Adopt a **thin policy-first kernel** for extension identity, discovery, lifecycle, authorization, and typed registration. The **engine** implements Model Deck use cases behind **application-owned capability ports**. **Plugins** (first-party and external) implement ports or register **contribution kinds** under identical contracts—including novel features (custom MCP tools, corporate gateways, extra native panels) by adding kinds, not kernel code. Reject a universal `Plugin` interface, global service locator, and broadcast event bus.

## Grounded current state

- **`local_router.py` (~1,392 lines):** `RouterHandler` (~647+) mixes HTTP transport, ChatGPT passthrough, endpoint routing, catalog injection, Cursor agent management, pricing/benchmark refresh, and usage ledger (`LocalRouter.record` ~1378+).
- **`routing_registry.py`:** authoritative managed model library under `~/.codex/agents/openrouter_*.toml`; symlink rejection (~114); Codex agent files are a **managed projection**, not a second library.
- **`OpenRouterSettings.swift` (~3,488 lines):** AppKit UI spawns Python with JSON (~2018, ~3073, ~3157)—product and IPC intertwined.
- **`model_deck_mcp.py` / `provider_bridge.py`:** overlap catalog, routing instructions, and `LocalRouter` startup with UI and router paths.

DADS (`dads-framework/docs/principles.md`): independent evolution; application-owned vocabulary; foreign types at edges; narrow consumer ports; composition-only concrete binding; **dynamic loading must not bypass the dependency wall** (§8).

## Kernel (minimal)

**Owns:** (1) extension identity and semver/manifest digest; (2) discovery over bundle and user extension roots with symlink policy aligned to registry; (3) lifecycle load→validate→activate→deactivate; (4) capability authorization (which contribution kinds and host caps: network, filesystem, Keychain namespaces); (5) **typed contribution registry** keyed by `(kind, id)`; (6) protocol compatibility (`requiresKernel`, `requiresEngineApi`, schema versions) with hard refuse on mismatch.

**Does not own:** routing rules, wire translation, TOML semantics, MCP tool meaning, AppKit, pricing, benchmark storage.

## Engine (core product)

Owns use cases as capabilities, e.g. **`ModelLibrary`** (`RoutingRegistry`), **`InferenceRouting`** (`route_for_model` ~76, `RouterHandler._handle` ~680+), **`CodexHostBridge`** (`LocalRouter.codex_arguments` ~1295+), **`UsageLedger`**, **`CredentialAccess`** (`KeychainKeyProvider` ~586+). Engine depends on kernel registry APIs and owned schemas—not vendor SDKs or AppKit.

## Plugins

Ship a manifest; register contributions at activate; implement only matching ports. **Kinds** (extensible): `provider.wire` (OpenRouter, Cursor, passthrough; future vLLM/gateway), `host.integration`, `datasource.catalog`, `tool.mcp`, `ui.native`, `storage.adapter`. First-party modules use the same manifest and APIs—no private backdoors. Arbitrary novel external behavior lands as a new contribution instance or a **new kind** with schema + policy entry, not kernel growth.

## Dependency rules

Adapters (UI, MCP, HTTP) → engine capabilities → kernel registry. Plugins may not import plugins; orchestration stays on engine ports. **No service locator:** `ActivationContext` exposes only allowed `register(...)` for declared kinds. **One composition root** per process (Swift app, Python router subprocess), per DADS §4. **One authoritative store per concern** (model library in engine; TOML as projection). **Cross-process:** versioned schemas in `schemas/` with envelope `{schemaId, payload}` replacing ad hoc dicts.

**Rejected:** giant optional-method `Plugin` (LCD, DADS §10); OSGi-style broker; unrestricted internal event bus—prefer named, versioned callbacks on ports.

## Registry and evolution

Register/query by kind (`providers()`, `mcpToolBundles()`). Duplicate `(kind, id)` fails at activate. Exclusive roles use manifest `priority` plus user override. v1: activate at startup only (matches current router thread model).

**API evolution:** versioned ports; additive schema fields; breaking changes need new port version. **Anti-bloat gate** for kernel changes: needed by ≥2 kinds or both hosts; cannot live in engine without cycle; machine-checkable; conformance test. Deprecation via local manifest telemetry.

## Failures and migration

Invalid manifest or schema mismatch: skip plugin, continue others; user-visible incompatibility message. Mid-request plugin errors map to stable router errors (`RouterError` ~68). Symlinked paths rejected. Keychain only with declared capability and consent.

Migration: extract `ModelLibrary` port without on-disk format change; split `local_router.py` into adapter + engine + `provider.wire` plugins; unify MCP/UI catalog through one engine use case; introduce `schemas/` before broad Swift bridge edits; shim existing modules as first-party manifests.

## Implementation slices (8)

1. **Manifest schema v1** + `validate-extension-manifest` (proposed CLI).  
2. **Kernel registry package** (Python; Swift mirror later)—no `local_router` import.  
3. **Composition root + ActivationContext** in router subprocess—zero-plugin parity.  
4. **`ModelLibrary` port**—`test_routing_registry.py` unchanged behavior.  
5. **`ProviderWire` + OpenRouter plugin**—`test_local_router.py` parity; thinner handler.  
6. **Bridge envelope schema**—golden round-trip; one Swift call site migrated.  
7. **MCP as `tool.mcp` plugin**—`test_model_deck_mcp.py` green.  
8. **Contribution-kind lint**—new kinds require schema + policy row.

Proposed checks only; **no tests run** in this planning phase (BRIEF).

## Dependencies and open questions

Other reports: provider wire details, Swift UI composition, MCP/security boundary, schema codegen. Open: dual composition roots (Swift UI plugins vs Python wire plugins); extension signing; whether ledger sinks are plugin-extensible; mechanical DADS-style enforcement in Swift/Python; hot reload for dev.

**Boundary:** Kernel = who may register what, at which version, under which policy. Engine = what Model Deck does with models, Codex, and usage. Plugins = how specific surfaces behave, including unnamed future features via contribution kinds.

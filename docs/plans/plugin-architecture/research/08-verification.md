> Non-normative research input. Read [the canonical plan](../PLAN.md), [boundary scope](../BOUNDARIES.md), and [review corrections](../REVIEW.md) before implementing. Recommendations in this report may have been rejected or superseded.

# Verification and enforceable architecture (Model Deck plugins)

Date: 2026-09-12. Planning only; no tests, builds, or provider calls were executed (BRIEF).

## Recommendation

Use a **verification ladder**: file-scoped worker checks (when a wrapper exists), full Python `unittest` discovery, Swift headless self-tests, proposed mechanical boundary scripts (DADS-inspired, not DADS TypeScript tooling), package artifact gates, and **user qualification** for live host/Codex—outside CI and outside planning-time runs.

Do not claim DADS enforcement until Model Deck owns check scripts with actionable diagnostics. Map each port to existing tests or a proposed conformance table.

## Current test map (grounded)

Python tests are root-level `test_*.py` (`unittest`), with **no in-tree CI workflow** at `6292193`. [VERIFICATION.md](/Users/brytoncooper/Documents/Model Deck/VERIFICATION.md) records **246 passing** tests (2026-09-12 compaction/Fast work); this inventory is structural, not a rerun.

| Concern | File | ~Methods | Migration role |
|---------|------|----------|----------------|
| Router, catalog, compaction, wires | `test_local_router.py` | 45 | Parity for `SessionEngine` + `ProviderWire` ([05-providers.md](05-providers.md)) |
| Bridge / spawn / argv | `test_provider_bridge.py` | 7 | `CodexHostBridge` |
| Cursor pause/resume/interrupt | `test_cursor_agent.py`, `test_cursor_sdk_runtime.py` | 37 | Cursor wire + SDK adapter |
| Settings, endpoints, agents | `test_codex_settings.py`, `test_endpoints.py` | 43 | Registration ports |
| MCP surface | `test_model_deck_mcp.py` | 18 | Future `tool.mcp` plugin |
| Registry, pricing, usage, benchmarks | other `test_*.py` | ~96 | Data sources / ledger |

**Swift headless:** `--self-test-keychain` (`OpenRouterSettings.swift` ~288), `--self-test-companion` (~3304), `--self-test-model-browser` (~3450), `--self-test-usage` (~3480 → `UsageDashboardView.selfTest()` ~848). `build.sh` links `UsageDashboard.swift` + main (~67–69). `--render-preview` is manual visual QA only.

**Documented install gates:** `zsh build.sh` (proposed), `codesign --verify --deep --strict`, stable credential-helper SHA-256, no bundle `__pycache__`, resource list parity with `build.sh` (~40–58).

## Python / Swift boundary limits

DADS file-suffix and `npm run verify` tooling in `dads-framework` is **TypeScript-only** and does not apply to Swift/Python/JSON IPC ([02-api.md](02-api.md)).

**Enforceable (proposed):** Python layer import allowlists (`scripts/checks/check-python-layers.mjs`); versioned JSON Schema for engine IPC in `schemas/`; static contribution-kind table (unknown kind → refuse, [01-kernel.md](01-kernel.md)); SwiftPM target rules after [04-swift.md](04-swift.md) slice 1 (Features → `EngineClient` only, not Python paths).

**Not worker-automated:** Keychain, Accessibility, live ChatGPT SSE, Cursor Node bridge quirks, cross-machine signing, any step requiring the **protected running app** (BRIEF).

**Rejected:** running DADS `dad doctor` here; ESLint/Vitest directory scans as worker substitutes. **Worker Editing-check today: NONE** (BRIEF).

## Port conformance and adapters

| Port | Existing proof | Proposed harness |
|------|----------------|------------------|
| `ModelLibrary` | `test_routing_registry.py` | Unchanged after extraction |
| `ProviderWire` (openai, openrouter, endpoint, cursor) | `test_local_router.py`, `test_cursor_agent.py` | `tests/conformance/provider_wires.json` maps case id → test method |
| `CodexHostBridge` | `test_provider_bridge.py` | Golden `codex_arguments` JSON |
| Compaction | `test_local_router.py`, `context_compaction.py` | Fixture file; document unsupported Cursor+remote compact |
| MCP | `test_model_deck_mcp.py` | Stdio + manifest handshake |
| Swift UI bridge | inline in `OpenRouterSettings.swift` ~2011+ | SwiftPM golden envelopes (proposed) |

**Rule:** in-memory fakes pass the same table as production adapters; host-only cases listed under `unsupported` in the manifest (DADS conformance intent, [goals.md]DADS goals: conformance suites for in-memory vs production).

## External plugin fixture (proposed)

- **`fixtures/plugins/echo-tool/`** — minimal manifest, one echo operation, no network/Keychain; kernel load + invoke in CI.
- **`fixtures/plugins/bad-manifest/`** — API/version mismatch → stable refuse at activate.
- **`fixtures/plugins/arch-violation/`** — imports engine internals or unregistered kind → load refuse once kind lint exists.

Author acceptance ([03-plugins.md](03-plugins.md)): `mdk plugin test` + stub host in &lt;15 minutes, no credentials.

## Bad architecture rejection (proposed)

Grep/script gates aligned with BRIEF anti-patterns: no universal fat `Plugin` interface; no service locator outside composition root; Swift features must not spawn `local_router` directly; single writer for model inventory; MCP copy must preserve Codex approval boundary (`model_deck_mcp.py` ~30); no untyped global event bus. Include `fixtures/arch-violations/` sample that must fail checks.

## Package artifacts

Finalizer checklist (from [VERIFICATION.md](/Users/brytoncooper/Documents/Model Deck/VERIFICATION.md) history): built `Model Deck.app`; strict deep codesign; helper source hash match; all `build.sh` Python resources present; `CodexProviderBridge` executable; no `__pycache__`; scoped updates may compare stripped hashes of touched modules only—full install still needs full build.

## Protected app and qualification

**Planning (BRIEF):** no rebuild, install, kill, or reconfigure of running Model Deck/ChatGPT/Codex during this phase.

**CI-safe (proposed):** unittest discovery, Swift self-tests, schema lint, import checker, stub-host plugin fixture.

**User qualification (Gqual, not merge blockers unless scheduled QA):** relaunch Codex via Model Deck; companion attachment; live routed inference; real usage poll; host resize—document in release notes ([VERIFICATION.md](/Users/brytoncooper/Documents/Model Deck/VERIFICATION.md) 1.7–2.1).

## Proposed command ladder

| Tier | Owner | Proposed command |
|------|--------|------------------|
| Worker | ≤3 exact files | `scripts/checks/editing-check-wrapper.sh --files … -- python3 -m unittest …` (30s cap; **not in repo yet**) |
| Finalizer | One owner | `python3 -m unittest discover -p 'test_*.py'` |
| Finalizer | One owner | `ModelDeck --self-test-companion` + `--self-test-usage` + `--self-test-model-browser` (after build) |
| Finalizer | Release | `zsh build.sh` + `codesign --verify --deep --strict "../Model Deck.app"` |
| Finalizer | Plugins | `mdk plugin test fixtures/plugins/echo-tool` (after CLI) |
| Finalizer | Architecture | `node scripts/checks/check-python-layers.mjs` + `check-architecture.mjs` (after land) |

## Atomic slices (4–8)

1. **`verify:python` script** — `scripts/run-python-tests.sh` wrapping unittest discover. *Acceptance:* finalizer one-liner; behavior unchanged.
2. **Wire conformance manifest** — JSON index of existing router/cursor tests. *Acceptance:* reviewed mapping; tests still pass (proposed).
3. **Python layer checker** — allowlisted imports kernel/engine/plugins. *Acceptance:* pass on tree; fail on violation fixture.
4. **Echo plugin fixture** — load/activate/echo/deactivate without network. *Acceptance:* bad-manifest fails closed.
5. **Schema goldens** — validate engine IPC envelopes ([02-api.md](02-api.md)). *Acceptance:* invalid JSON rejected with code.
6. **Swift EngineClient tests** — SwiftPM golden JSON ([04-swift.md](04-swift.md)). *Acceptance:* self-tests still green.
7. **Architecture rejection script** — grep rules + sample violations. *Acceptance:* sample fails loudly.
8. **Package verify script** — helper hash, resource list, codesign, pycache scan. *Acceptance:* matches VERIFICATION.md finalizer bullets.

Dependencies: 2↔provider extraction ([05-providers.md](05-providers.md)); 4↔kernel lifecycle ([01-kernel.md](01-kernel.md)); 6↔SwiftPM skeleton ([04-swift.md](04-swift.md)). Slices 1, 3, 7, 8 can start without refactors.

## Unresolved

Single verify orchestrator vs shell-only? `mdk` in app bundle or separate tool? Conformance manifest duplicate tests vs annotate only? External plugin signing before policy ([03-plugins.md](03-plugins.md))? CI workflow vs local finalizer-only?

**Summary:** Workers: **NONE** until wrapper exists. Finalizer: broad Python, self-tests, build/sign, proposed architecture scripts. Protected running app: qualification documented, not executed in planning.

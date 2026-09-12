# Model Deck extensible architecture planning brief

Date: 2026-09-12. Lead owns final decisions and integration. Planning only.

## User objective
Identify a minimal kernel, a focused core engine and its public API; apply DADS/hexagonal architecture to current Swift/AppKit + Python Model Deck; enable external authors to add entirely new functionality, not just providers. Produce a complete implementation plan, migration path, atomic slices and verification strategy. User approved prior direction and explicitly requested a written plan and 8+ Composer 2.5 agents.

## Critical operating constraint
DO NOT rebuild, install, launch, restart, kill, signal, reconfigure or replace the running Model Deck, ChatGPT/Codex, router or Cursor SDK. They provide our tools. Do not execute build.sh/build-icon.sh, provider calls, production test suites, migrations or application source. Do not touch application bundles, Keychain, ~/.codex, Application Support, environment/config settings, or git refs/history/index. Read-only source inspection and writing YOUR ONE assigned research Markdown are allowed. No production implementation.

## Current known facts
Repo /Users/brytoncooper/Documents/Model Deck; main at 6292193, nine local commits over initial checkpoint; backup/pre-atomic-hih5xei2 preserves pre-reconstruction complete source. 50 tracked files/~15,200 lines at prior survey. AppKit UI controller OpenRouterSettings.swift 3,488 lines; UsageDashboard.swift 878; Python local_router.py 1,392. Swift invokes Python JSON subprocess commands. Codex bridge starts app-server with loopback routing overrides. Registry is based on ~/.codex/agents TOML. HTTP router mixes transport, orchestration, provider branches, storage/accounting. Cursor execution depends on router event conversion. MCP contains overlapping management behavior. Existing tests were verified previously; do not rerun during planning.

DADS repo /Users/brytoncooper/Documents/CooperTechnology/command/local-repos/dads-framework. Read docs/principles.md as needed: independent evolution; application vocabulary; foreign types at edges; consumer-owned narrow ports; composition-only concrete implementation selection; conformance suites; mechanically enforced boundaries. Current file suffix tooling is TypeScript-specific. DADS branch has user work; never edit it.

## Working direction (challenge with reasons)
Retain Python engine and native AppKit UI initially. Small kernel: plugin identity/discovery/lifecycle, capability authorization, operation/event registration and protocol compatibility; core features implement application use cases through public ports. Separate provider execution, host integration, data sources, storage/credential adapters and feature/UI contributions. First-party extensions obey same contracts as external ones. Public application API is distinct from private plugin control protocol. Shared versioned schemas cross Swift/Python/process boundaries. Model Deck owns model library; Codex files become a compatibility projection. No giant universal Plugin interface, service locator, unrestricted event bus, lowest-common-denominator provider behavior or accidental dual authoritative stores.

## Required research output
Write only assigned report, about 700–1400 words. Ground current facts with relative source paths and line numbers. State firm recommendations, alternatives rejected, exact ownership/contracts, failure and edge cases, migration implications, 4–8 atomic implementation tasks with acceptance checks, dependencies and unresolved questions. Label proposed commands/tooling as proposed. Do not claim tests ran or requirements are implemented. Prefer specific decisions over generic patterns. Research budget approximately 8 minutes; if blocked, write partial findings and stop. User wants thoroughness, not unlimited abstraction.

## Collaboration
You are not alone. Other agents own other reports; do not edit theirs. No worker Editing-check wrapper is available, so worker test commands: NONE. Allowed inspection: rg, sed/cat, git read-only status/show/log. Web only for necessary official primary docs. Output paths under docs/plans/plugin-architecture/research are safe. Finalizer will verify plan consistency, coverage and file links; no builds/tests/provider calls. Scope expansion means stop and report to lead. Delivery: planning artifacts only, uncommitted and unpushed.

## Latest scope correction
Provider/agent-host/platform independence is required. Do not design or implement another named host or operating-system app. Use current implementations plus small synthetic adapters to prove replaceability. Additional target-specific research is discarded from the canonical plan.

# Parallel implementation operating plan

Status: planning only. The user will explicitly start implementation after reviewing the completed plan. [BACKLOG.md](BACKLOG.md) defines outcomes; [work-packages.json](work-packages.json) is the machine-readable dependency graph. This document defines safe fan-out within that graph.

## 1. Parallelize independent evolution

IEP means work that changes for different reasons has a separate owner, interface and proof. Concurrency follows those boundaries. The engine API, provider port, schema vocabulary and authority/migration rules must have one agreed version before dependent workers begin. Inventing different APIs in parallel and merging them later is not an acceleration strategy.

Use up to six concurrent code writers, as required by the repository's agent rules. A larger active team can include four to six read-only scouts/reviewers plus one independent finalizer and the lead. With a runtime ceiling of 17 total agents, a practical full roster is 1 lead + 6 writers + 5 scouts + 1 finalizer = 13; retain room for repair/context work. Actual runtime capacity must be checked at launch. Six writers is a cap, not a reason to create fake independence.

Implementation workers: Composer 2.5, as requested. Fast tier is desired but must be selected using a real service-tier control and recorded from evidence; when unavailable, disclose it rather than changing the installed tool-providing app. Lead owns uncertain architectural decisions. Finalizer model is chosen for review quality, not forced to match workers.

## 2. Serial spine kept deliberately small

The serial spine is **B00 protected development → B01 contract freeze → B02 one public-API vertical path**. Within it, scouts can prepare source maps, conformance cases, UI extraction inventories, import rules and migration fixtures as proposals. They cannot land competing contract implementations.

B01 freezes these specific shared artifacts:

- Engine/extension envelopes, versioning, errors and operation/event descriptor schemas.
- ProviderExecution input/event/cancel contracts and optional capabilities.
- Repository, credential, local transport, process ownership and platform ports required by actual consumers.
- Session/tool/continuation identity semantics and per-operation authorization model.
- Declarative panel components and operation-binding schema.
- Test fixture format, package public surfaces and ownership map.

Each contract has examples, invalid cases, version and review evidence. A freeze is a reviewed commit with working fixtures, not a chat message saying names are settled. Post-freeze changes go to lead/contract owner; dependent packages pause only where affected.

## 3. Ownership lanes

Proposed paths become exact ownership packets once baseline extraction maps are approved. Existing monolithic files each have a single extraction owner. Read-only inspection is shared; edits are not.

| Lane | Exclusive implementation ownership | Independent work after freeze |
|---|---|---|
| Contracts/infrastructure | `contracts/`, architecture rules, build/verify entrypoints | Schema maintenance, negative fixtures, packaging mechanics; initially B00/B01/B03 |
| Application state | `python/src/model_deck/engine/model_library/`, `connections/`, corresponding SQLite adapter and tests | B07/B08; durable IDs, revisions, import preview |
| Host delivery | `python/src/model_deck/integrations/hosts/codex/`, MCP adapter, host fixtures | B06/B09/B10; projection and protocol parity |
| Runs/providers | `engine/sessions/`, `engine/routing/`, then separately assigned provider packages | B12; after interface lands split HTTP B13 and Cursor B14 into separate owners |
| macOS client | `macos/` divided into exact targets after B04 | B04/B05/B21/B25; typed client, presenters, AppKit views and platform attachment |
| Extension runtime | `kernel/`, extension process/storage/jobs/install modules with explicit sub-ownership | B17–B20; provider work need not finish before generic extension operations |
| Evidence/data sources | `engine/usage/`, `integrations/evidence/`, corresponding tests | B16 independent of provider-specific wire changes once event/usage schemas freeze |
| External authors | `sdk/python/`, `examples/session-notebook/`, other-language fixture | B22/B23; cannot edit core to make the example work |

These eight lanes are a pool, not eight simultaneous writers. Scheduler admits the six currently ready, nonoverlapping assignments with greatest downstream value. One worker may own a tightly coupled slice across several files; do not split just to occupy more agents.

## 4. Concrete ready queues

### Queue A: after B02

Run B03 (import enforcement) and B04 (Swift modules) independently. Parent package readiness uses depends_on plus aggregate_requires in work-packages.json; leaf readiness uses its own dependencies. B06 (MCP adapter) starts only after B03 is accepted. State/migration and run-contract scouts prepare exact B07/B12 packets. Once B03 is accepted, B07 and B12 preparation can progress; B12 code waits for B07's repository contract implementation required by its tests. B05 starts after B04 and can proceed alongside state/host work.

B04 may be internally parallelized after a single owner lands module wiring: value types/state tests; window geometry/recovery; typed client; presentation components. Only the shell integration owner edits the legacy `OpenRouterSettings.swift`, package manifest and root app entrypoint. Other workers edit new owned targets against frozen exported interfaces. Integrate compilable modules before switching call sites, not a pile of duplicate declarations.

### Queue B: after B07

Ready candidates: B08 (import preview), B12 (fixture run engine), B16 cached-query/source preparation only after B12 is accepted (full refresh-job acceptance also waits for B19), remaining macOS B05, and independent tooling work. B09 follows B08; B10 follows B09. The projection owner and engine DB owner share an outbox contract, not writable files. No worker makes live home-directory changes.

### Queue C: after B10 and B12

Run B13 (HTTP provider), B14 (Cursor provider), B16 cached-query preparation, B17 (kernel), remaining UI tasks and adapter-conformance fixture authoring concurrently, up to six writers. Provider packages own their tests and provider-specific translation only; the session-engine owner owns shared event/lifecycle code and the B10 host owner owns shared Codex conversion. Stop/request a shared change rather than editing another owner's helper. B15 begins when both provider ports are accepted. Kernel work does not wait on network vendor qualification.

### Queue D: extension runtime and UI

B18 follows B17. After B18 contract/transport acceptance, split B19's independent broker implementations (owned storage, job state, event routing) into separate file owners using the frozen grant principal and operation schemas. The parent B19 completes only when the finalizer checks them together. B20 then owns package activation/update pointers. B21 joins the accepted extension APIs to generic AppKit rendering.

While these land, B25's independent native pages/platform attachment can migrate only after their leaf prerequisites in work-packages.json; final B25 acceptance waits for both its listed prerequisites and all leaf results. Workers must not publish a new engine operation merely to unblock UI without contract-owner review.

### Queue E: external proof and finish

B22 (Notebook) uses only the accepted SDK/protocol interface; SDK scaffolding may be prepared alongside it, but B23's public documentation/package acceptance waits for B22 to prove the workflow. Another-language fixture and documentation authoring can run in parallel in distinct paths. B24 is optional restricted-mode feasibility, independent once B20 exists; it cannot silently broaden default trusted-mode guarantees.

B26 is the final integration owner with the single finalizer running applicable aggregate gates. B27 live cutover is not part of automatic fan-out or an implicit consequence of finishing code.

## 5. Leaf task packet

Every delegated task must contain this completed packet. Store it in the isolated implementation workspace, not in the active application's resources.

```text
Task ID / parent backlog slice:
Objective and observable outcome:
Model: Composer 2.5; Fast requested/observed/unavailable:
Base commit and exact contract versions:
Owned source paths and owned test/fixture paths:
Documentation delta and exact owned subsystem guide paths:
Existing shared files that must remain untouched:
Fixed invariants (billing, approval, state authority, protected runtime):
Public ports consumed and produced:
Allowed Editing wrapper command (or NONE):
Acceptance tests/gate the finalizer will run:
Delivery level: locally implemented, no installation/push unless separately requested:
Commit boundary and rollback:
Scope expansion stop condition:
```

Workers are not alone and never revert another worker's changes. A worker can discover files and choose implementation inside its subsystem, but the integration packet must settle public contracts first. Before using an isolated worktree, check whether the worker tools actually honor its cwd/path constraints; shared-filesystem branches are not isolation by themselves.

## 6. Integration and verification throughput

Use isolated branches/worktrees when shared legacy files or evolving build manifests create collision risk. Otherwise exclusive package ownership is sufficient. Each checkpoint includes its resource/build wiring so the next worker starts from a valid artifact graph. Lead integrates in topological order and inspects the resulting diff.

Workers author meaningful tests and may obtain bounded feedback only through the approved wrapper. They do not run project typechecks/builds or widen checks. The finalizer checks the narrow affected contract after an integration checkpoint; the aggregate gate runs after all implementation writers finish. No redundant full-suite reruns after every tiny leaf commit. A genuine failure invalidates only relevant evidence; identify that scope before rerunning.

A review agent may inspect an in-progress package, but there is only one owner of acceptance status. Read-only scouts can continuously audit forbidden imports, stale documentation, lost behavior mappings and API compatibility without contending for source files.

If two repair attempts fail on one issue, stop the worker/finalizer ping-pong: one capable owner traces the real path and proposes the repair; the same independent finalizer retains the verdict. A missing external SDK/platform/device remains an explicit evidence gap, never a reason to start unbounded retries.

A documentation integrator may write root README/catalog/cross-cutting navigation concurrently in exclusively owned paths; implementation workers maintain their own subsystem guides. Documentation reviewers are read-only. The same six-writer cap includes documentation writers, while read-only editorial review can use other slots.

## 7. Progress ledger

Track each backlog ID as `blocked | ready | implementing | review | accepted`, plus dependency commit IDs, exact files/owner, contract version, narrow check result, aggregate evidence validity and unresolved risks. Acceptance belongs to a verified commit/artifact, not an agent's confident summary.

Prefer small commits with outcomes such as "model library reads through repository port", "Cursor execution no longer imports router", "external notes panel renders from descriptor", and "host projection retries preserve user edits". Commit count is an outcome of independent changes; dozens are plausible but there is no quota.

The handoff after this planning task is: user reviews the plan, then explicitly starts implementation. No implementation agent should interpret this document itself as permission to touch the live application or start building.

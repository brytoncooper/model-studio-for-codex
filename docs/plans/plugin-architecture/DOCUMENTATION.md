# Human-readable system documentation and repository entrypoint

Status: required implementation deliverable, added at the user's request. This file specifies future documentation; the current root product README is unchanged during planning.

## 1. Every system and subsystem has an owner and a Markdown guide

Every designated system/subsystem/package introduced by this architecture has a colocated `README.md` or, when source packaging conventions prohibit it, one clearly linked Markdown document in the corresponding documentation subtree. This is a responsibility rule, not a demand for prose in every utility/test/assets folder. Declare the subsystem set in the architecture ownership inventory and enforce coverage; do not let a meaningful subsystem escape by calling it a helper.

Examples: kernel registration and lifecycle; model library; connections; sessions; routing; usage; plugin installation, data, jobs and event brokers; each provider and host integration; each platform adapter family; Swift client, presentation, renderer and attachment; schemas, SDK and example extension. Nested responsibilities that can evolve independently get their own guide and parent link. No empty template files count as documentation.

Each guide answers, in clear connected prose:

1. **What this system is and why it exists.** One short orientation with its users/consumers and ownership boundary.
2. **What it owns and does not own.** State/data/process authority and adjacent systems; no ambiguous shared owner.
3. **Invariants.** Concrete rules and failure semantics with links to the code/contracts/tests that enforce them.
4. **Contracts.** Public entrypoints/ports, inputs/outputs, events/errors, versioning and dependency direction. Link the authoritative schema/API instead of copying it into a second specification.
5. **How it works.** A small example or sequence/diagram when needed, including startup/shutdown, persistence or asynchronous transitions where relevant.
6. **How to extend it.** A realistic change recipe: where implementation belongs, which contract to implement, how to register it, what callers may assume, and which checks demonstrate success.
7. **How to verify and troubleshoot it.** Actual runnable local commands once implemented; common failures and recovery, with protected-runtime caveats where relevant.
8. **Known limits and compatibility.** Current implemented behavior, optional capabilities and unverified boundaries. Planned work is visibly separate from supported behavior.

File names, diagrams and examples use ordinary language. Avoid a wall of abstract design vocabulary. A new contributor should understand the change location before reading implementation details. Docs should explain the why and non-obvious invariants rather than paraphrase each line of code.

## 2. Root README: why Model Deck, then where to go

The rewritten root README must be a welcoming, accurate front door for users and contributors. Suggested narrative:

- **Why Model Deck:** bring model connections, execution integrations and extensible capabilities behind one locally owned interface; let features evolve without rebuilding the whole application architecture.
- **What it does today:** only verified implemented capabilities, with provider/host/platform limitations plainly stated. Architectural replaceability is not a claim of an unimplemented port.
- **A short getting-started path:** prerequisites, supported installation/development flow, first useful result, and separate billing/credential context where relevant.
- **Architecture at a glance:** kernel, engine, adapters and UI explained simply, with one diagram and a link to the system catalog.
- **Choose your path:** use the app; understand a system; write a plugin; add a provider; adapt a host/platform; contribute/test.
- **Project status and compatibility:** source/package/install/live evidence boundaries and known limitations.
- **Development and contribution links:** local verification, supported runtime protection, architecture rules, license/contribution details that actually exist.

Do not put every implementation detail on the front page. Do not describe proposed features as shipped to make the README more impressive. As each slice lands, add working links and update capabilities incrementally; B26 owns the final editorial/integrity pass.

## 3. Catalog and learning paths

Proposed catalog: `docs/README.md`, linked prominently from root README. Organize by reader task and responsibility, not just alphabetical files.

Suggested catalog columns: system/subsystem, one-sentence purpose, source owner/public surface, human guide, extension recipe. Link every designated subsystem, parent/child relationship and cross-cutting guide. A lightweight inventory/link checker may generate the index rows, but the descriptions and guides remain human-authored and reviewed.

Required cross-cutting guides, created with their implementing slices:

| Guide | Content | Owning slices |
|---|---|---|
| Architecture and IEP | Ownership, import rules, ports, concrete examples of independent change | B01/B03, consolidated B26 |
| Engine API | Connection lifecycle, authentication, operations/events, errors, cancellation and compatibility | B01/B02/B12 |
| First external plugin | Scaffold, implement, validate, run in isolated state, package, install and update | B18–B23 |
| External feature recipe | Notebook walkthrough covering operations, panels, data, jobs, grants and export | B19/B21/B22 |
| Provider extension | Provider port, capabilities/unknowns, credentials, streaming/tools, conformance and package binding | B13/B14/B15/B18 |
| Host and platform seams | Existing adapters and fixture substitution; how future authors identify the needed contract | B03/B10; no named future-port design |
| Data and migration | Authority, projection outbox, conflicts, snapshot/cutover/rollback and plugin updates | B07–B11/B20 |
| Development and verification | Exact supported commands, bounded worker checks, independent finalization, artifacts and protected running instance | B00/B03/B26 |

Future paths can be selected during implementation, but the catalog must not contain dead placeholder links. A declared unavailable guide shows status as text until implemented.

## 4. Definition of done for every backlog slice

Every B00–B27 packet includes a **Documentation delta**: new/updated subsystem guide(s), invariants/contracts changed, catalog/parent links, and user/developer-facing example affected. A slice that creates a system is incomplete without its guide. A slice changing an invariant updates the guide in the same atomic commit. An implementation-neutral task records why documentation is unchanged rather than creating filler.

Source owner owns its colocated Markdown. A separate documentation integrator owns only the root README, catalog and cross-cutting navigation, consuming approved subsystem summaries. This allows parallel code/doc work without two writers editing the same subsystem guide. Read-only reviewers can critique clarity and correctness concurrently.

Contracts remain authoritative in code/schemas; prose links to them and explains their implications. The plan/spec archive is not the product documentation. Do not make a new contributor reconstruct current behavior from old planning discussions.

## 5. Verification and editorial acceptance

Add documentation coverage/link checks to G1/G7: every designated subsystem has a nonempty guide, catalog rows resolve, relative source/contract links exist, exported public surfaces have an extension/usage explanation, and no guide claims a planned command already works. Codegen drift checks ensure referenced schema names match actual contracts.

A human/agent review reads at least one guide from each system family and the full root README/catalog. It must answer: can a newcomer explain the purpose, name the invariants, find the public contract, add a bounded extension, and run the relevant check without tribal knowledge? Broken links, contradictory authority rules or missing extension instructions block completion; cosmetic wording does not require a new implementation cycle.

The external-plugin acceptance runs the quickstart from a fresh temporary project using only published/staged SDK/docs. Success from a private developer checkout alone is insufficient. Preserve the current app and its state throughout documentation examples and local development instructions.

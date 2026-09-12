# Model Deck: kernel, engine, and external feature architecture

Status: proposed implementation design; independent review and corrections are tracked in REVIEW.md. No runtime implementation is claimed.
Date: 2026-09-12. Source baseline: `6292193`; recovery branch: `backup/pre-atomic-hih5xei2`.

## Read this first

The deliverable is a maintainable engine that can serve multiple clients and accept independently shipped functionality. A successful external extension adds a new operation, background task, data model, and usable panel without a change to Model Deck's kernel, engine internals, host adapter, or Swift navigation switches.

This plan is the lead's synthesis of ten parallel Composer 2.5 research assignments. Research reports are inputs, not implementation specifications; decisions here and in [API.md](API.md) supersede them. [BACKLOG.md](BACKLOG.md) is the executable sequence; [PARALLEL-EXECUTION.md](PARALLEL-EXECUTION.md) defines high-concurrency ownership and [work-packages.json](work-packages.json) encodes dependencies; [VERIFICATION.md](VERIFICATION.md) defines completion evidence. [BOUNDARIES.md](BOUNDARIES.md) defines provider, host and platform independence using current implementations and fixtures; building additional hosts or operating-system clients is outside scope. [REVIEW.md](REVIEW.md) records independent review and corrections.

**Protect the live toolchain.** Do not rebuild, install over, restart, signal, or reconfigure the running Model Deck, ChatGPT/Codex, router, credential helper, or Cursor SDK. Do not change their resources or active state. Source rollback does not restore in-flight sessions, credentials, or databases. During this planning task only these documentation files may change. During implementation, isolated source, state roots and output directories remain mandatory. Replacing the active installation is a later, explicitly scheduled cutover.

## 1. Goals and observable success

Audience: the owner maintaining Model Deck; external developers adding functionality; users consuming models and extensions; other applications using the engine.

Required outcomes:

1. Engine functions without AppKit or a running Codex application; CLI and current macOS UI share application operations. Fixture provider/host/platform substitutions prove independence without designing another product integration or OS client.
2. Model Deck owns connections, registered models, extension activation, grants, and run metadata. Host artifacts are projections.
3. Kernel has no imports of provider SDKs, Codex schemas, AppKit, pricing, or feature-specific logic.
4. Providers, host integrations, usage/catalog/benchmark sources, storage adapters, and optional features have distinct narrow contracts.
5. A separately packaged **Session Notebook** extension registers operations, a panel, scoped data and an export job without editing the app. A second non-Python fixture proves the protocol is language-neutral.
6. Existing registered routes, billing distinctions, terminal event semantics, tool approval ownership, continuation isolation, compaction, usage display, and companion behavior survive migration.
7. Architecture violations fail a fast automated gate. Fakes and production adapter implementations run against shared conformance tests; real provider qualification remains separate evidence.
8. Every designated system/subsystem has a human-readable guide describing purpose, ownership, invariants, contracts and extension steps; root README explains why Model Deck and links to a complete system/extension catalog. [DOCUMENTATION.md](DOCUMENTATION.md) defines this delivery requirement.
9. Every implementation checkpoint is buildable in isolation and has a meaningful behavior or ownership result. No arbitrary target commit count.

Not required for the first public extension release: a marketplace, cloud multi-tenancy, arbitrary third-party AppKit binary loading, SwiftUI conversion, universal compatibility with every host, or guaranteed replay of completed model generation. These may be separate features, not hidden dependencies of the core.

## 2. Current architecture and reasons to change

Current Swift screens, saved account types, Keychain subprocess calls, network calls, Python subprocess execution and window lifecycle share [OpenRouterSettings.swift](../../../OpenRouterSettings.swift). [UsageDashboard.swift](../../../UsageDashboard.swift) is separate but still interprets provider-specific dictionaries. [build.sh](../../../build.sh) compiles Swift directly and explicitly copies Python resources; its default output is a sibling app bundle, unsuitable for protected development.

[provider_bridge.py](../../../provider_bridge.py) wraps Codex app-server and starts [local_router.py](../../../local_router.py). The router combines HTTP transport, provider choice, wire translation, credentials, continuation, compaction and accounting. [routing_registry.py](../../../routing_registry.py) reads managed Codex TOML as authority. [model_deck_mcp.py](../../../model_deck_mcp.py) repeats application orchestration. [cursor_agent.py](../../../cursor_agent.py) imports a router translation helper at runtime, illustrating a dependency that directory renaming alone will not fix.

DADS supplies the discipline: independent evolution, application-owned language, foreign types quarantined at boundaries, consumer-owned ports, composition-only selection of implementations, and shared conformance suites. Its current TypeScript suffix checker is not Swift/Python enforcement. Sources: [DADS principles](../../../../CooperTechnology/command/local-repos/dads-framework/docs/principles.md), [house rules](../../../../CooperTechnology/command/local-repos/dads-framework/docs/house-rules.md). Resolve these sibling-workspace references locally; DADS is not a runtime dependency.

## 3. Decisions

### D1. Retain Python engine and AppKit presentation

Reorganize the existing Python behavior into a distributable package. Use Swift Package Manager targets for the macOS client. Do not duplicate routing policy in Swift. Python typed ports and Swift protocols express local contracts; versioned JSON schemas express cross-process contracts. `Foundation` values are allowed in client code; AppKit and Accessibility remain in macOS targets.

### D2. Kernel is coordination, not the product

Kernel owns extension identity, compatible activation, registered operation/event descriptors, lifecycle, scoped API dispatch and capability grants. It depends on abstract process, clock, store and transport ports supplied by bootstrap. Implementations of spawning processes, reading files, Keychain and logging are adapters.

Engine capabilities own product behavior: model library, connections, run/session lifecycle, routing policy, usage queries, host registration plans and feature installation workflows. Optional capabilities can be omitted from a product composition. The default composition includes the current useful product.

Plugin implementations sit outside these boundaries. A plugin may contribute a known provider port or a completely new namespaced operation, event, job and panel. Novel feature operations use the generic typed registration mechanism; they do not require a new hard-coded kernel kind. Specialized new port families are introduced only when another independently owned consumer needs them.

Only bootstrap imports concrete implementations and binds dependencies. A scoped extension context exposes requested public operations, owned storage and declared event subscriptions; it is not an object graph or service locator. No unrestricted global event bus and no catch-all `utils` or `services` package.

### D3. Separate public engine API from plugin lifecycle protocol

`engine.v1` serves clients. `plugin.v1` supervises extension workers and routes registered calls. They share envelope primitives but have different principals, allowed methods and lifecycles. Codex app-server JSON-RPC, Responses/SSE and MCP remain explicit delivery adapters. The public engine is not the Codex wire protocol.

Use a LocalTransport port with the current macOS Unix-domain socket adapter, plus stdio for supervised extensions. CLI and UI clients connect through typed transport adapters. Rendezvous records identify the transport rather than assuming a socket path. OS security adapters enforce private endpoint access. A `serve --stdio` mode hosts a foreground isolated engine for tests/embedding with a distinct state root. No public network listener is necessary for v1. A later authenticated HTTP transport can implement the same contracts without becoming the core API.

### D4. Explicit engine ownership and lifetime

One engine owns one state root using an exclusive instance-lock port and protocol-version handshake. Paths, file locking/replacement, child-process supervision, credential access and IPC are platform adapters; no POSIX/macOS API belongs in core. Bootstrap can start an absent engine from an immutable installed/staged runtime; clients discover it through a private rendezvous record. Closing a UI connection does not terminate runs or the engine. An incompatible engine is reported, not automatically replaced. Private plugin worker pipes are not client rendezvous endpoints.

Engine shutdown drains or explicitly cancels owned work; the protected existing router is never managed by this mechanism. Tests use temporary state/socket directories and fixture hosts. The first cutover is opt-in after an offline migration; legacy and new runtimes never both write the same state.

### D5. App-owned data with explicit projections

Use an engine-owned SQLite database for new model/connection/activation/grant/run metadata, transaction log and projection outbox. Treat existing JSON/TOML as import sources or compatibility outputs. Keep existing continuation SQLite semantics behind its port until separately migrated. Keep credentials behind a CredentialStore port implemented by the existing macOS Keychain helper, referenced by opaque account IDs. Large content and plugin artifacts are separate from operational metadata.

A transaction commits desired model state and pending host projection together. Filesystem writes happen through a reconciler with recorded expected hashes. Report desired versus applied host revision. Never describe a multi-file filesystem sequence as atomic. Failure leaves a recoverable pending projection, not contradictory authoritative stores.

### D6. Preserve provider differences

Provider ID, account/connection, model ID, wire format, execution mode and billing source are separate fields. `unknown` is a first-class capability value. Cursor is an SDK harness with tool suspension semantics; generic HTTP generation is a different execution mode. Host-native ChatGPT subscription passthrough requires a valid host-bound execution context; it is not exposed as an arbitrary reusable subscription API.

The initial extraction may wrap legacy Responses objects inside an explicitly private compatibility adapter. Public v1 run requests/events must use application-owned types before third-party SDK publication. Provider-private continuation stays opaque and scoped to provider, connection, model and execution mode. Switching scope does not silently reuse it.

### D7. External code is supervised; permissions are not a sandbox claim

Built-ins and external extensions register the same descriptors and pass the same conformance suite. Trusted built-ins may execute in process behind their ports. External executable plugins use supervised child processes, private stdio channels, bounded messages, activation tokens and no in-process Python imports or native UI code loading.

API grants restrict brokered operations and accidental overreach. An unsandboxed same-user process can still access OS resources outside the broker; do not advertise hostile-code containment. Initial external installation is a user-trusted executable extension model with explicit provenance and requested access. A separately gated macOS sandbox feasibility/implementation slice is required before offering an untrusted-extension safety guarantee; unsupported sandbox profiles fail closed for that mode. Do not make all extensibility wait for an unsupported promise.

### D8. Declarative native UI, independent external features

External plugins contribute command descriptors, settings forms, navigation items and panels built from a versioned component schema (text, fields, actions, lists/tables, status, progress, markdown without active HTML). The shell renders them with AppKit. A panel binds to declared operations and data/events, with revision checks and validation; it cannot invoke arbitrary selectors or load code into the shell.

Native first-party screens may have specialized renderers, but use the same public engine operations. Third-party arbitrary canvas/web content is a later renderer extension with a separate trust and versioning decision. No unbounded UI tree, executable JavaScript or raw file URL capability is implied by `ui.panel`.

### D9. Focus files by responsibility and enforce real boundaries

Use ordinary filenames such as `register_model.py`, `model_repository.py`, `sqlite_model_repository.py`, `ModelLibraryPresenter.swift`, `KeychainCredentialStore.swift`. Keep a capability's pure rules, use cases and ports together. Place external adapters beside the owning integration, not inside a global miscellaneous folder. No mechanical one-class-per-file law or mandatory TypeScript suffix vocabulary.

Dependency rules must be checked, including negative fixtures that deliberately violate them. Swift targets express module dependencies. Python AST/import checks enforce declared allowed packages, with narrowly reviewed exceptions for the loader/composition roots. Import checks do not prove all functions pure; effect boundaries also need injected clocks/stores and behavior tests.

## 4. Proposed source layout

Paths below are proposed, not existing code. Module count follows real dependency needs; do not create empty layers in advance.

```text
contracts/                     # engine.v1, plugin.v1, port schemas and fixtures
python/
  pyproject.toml
  src/model_deck/
    kernel/                    # registration, grants, lifecycle; no product/provider imports
    engine/
      model_library/           # types, use cases, consumer-owned repository port
      connections/
      sessions/
      routing/
      usage/
      extensions/              # install/enable workflows using kernel contracts
    adapters/
      storage/                 # SQLite, legacy imports, ledger/continuation persistence
      credentials/             # macOS helper adapter
      process/                 # supervised child processes
      transport/               # local IPC interface, extension stdio
      platform/macos/          # Unix socket, Keychain, POSIX process/lock implementation
    integrations/
      hosts/codex/             # app-server, discovery, TOML projection, passthrough
      providers/openrouter/
      providers/openai_compatible/
      providers/cursor/
      evidence/                # pricing and benchmark source adapters
      clients/mcp/             # thin public-API client
    bootstrap/                 # only place selecting concrete implementations
  tests/                       # capability tests, conformance and integration fixtures
macos/
  Package.swift
  Sources/
    ModelDeckContracts/        # generated wire DTOs; no routing policy
    ModelDeckClient/           # socket client, cancellation, reconnect
    ModelDeckPresentation/     # screen/panel state and presenters
    ModelDeckAppKit/            # native views, declarative renderer, design components
    ModelDeckPlatform/         # AX, host windows, platform services
    ModelDeckApp/              # composition, menu, lifecycle
  Tests/
sdk/python/                    # lightweight external author SDK
examples/session-notebook/     # independently packaged external feature
examples/protocol-fixture/     # non-Python process implementing plugin.v1
scripts/                       # build/verification, explicit output and state roots
```

Contract packages expose only documented public symbols. Plugin code depends on schemas/SDK, not `model_deck.engine.*`. The Session Notebook acceptance build must succeed when private engine source is absent from its import path.

## 5. Runtime behavior and boundaries

A UI/CLI caller invokes a typed operation. Dispatch checks negotiated version and caller scope. An engine use case applies policy, calls a narrow port, and records state. Provider adapters translate at the edge and return typed events. Host adapters receive tool requests and apply their own execution/approval policy; the engine never executes an arbitrary tool simply because a provider requested it.

CLI fixture sessions support text generation without tools. A real headless tool executor is a separately configured host capability, default-denied until defined. Engine APIs must not silently inherit Codex authority. Provider-native built-in tools stay disabled unless separately supported and explicitly disclosed.

Usage collection is separated into allowance, activity, pricing estimate and settled cost. Preserve source, time and unknown values. Do not convert subscription allowances into invented dollar cost or equate cached catalog pricing with charges. Pricing/benchmarks subscribe through narrow data-source ports; they cannot delay or mutate a request by performing an implicit remote refresh on the critical path.

Compaction is explicit capability negotiation and a run operation using adapter-specific implementation. Engine request content is not a generic telemetry payload. Operational events expose IDs/status/counts by default; content requires separate grants and retention policy. A plugin's own notes are its data, not permission to read host transcripts.

## 6. Swift migration and user experience

Extract value types and existing self-testable window geometry/state first, then a typed engine client and presenters. Preserve generation guards that discard stale catalog responses, usage refresh that retains successful data, keyboard navigation, Accessibility labels, and shared companion/full-window presentation. View updates and presenter state transitions run on the main actor; network/process work does not block it.

Five states are mandatory for each screen and extension panel: loading, ready, empty, recoverable failure, and unavailable/unsupported. Long-running jobs add cancellable progress. Stale usage shows the last successful value with timestamp and a failed-refresh message. A disabled/crashed extension shows status in Extensions and stops receiving new calls; its data remains available for export/re-enable.

Extensions UX: install archive/directory → inspect identity/version/provenance/permissions → enable → visible commands/panels. Permission expansion on update requires renewed consent. Disable retains data; uninstall removes executable artifacts while retaining data by default; deletion of user data is a separate explicit action. Active jobs produce a drain/cancel decision, never disappear as an unexplained success.

## 7. Migration, compatibility and rollback

The source backup branch protects code, not user state. Implement a read-only inventory with checksums, schema versions, routes and classification (managed match, managed drift, foreign, malformed, orphan). Never collect credential values in the inventory. Build a deterministic import preview and validate it into a temporary database. Missing, conflicting or duplicate route identity is an explicit unresolved item.

After a separately authorized quiescent cutover: acquire the state writer lock; verify inventory fingerprints still match; take a consistent snapshot of databases including WAL state through supported SQLite backup; commit imported data and outbox; write host projections using expected hashes; record applied revisions. Unknown/unowned agent files remain untouched. No global Codex config rewrite. Old UI JSON preferences become presentation settings or read-only legacy inputs, never a second connection authority.

Every engine schema upgrade records version, migration ID/checksum and outcome in the database. Acquire the writer lease, reject unsupported newer schemas, snapshot consistently, and apply each supported upgrade transactionally with its bookkeeping. Failed upgrades reopen only the previous compatible schema; multi-step upgrades have explicit recovery checkpoints. This covers future schema changes as well as the one-time legacy import.

Rollback before activation is discard-only for the isolated new state. After activation, preserve the new database and exports before restoring any old snapshot. Do not lose newly created models/notes/grants by blindly reverting the binary. Schema downgrade is a tested export/restore path with explicit conflict handling. Existing credential helper identity and token command compatibility remain unchanged until dedicated qualification.

Side-by-side versions of a plugin use separate executable and data-version directories. Stage/validate executable and schemas first; stop admission, drain or explicitly interrupt owned jobs, freeze old-activation writes and record the final committed data revision. Only then snapshot/migrate a copy and validate a non-serving activation. Switch one transactional executable/data/grant activation record, revoke the old tokens, then admit new work. Failure revokes staged tokens before reopening the old activation. A write racing the freeze is included in the final revision or explicitly rejected, never lost. Post-activation rollback that would discard new data requires export/explicit resolution.

## 8. Reference proofs of extensibility

**Session Notebook:** external archive contributes a notes list/editor, namespaced CRUD operations, plugin-owned records and a cancellable export job. Linking a session requires a metadata capability exposed by the host; when absent, manual note entry still works. The plugin does not assume the current app exposes a selected thread. Full transcript access is neither required nor enabled. A generic CLI can invoke its operations without a built-in `notebook` subcommand.

**Provider fixture:** a separately packaged deterministic provider exercises text deltas, tool suspension/result return, cancellation and terminal failures through the public provider port. It has no SDK/network access. It proves protocol conformance, not any vendor's live compatibility.

**Other-language fixture:** a small JavaScript process with no private Python imports performs activation, registers an operation, receives cancellation and calls only a granted broker operation. The production app does not silently install Node; this fixture is a development requirement with a documented local runtime.

The kernel may still evolve when a truly new privileged primitive is needed. The extensibility promise is independent features using supported capabilities without editing core, not infinite future functionality with a frozen API forever.

## 9. Alternatives and resolved disagreements

- Keep Codex Responses as the public engine language: rejected; allowed only as an internal transitional adapter to preserve behavior during extraction.
- One universal provider/plugin interface: rejected; distinguish provider execution, hosts, data sources and generic feature contributions.
- Third-party `ui.native` factories/in-process AppKit: rejected for v1; use `ui.panel` declarative components. Native built-ins remain app-owned.
- OpenAPI/HTTP as the first API: rejected; JSON-RPC schemas with local transports first. HTTP may be another adapter later.
- Multiple JSON files updated with CAS as an atomic transaction: rejected; SQLite authority plus explicit outbox reconciliation.
- Imported Codex TOML remains live co-authority: rejected after cutover; subsequent external drift is surfaced and imported explicitly.
- Delete plugin data on uninstall: rejected; preserve by default.
- Automatic retries after model/process failure: rejected unless the adapter has an explicit idempotent resume guarantee; avoid duplicate spend/tool effects.
- A plugin manifest is an OS sandbox: rejected; separate trusted extension mode from a proven sandbox profile.
- Additional Swift files alone establish hexagonal architecture: rejected; package/import boundaries and negative tests are required.

## 10. Delivery contract

Implement the dependency-ordered slices in [BACKLOG.md](BACKLOG.md), using Composer 2.5 for bounded implementation tasks as requested. Fast must be requested through a supported service-tier control and verified from runtime evidence; a model name or prompt is not proof of Fast mode. Lead owns contracts, ambiguous architecture, integration and final diff review. One independent finalizer owns aggregate checks. At most six concurrent writers, one writer per module; do not parallelize against draft shared interfaces.

Plan completion means this design, API, backlog and verification strategy have been reviewed and reconciled. Implementation completion requires gates in VERIFICATION.md, not a green Markdown check. Next executable task is B00 (isolated development guard) after implementation begins; live app cutover remains outside ordinary local implementation completion.

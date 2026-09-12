# Dependency-ordered implementation backlog

Status: proposed work, none implemented by this planning task. [PLAN.md](PLAN.md) and [API.md](API.md) are authoritative. Gates/commands are defined in [VERIFICATION.md](VERIFICATION.md). A slice may contain multiple atomic commits; do not combine unrelated steps merely because they share a phase.

## Documentation requirement for every slice

Every B00–B27 slice includes the documentation delta required by [DOCUMENTATION.md](DOCUMENTATION.md): colocated subsystem guide, purpose/invariants/contracts/how-to-extend updates and parent/catalog links in the same commit. Source owners own their guides; a separate integrator owns the root README/catalog. B26 finishes the root README narrative and verifies catalog completeness. No newly introduced subsystem is accepted without its human guide.

## Execution rules

- Lead freezes contracts before dependent implementation, owns integration/final diff review and any scope change.
- Composer 2.5 is the requested implementation worker model. Request Fast only through a supported runtime setting and record whether it was observed. Do not alter the current installed router to enable it.
- One independent finalizer owns acceptance and aggregate checks. Reuse that owner after repairs. Workers get only the approved exact-file, <=3-file, 30-second Editing wrapper after B00 supplies it; before that, no worker test command.
- At most six concurrent writers, one writer per package/file. Leave a concurrency slot for finalization/repair. Each worker receives objective, owned paths, fixed contracts/invariants, check command, acceptance/delivery level, and stop condition. Stop when a shared contract must change.
- Keep temporary state, fixture credentials and build outputs outside all live application/state paths. No installed build command, process restarts or migrations into active directories.
- Snapshot affected dirty files before any deterministic bulk transform; verify independently and delete only the temporary snapshot after success. Retain the user-requested named backup branch.
- Each commit combines the behavior or boundary extraction with its necessary tests and package resources. Pure file moves and semantic changes should be separate when that preserves green checkpoints. Do not conceal failed intermediate commits in a later fix.

## Wave 0 — protected development and a contract freeze

### B00 — Make isolated development a supported operation

Depends: none. Owner: build/verification infrastructure. Paths: proposed `scripts/`, build entrypoint and contributor instructions; no app source behavior change.

Implement explicit artifact/state/socket directories, output-path collision rejection and an isolated source worktree recipe. New development commands refuse active app output/default Application Support. Preserve existing credential-helper bytes and signing behavior. Add an Editing wrapper enforcing exact file ownership, <=3 files, process-tree deadline 30 seconds, bounded output, only selected lint/unit operations, no retries/globs/arbitrary forwarded commands. Baseline inventory records current tests/self-test modes without changing them.

Acceptance G0: negative path tests reject active bundle/state/symlink aliases; wrapper rejects unsupported checks; fixture subprocess children are terminated on timeout without broad signals. Finalizer validates wrapper. Rollback: remove new development tooling; live runtime unchanged.

### B01 — Freeze contract vocabulary and prove cross-language encoding

Depends: B00. Owner: contracts. Paths: `contracts/`, tiny Swift/Python schema fixtures, protocol design notes.

Define the API document's types/methods/errors, event states, capability tri-state, manifest and panel schemas. Define narrow platform ports for paths, locks, IPC, process ownership and credentials; implement only current-platform and fixture adapters, with no alternate OS/client design. Choose schema codegen/validation after a narrow round-trip spike; pin tool versions. Define the complete public, activation-broker and privileged enrollment method inventory in API.md, including provider wire binding, invocation authority, UI data discovery, attachments, storage/jobs and compaction. Define operation catalog and compatibility/unknown-field policy. Include limits, permission vocabulary and response redaction. Architecture consumers review actual fixtures before freeze.

Acceptance G1: Swift/Python consume the same valid/invalid examples; incompatible negotiation rejects; UUID/reference fields never accept secrets as identities; tool call and terminal-event fixtures encode identically. Rollback: version unreleased contract; no consumer can start against an unfinished B01.

### B02 — Prove a headless model-library vertical path

Depends: B01. Owner: engine model library + local transport (single owner until seam lands). Paths: proposed Python package bootstrap, `engine/model_library`, socket adapter, generic CLI; legacy registry read adapter.

Expose `models.list` through `engine.v1` using a read-only legacy repository port. CLI lists a fixture model through a real local protocol connection without importing AppKit or launching Codex. Explicit isolated state roots only. Inject transport, instance-lock and path adapters at bootstrap; core cannot import OS implementations. No migration or extra authority yet.

Acceptance G2: CLI → socket → use case → fixture/legacy read port → typed output; absent engine and incompatible handshake have clear errors; closing CLI does not stop another client's request. Rollback: legacy entrypoints remain the default.

## Wave 1 — enforce boundaries and make existing clients converge

### B03 — Establish architecture enforcement on the real vertical path

Depends: B02. Owner: architecture tooling. Paths: proposed import rules/negative fixtures and Python package public surfaces.

Enforce kernel→contracts/ports only; engine→owned ports/contracts; adapter→public core; plugins→SDK only; concrete selections in bootstrap only. Baseline untouched legacy files explicitly and ratchet down on migration. Reject dynamic imports outside named loader exceptions. Avoid pretending AST rules prove all effects.

Acceptance G1/G2: negative fixtures fail for core importing Cursor/AppKit/storage implementations, plugin importing private engine, cross-feature private import and registry service locator. Existing allowed graph passes. Rollback: retain report-only baseline for untouched files, never disable migrated-module checks.

### B04 — Package Swift without altering the visible app

Depends: B01. Owner: macOS module boundary. Paths: `macos/Package.swift`, value/state/window-geometry extractions, isolated build packaging references.

Move value types and existing self-test logic into appropriate targets. Build the same AppKit app from explicit staged output; original launch behavior retained. Preserve main entrypoint semantics, helper identity, assets and resource packaging. Do not add one target per class.

Acceptance G3: compile/test extracted types; original self-test scenarios remain represented; staged executable/assets inventory equals expected contract. No install or active app execution. Rollback: previous build path in isolated checkout.

### B05 — Connect Models screen to the typed engine client

Depends: B02, B04. Owner: Swift client/model presenter. Paths: `ModelDeckClient`, model presenter/view and DTOs.

Replace one scattered dictionary/subprocess path with `models.list`. Preserve searching, selection, stale response generation guards and keyboard behavior. Client handles handshake/reconnect/cancellation; presentation stays main-actor-bound. This is the second real consumer of B02.

Acceptance G2/G3: fixture engine drives loading/ready/empty/failure/unavailable; stale response cannot replace new connection results; no view starts a Python `Process` directly. Rollback: adapter-backed legacy model service remains selectable only in bootstrap.

### B06 — Converge MCP on application use cases

Depends: B02, B03. Owner: MCP adapter. Paths: `integrations/clients/mcp` and legacy entrypoint wrapper.

Move model list/search/register orchestration behind public operations as each exists; preserve MCP tool names, bounded descriptions and protocol behavior. During migration, unconverted methods delegate to a named legacy application adapter, not direct new storage. Keep MCP independent of Codex-specific injection; injection remains the host adapter's job.

Acceptance G2: existing MCP fixtures preserve results and errors; Swift/CLI/MCP fixture reads agree; no duplicate repository implementation. Rollback: same MCP entrypoint delegates to prior adapter.

## Wave 2 — application-owned state and host compatibility

### B07 — Model connection/registration transactions

Depends: B01, B02, B03. Owner: model library/storage capability. Paths: model/connection use cases, SQLite adapter, transaction/outbox schema.

Implement revisioned register/rename/remove/save and durable idempotency records. Connection identity distinct from model labels and provider URL. Repository fake and SQLite pass identical contracts. Existing production state remains read-only; new writes go to isolated database.

Acceptance G2/G4: concurrent revision conflict; duplicate idempotency payload consistency; no plaintext secret fields; removal blocks new runs but not captured active route snapshots. Rollback: discard fixture DB; no live state migration.

### B08 — Deterministic legacy import preview

Depends: B07. Owner: migration adapter. Paths: legacy readers/import plan and fixtures.

Inventory JSON preferences, managed TOML, display names, endpoint overrides and relevant store versions; classify drift/foreign/invalid data. Produce redacted deterministic import plan with source hashes. Resolve identity collisions explicitly. Preserve all legacy inputs.

Acceptance G4: repeated scan gives same plan; foreign/symlink/malformed files reject or classify without modification; previews never read/export keys. Rollback: delete only preview artifact.

### B09 — Host projection outbox and conflict-aware reconciliation

Depends: B07, B08. Owner: Codex projection adapter. Paths: `integrations/hosts/codex` projection writer and outbox port consumer.

Write managed TOML into fixture directories from committed desired state; expected-hash preconditions prevent overwriting user edits. Record applied revision separately. Export original identity/credential-helper compatibility fields as needed. Explicit import is required for post-cutover external changes.

Acceptance G4: crash at each file/outbox boundary converges or reports pending conflict; foreign files remain untouched; no second authority; repeat apply idempotent. Rollback: restore owned projection only when hash still matches engine output.

### B10 — Codex bridge as a host adapter

Depends: B03, B09. Owner: host bridge. Paths: discovery, app-server mapping, launch preparation, model injection and compatibility profile.

Extract host-specific JSON-RPC/Responses/catalog rules and launch config from core. Preserve per-process overrides, no global config rewrite. Host capability reports distinguish available, already-running-unverified and incompatible. Native subscription route stays tied to host context. Keep current entrypoint wrappers available.

Acceptance G2/G4: fake app-server verifies method/event/projection parity, cancellation ownership and unknown-version refusal; no tests launch real Codex or access real tokens. Rollback: bootstrap selects the legacy bridge in isolated qualification only.

### B11 — Offline migration and rollback rehearsal

Depends: B08, B09, B10. Owner: migration/activation. Paths: migration coordinator/snapshot/export tools.

Validate import to temp DB; require exclusive writer/cutover readiness and unchanged fingerprints; SQLite backup handles WAL; commit data/outbox; preserve sources and snapshot journal. Rehearse rollback with post-import new data, export and conflicts. Add engine schema-version/migration-ID/checksum bookkeeping, transactional upgrades and unsupported-newer-schema refusal, covering future schema changes beyond the one-time import. This task implements tooling, not production cutover.

Acceptance G4: interrupted import never changes authority partially; active-writer refusal; snapshots restore all committed fixture records; no blind downgrade/data loss. Rollback: tested preserved snapshots with compare-before-write.

## Wave 3 — session engine and provider ports

### B12 — Extract run admission/state machine with fixture provider

Depends: B01, B03, B07. Owner: sessions/routing together. Paths: run use cases, run store, provider port, deterministic adapter.

Implement `sessions.*`, `runs.*`, event sequence, terminal exclusivity, durable dispatch admission, idempotency and route snapshots. Deliver CLI fixture text run end-to-end before vendor migration. Event routing is scoped, bounded and supports explicit replay expiry.

Acceptance G2/G5: cancel/completion races, crash-after-admission, reconnect without resubmission, slow reader limits, unknown capabilities, no double provider dispatch. Rollback: new session path fixture-only until B15/B16.

### B13 — Extract HTTP provider adapters and wire translation

Depends: B12, B10. Owner: HTTP provider implementation. Paths: OpenRouter/compatible integrations, chat wire, private Codex compatibility conversion.

Move provider-specific request/stream logic out of RouterHandler while preserving vendor reasoning quirks and Responses→Chat fallback rules. Public events normalize at edge. Handler becomes transport/host adaptation. Maintain terminal outcome and tool-call completeness safeguards.

Acceptance G5: existing HTTP/router fixtures plus common provider conformance; malformed/truncated streams fail; fallback does not duplicate an already-started billed request; native passthrough stays host-bound. Rollback: legacy provider adapter behind same port in isolated tests.

### B14 — Extract Cursor execution adapter

Depends: B12, B10. Owner: Cursor integration. Paths: Cursor SDK runtime/agent, execution-mode capability and private translation helper.

Remove runtime imports back into router. Keep existing SDK isolation, tool callback suspension, account routing, reasoning/Fast model parameters and installation behavior behind dedicated ports. External feature loading never changes SDK installation.

Acceptance G5: fixture callback IDs/results, cancellation/teardown, unsupported Fast, truncated terminal outcomes, parallel sessions and active run reuse match current tests. No live Cursor calls for this gate. Rollback: prior Cursor adapter retained in immutable legacy artifact.

### B15 — Continuation and compaction parity

Depends: B13, B14. Owner: sessions continuation/compaction. Paths: continuation store, compaction port, host/provider conversion.

Preserve endpoint/account/model scope, private provider signatures, cross-provider stripping, encrypted host context handling, both legacy compaction entry paths and SDK harness continuation. Extraction must not treat a summary as portable encrypted provider state.

Acceptance G5: every existing continuation/compaction fixture maps to named new contract; switching provider/account cannot reveal or reuse incompatible state; failed summary never replaces history. Rollback: legacy store/codec remains readable until approved schema change.

### B16 — Usage, pricing and benchmark sources

Depends: B07, B12, B19. Owner: usage/evidence. Cached-query/source extraction can be prepared against the frozen JobPort earlier; acceptance of public refresh jobs waits for B19. Paths: usage use cases, ledger/source ports, price/benchmark adapters.

Separate metadata activity, settled costs, estimates and subscription allowances. Public cached queries serve UI/MCP. Explicit refresh jobs preserve provenance and age. Request paths consume cached snapshots, not hidden refresh calls. Sensitive content excluded.

Acceptance G2/G5: unknown stays unknown, time/source provenance retained, cache expiry/job failure, duplicated usage event handling, no injected provider cost assumptions. Rollback: source adapters continue reading old ledger formats.

## Wave 4 — kernel and external features

### B17 — Kernel registration and built-in composition

Depends: B01, B03, B12. Owner: kernel/bootstrapping. Paths: kernel registry/grants/ports and builtin descriptors.

Register operation/event descriptors, specialized provider ports and explicit required engine capabilities. Collision, incompatible version and absent required capability fail startup for that composition. Optional capability failure does not kill unrelated features. Built-ins use public descriptors/ports rather than privileged internal lookups.

Acceptance G1/G2: generic namespaced fixture operation works without changing a kernel dispatch switch; private imports fail; missing/cyclic dependency diagnostics; fresh minimal composition starts without any vendor.

### B18 — Supervised external plugin protocol

Depends: B17. Owner: plugin process adapter. Paths: supervisor, stdio transport, activation/token service.

Implement manifest inspection without execution, handshake, scoped dispatch, deadlines, heartbeat, cancellation/drain, crash backoff and resource limits. Implement the provider.execution/v1 proxy binding and a separately packaged deterministic provider, including start/events/tool-result/cancel and explicitly declared resume support. The extension provider is distinct from the generic feature fixture and uses the same ProviderExecution conformance suite as built-ins. Record exact owned process handles; teardown affects only its own children. Reject hostile frames and identity spoofing.

Acceptance G6: malformed/oversized messages, timeout, duplicate registration, revoked token, dependency crash, slow readers and restart-loop cases; external feature and provider fixtures run with private engine imports unavailable; the installed provider is selected through the model library and proves route/credential scope, tool-result identity and cancellation. Rollback: deactivate fixture, keep core available.

### B19 — Brokered plugin data, jobs and events

Depends: B18. Owner: extension capabilities. Paths: storage/job/event brokers and grant schemas already frozen in B01.

Provide plugin-scoped storage with revisions/quotas, durable job status, explicit resumability, schema-checked events and content grants. Enforce namespace/principal on every operation. No cross-plugin private store reads or assumed authorization forwarding.

Acceptance G6: quota, stale revision, revocation during subscription, worker crash interruption, plugin-to-plugin confused-deputy denial, metadata/content distinction. Rollback: retain owned data on deactivation.

### B20 — External package lifecycle

Depends: B18, B19. Owner: extension install/update service. Paths: archive validation, artifact store, activation revisions and migration coordinator.

Implement inspect/install/enable/disable/update/remove; stage and validate side-by-side executable versions and permissions first; stop admission, drain/interrupt jobs and freeze writes before copying/migrating final-revision data; validate non-serving activation and transactionally switch executable/data/grant pointers before new admission. No network/package-manager activation hooks. Trusted-executable status and provenance clearly displayed.

Acceptance G6: zip traversal/symlink/size rejection; failed upgrade keeps prior executable/data usable; uninstall retains data; grants never silently expand; in-flight jobs have explicit outcomes; a write racing the update freeze is included or rejected; failed staged activation tokens cannot keep writing after fallback. Rollback: prior version/data pointer with post-activation data-loss guard.

### B21 — Declarative extension UI and management

Depends: B05, B18, B20. Owner: AppKit plugin renderer/Extensions screen. Paths: shared component schema renderer, presenter and navigation/command registry.

Render declared panels/forms/commands with bindings to public operations. No per-plugin app switch or third-party native binary loading. Validate tree depth/size, accessibility, focus, disabled/loading/error states and asynchronous revisions. Show install/enable/crash/update/grant lifecycle.

Acceptance G3/G6: two synthetic panels render without shell source edits; malformed/unknown required component fails one panel; disabled plugin data is retained; no arbitrary selector/URL execution.

### B22 — Session Notebook as an independently packaged feature

Depends: B19, B20, B21. Owner: example extension (no engine private edits). Paths: `examples/session-notebook` only plus its external tests/docs.

Provide notes CRUD, user-entered content, optional session metadata link, declarative editor/list, cancellable export job and generic CLI invocation. Default no transcript permission. Package/install against staged engine without repository-private imports.

Acceptance G6: user can install, write note, export, disable, update and re-enable with data preserved; metadata-unavailable host still supports manual notes; adding feature required zero kernel/core/shell edits. Any required contract expansion stops and returns to lead.

### B23 — Author SDK, tooling and second-language proof

Depends: B18, B20, B22. Owner: external developer experience. Paths: `sdk/python`, generator/template/CLI docs and `examples/protocol-fixture`.

Implement documented init/validate/dev/test/pack workflow with isolated dev state; package independent of engine. Include command discovery so `model-deck invoke <operation>` works for unknown future extensions. JavaScript fixture implements protocol without Python SDK.

Acceptance G6: fresh temp project builds/installs through documented steps; SDK wheel has no engine imports; non-Python fixture negotiates, invokes, cancels and is disabled correctly. No hidden runtime download.

### B24 — Sandbox feasibility and optional restricted execution mode

Depends: B18, B20. Owner: macOS platform security boundary. Paths: isolated sandbox prototype/tests, execution-profile adapter and documentation.

Determine supported macOS signing/entitlement mechanism for the selected external runtimes without changing current app privileges. Implement a restricted profile only after proving credential/socket/file/network restrictions and child inheritance. If arbitrary runtimes cannot satisfy it, restrict that mode to qualified packages/runtime and retain explicitly trusted executable mode. Unknown profiles reject activation; never advertise a manifest as OS enforcement.

Acceptance G6/G7: adversarial fixtures attempt sibling data/private socket/credential access and unauthorized subprocess/network; restricted profile blocks them at OS level. This gate is required for restricted-mode claims, not satisfied by app API errors. Rollback: disable unsupported profile, disclose trusted-mode boundary.

## Wave 5 — finish product parity, package and qualify

### B25 — Complete native feature presenters and platform attachment

Depends: B05, B10, B15, B16, B21. Owner split only after shared client/presenter contracts: library/connections; usage; platform attachment (separate files).

Migrate remaining overview/connections/model registration/usage/host launch paths to public operations. Isolate AX window tracking/recovery from presentation. Preserve existing key helper, keyboard focus, appearance, minimum-size/reservation recovery and current process lifecycle behavior. Remove legacy UI orchestration only after parity.

Acceptance G3: all five states per page, generation races, stale usage, main actor access, geometry/recovery fixtures. Real AX/host observations require G8 later. No running app action in local gate.

### B26 — Finish packaging, compatibility wrappers and aggregate local gate

Depends: B06, B11, B15, B16, B23, B25; B24 only if restricted mode is included. Owner: packaging/final integration.

Build immutable staged app/engine/SDK artifacts with explicit outputs; include modules/resources/manifests; preserve legacy launch/token entrypoint compatibility and signed-helper bytes. Remove obsolete code only after references/contracts/tests show it unused. Run aggregate local suite once, then rerun only invalidated checks after repairs.

Acceptance G7: clean isolated source builds; expected artifact inventory, signatures, no bytecode mutation of signed resources; headless and plugin fixtures pass with no private source checkout. Existing behavior matrix fully mapped. Local source success is not installation/live provider proof.

### B27 — Scheduled live migration and qualification

Depends: B26. Owner: lead + sole finalizer; user schedules protected-runtime cutover.

Before action, disclose exact app/state targets, snapshot recovery and tool availability impact. Only after that separate authorization quiesce old writers, migrate, activate staged version and qualify real providers/host/AX. Validate billing route, streaming/tools/cancel, compaction, restart state, plugin update and helper identity. Preserve a usable fallback environment for the agent connection.

Acceptance G8: recorded actual route/provider observations and device behavior; failure invokes tested recovery without losing new data. No automatic push, merge, install or kill follows merely from completing this plan.

## Parallel implementation map

- Wave 0: B00 → B01 → B02. Contract decisions remain serial.
- After B02: B03 and B04 can proceed with exact ownership; B06 starts only after B03; B05 follows B04.
- After B07: B08/B09 (sequential) and B12 can progress on separate contracts/modules; B16 cached-query preparation follows B12; its refresh-job acceptance also waits for B19. B10 follows projection seam; B11 follows B10.
- B13 and B14 run in parallel only after B12's provider contract and B10's host seam are merged; B10 owns shared host conversion code and B12 owns shared event types. Provider workers own only their own package conversions, and request shared changes from those owners. B15 integrates both.
- B17 can proceed after B12 while provider integrations continue; B18 → B19 → B20. B21 follows its listed UI prerequisites.
- B22 → B23 proves end-to-end external authoring. B24 is independent once B20 lands. B25 feature owners share only frozen client schemas.
- B26 integrates. B27 is an explicitly separate operational delivery level.

These are dependency waves, not a requirement to use every slot. B00–B26 define complete local implementation of the current product and extension architecture. Other provider/host/platform implementations are not delivery requirements. B27 is required before claiming the replacement installation is qualified. Keep an evidence ledger per slice: commit, paths, contract version, checks, result, gaps, next action. Expected size is dozens of focused commits; revise slice boundaries if a commit cannot be understood or reverted as one coherent step.

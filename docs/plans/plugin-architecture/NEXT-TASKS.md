# B00-B27 bounded follow-up assignments

> Current scope: [V2 starts fresh](V2-SCOPE.md). Prototype saved-state import
> and legacy-setup compatibility are not delivery requirements. Keep the
> prototype running solely to preserve development tool access.

These assignments follow the completion audit in [STATUS.md](STATUS.md). They
are intentionally smaller than the original workstreams. One writer owns each
listed file set; contract changes or ownership collisions return to the primary
agent. Existing untracked files are not assumed available or accepted.

## 2026-09-13 delivery-round outcome

- **A1 delivered (`61a105b`):** B01 is complete at the shared exact revision
  maximum with cross-language fixtures and a non-writing parity/freeze gate.
- **A2 delivered (`e017437`):** B03 is complete. The real graph is zero-error
  without allowlist/rule weakening; the B17 boundary prerequisite is removed.
- **A3 advanced (`e017437`, `779a8da`, `a504e75`):** serving plugins can start, observe,
  cancel, interrupt, and retrieve bounded job results. Explicit safe resume,
  event-revocation/confused-deputy/content-grant proof, and remaining supervisor
  behavior still belong to B19.
- **B22 export advanced (`779a8da`):** Notebook exports actual stored notes and
  V2 observes/retrieves the generic result. Optional session metadata and
  update/re-enable preservation remained at that checkpoint.
- **A8/B22 update advanced (2026-09-14):** public inspect/update/remove, generic
  native update, packaged Notebook preservation, failed-candidate fallback, and
  real switched-boundary restart recovery are delivered. B20 remains open for
  permission/provenance presentation and its remaining original fault/job
  acceptance; B22 remains open for optional session-metadata linkage.
- **Relocated V2 artifact advanced (2026-09-14):** a separately built and moved
  V2 app completed native model create/rename, packaged Notebook editing/export,
  normal quit/reopen persistence, and disable/unavailable/data-retention checks
  with isolated state and exact process ownership. The builder now validates and
  retains the external Python 3.11+ dependency contract. This is accepted
  B04/B05/B25/B26 milestone evidence, not closure of the original workstreams.

B16 refresh work is newly unblocked by the public job path. Do not repeat A1 or
A2; continue the exact remaining requirements above rather than reopening their
completed boundary/contract slices.

## 1. Close now by recording existing evidence

The audit closed B00, B02, B08, B12, B13, and B24 in `STATUS.md`. No additional
implementation task is required for those workstreams. Future aggregate gates
may rerun their checks, but that is B26 evidence collection rather than reopening
the original slice.

## 2. Delivery-round assignments (historical scope and acceptance)

### A1 - Contract parity and current G1 evidence — delivered

- **Outcome:** Python, Swift, and canonical contract resources accept the same
  bounded values and a non-writing check proves bundle parity at one revision.
- **Original workstreams covered:** B01.
- **Ownership:** `contracts/`, `macos/Sources/ModelDeckContracts/`,
  `python/src/model_deck_contracts/`, contract tests, and the contracts branch of
  `scripts/verify.py` only.
- **Reuse:** Existing schemas, golden fixtures, generated bundles, and round-trip
  suites.
- **Dependencies:** None.
- **Acceptance:** Resolve the panel-revision numeric limit; run current valid and
  invalid fixtures in both languages; compare all packaged schema bytes without
  rewriting them; record the audited commit and compatibility result.
- **Non-goals:** New operations, schema redesign, contract generation during the
  check, or consumer implementation.
- **Parallelism:** Can run with A2, A3, V1, V2, A4, and A5 until a public schema
  change is proposed.
- **Stop condition:** Any required public-field or semantic change beyond the
  numeric parity repair returns to the primary agent.

### A2 - Restore the enforced architecture graph — delivered

- **Outcome:** The full current source graph passes with the existing negative
  fixtures and concrete construction remains at composition boundaries.
- **Original workstreams covered:** B03 and the boundary prerequisite of B17.
- **Ownership:** `development/architecture/`, `python/src/model_deck/bootstrap.py`,
  `python/src/model_deck/engine/dispatch.py`, and only the minimum composition
  modules implicated by the 11 reported edges.
- **Reuse:** Current layer model, plugin-runtime layer, kernel composition, and
  public ports.
- **Dependencies:** None; coordinate if A1 changes imports or contracts.
- **Acceptance:** `scripts/architecture_check.py` reports zero errors over the
  full graph; existing negative fixtures still fail; focused dispatch/bootstrap
  tests pass.
- **Non-goals:** Reclassifying forbidden edges as allowed without an ownership
  justification, broad package moves, or completing every B17 built-in.
- **Parallelism:** Can run with all assignments except another writer in the
  listed bootstrap/dispatch files.
- **Stop condition:** If fixing an edge requires a new public contract or moves
  state ownership across systems, return to the primary agent.

### A3 - Public serving plugin-job path — accepted slice delivered; B19 remains

- **Outcome:** A supervised installed plugin can start a durable job, expose its
  status, cancel it, and record worker loss as interrupted.
- **Original workstreams covered:** B19; unlocks B16 refresh and B22 export.
- **Ownership:** `python/src/model_deck/engine/jobs/`, broker dispatch,
  the minimal supervisor broker hookup, and focused tests. Do not adopt existing
  untracked job-wire files without an explicit diff review.
- **Reuse:** Settled invocation authority, SQLite job repository, existing job
  broker, activation identity, and frozen job schemas.
- **Dependencies:** A2 must settle the engine-to-runtime composition boundary;
  job repository/use-case work can proceed in parallel.
- **Acceptance:** Real deterministic child proves job start/get/cancel, terminal
  exclusivity, crash -> interrupted, revocation on subsequent broker access, and
  explicit resume refusal or declared checkpoint resume.
- **Non-goals:** Notebook implementation, lifecycle update/remove, marketplace,
  sandboxing, or live app installation.
- **Parallelism:** Can run with A1, A4-A8, V1, and V2 outside shared dispatch.
- **Stop condition:** Any change to frozen job schemas, authority intersection,
  or resume meaning returns to the primary agent.

## 3. Focused verification-only assignments

### V1 - Swift extraction and Models path acceptance

**Completed 2026-09-14.** At `511dd11`, the legacy-compatible stager produced
`/private/tmp/md-b04-acceptance.gReTAv/artifacts/qualification-511dd11/Model Deck.app`
from fresh isolated roots. Its 1,805-entry inventory matched the executable,
assets, resources, vendor, compatibility alias, and two Swift-bundle contract;
the qualified helper bytes and ad-hoc identity were preserved. Three safe
self-tests and 39 focused Swift cases passed. The live-Keychain self-test was
not run because acceptance prohibited live Keychain access; its unchanged
source/helper scenario remains mapped.

The retained `da32d4f` acceptance record already proves the real Swift client
and Unix transport against the Python fixture engine, including authenticated
handshake, four typed catalog rows, repeated `models.list`, server survival,
and owned cleanup. Current tests re-proved the five presentation states,
cancellation, stale generations, search/selection, bootstrap boundaries,
framing, and transport. The space-key handler remains unchanged from the
pre-extraction source. No catalog-service, presenter, browser-state, or
transport change after that record invalidated the cross-language proof.

- **Outcome:** B04 and B05 are complete against their original acceptance.
- **Original workstreams covered:** B04 and B05.
- **Ownership:** Verification artifacts only; `macos/Package.swift`,
  `ModelDeckClient`, and `ModelDeckPresentation` are read-only unless a reproduced
  defect is separately assigned.
- **Reuse:** `scripts/package/stage.py`, ModelCatalog presenter/client tests, and
  Python fixture engine.
- **Dependencies:** Prepared offline vendor/helper fixtures for the legacy stage.
- **Acceptance:** Isolated Swift build/tests; expected app/resource/helper
  inventory; Swift client -> Python `models.list`; five states, stale generation,
  search/selection, and keyboard behavior. Record commands and artifact roots.
- **Non-goals:** Launch, install, signing identity changes, live state, or UI
  redesign.
- **Parallelism:** Can run with every ready implementation assignment.
- **Stop condition:** A failing assertion becomes a concrete implementation task;
  do not repair it inside this verification assignment.

The relocated V2 artifact at
`/private/tmp/model-deck-v2-final-delivery.ab6ed4/relocated/Model Deck V2.app`
was rechecked with its retained runtime validator; staged and relocated trees
match, and all three Model Deck packages resolve from the relocated bundle.
The final `511dd11` repair changed only the V2 runtime check/build composition
and documentation, so it did not invalidate the earlier native walkthrough or
the B05 catalog acceptance. B25/B26 remain open for their broader parity and
distribution requirements.

### V2 - Connection/model repository conformance

**Completed 2026-09-14.** Six shared behavioral methods now exercise the model
and connection lifecycle, stable identity, revision conflicts, exact replay,
changed-payload conflicts, detached reads, and deterministic competing updates
against both behavioral fakes and temporary SQLite repositories. A seventh
method guards the reference-only public record shapes. SQLite-only
persistence/outbox/rollback tests remain separate, and the public socket
removal scenario still proves a captured route completes while new admission
fails. B07 is closed.

- **Outcome:** Existing B07 behavior is closed by shared fake/SQLite conformance.
- **Original workstreams covered:** B07.
- **Ownership:** Shared conformance fixture and focused repository/run tests only.
- **Reuse:** Existing fake repositories, SQLite repositories, idempotency cases,
  and route snapshots.
- **Dependencies:** None.
- **Acceptance:** The same contract cases run against fake and SQLite; one active
  run retains its captured route after model removal while new admission fails.
- **Non-goals:** Projection, migration, live databases, or repository redesign.
- **Parallelism:** Can run with all ready assignments.
- **Stop condition:** Any semantic disagreement between adapters returns to the
  primary agent as a defect with the smallest reproducer.

## 4. Other ready independent implementation assignments

### A4 - Projection composition and connection fanout

**Completed 2026-09-14.** Bootstrap and dispatch now compose the existing
consumer and durable stores. Isolated full-path acceptance covers connection
fanout, conflict persistence/recovery, removal, and restart. Follow-on host
compatibility belongs to B10.

- **Outcome:** Committed model/connection mutations reconcile to managed fixture
  TOML with durable receipts and conflict-safe rollback.
- **Original workstreams covered:** B09; unlocks B10.
- **Ownership:** Codex projection consumer/composition, snapshot resolvers,
  bootstrap hook after A2, and projection tests.
- **Reuse:** Outbox, dependency receipts/recovery, renderer, materializer, and
  conditional file adapter.
- **Dependencies:** B07 exists; coordinate bootstrap ownership with A2.
- **Acceptance:** Full mutation -> file -> receipt test; `connection.saved`
  re-drives affected live registrations; crash boundaries converge or remain an
  explicit pending conflict; foreign edits are never overwritten.
- **Non-goals:** Live Codex files, migration apply, or host launch.
- **Parallelism:** Consumer/fanout tests can proceed beside A2 until bootstrap
  composition.
- **Stop condition:** Any new outbox event meaning or change to ownership of
  external edits returns to the primary agent.

### A5 - MCP mutation convergence

**Completed 2026-09-14.** With explicit isolated engine paths, the actual
stdio entrypoint routes registered reads plus add/remove/display-name mutations
through authenticated engine operations. The staged source-independent fixture
reaches the same projection; absent paths retain the legacy rollback route.

- **Outcome:** MCP reads and available writes use application operations without
  duplicate storage authority.
- **Original workstreams covered:** B06.
- **Ownership:** `model_deck_mcp.py`,
  `python/src/model_deck/integrations/clients/mcp/`, and MCP fixtures/tests.
- **Reuse:** Existing MCP read service, B07 operations, legacy formatting, and
  named legacy adapter only for genuinely unavailable methods.
- **Dependencies:** B07; benchmark refresh remains deferred to B16.
- **Acceptance:** Add/remove/display-name operations preserve existing MCP names,
  envelopes and errors through public use cases; Swift/CLI/MCP fixture reads
  agree; no direct new storage implementation appears.
- **Non-goals:** MCP tool injection into model runs, B16 refresh implementation,
  or entrypoint packaging.
- **Parallelism:** Fully parallel with A3/A4 and verification tasks.
- **Stop condition:** If a required public mutation is absent or its behavior
  conflicts with the frozen MCP contract, return to the primary agent.

### A6 - Cursor adapter root binding

- **Outcome:** The extracted Cursor adapter owns the existing SDK process path
  behind `ProviderExecutionPort` with fixture parity.
- **Original workstreams covered:** B14; unlocks B15.
- **Ownership:** `python/src/model_deck/integrations/providers/cursor/`, the
  narrow root runtime binding, and Cursor adapter tests.
- **Reuse:** Coordinator, process runtime, legacy SDK fixtures, run state machine,
  and provider port.
- **Dependencies:** B10 host seam only where host context is required; pure SDK
  binding can proceed now.
- **Acceptance:** Account/reasoning/Fast validation, tool callback identity,
  cancel/teardown, malformed/truncated terminal, parallel-session isolation, and
  active-run reuse pass without live Cursor calls.
- **Delivered:** application-owned Cursor profile and V2 composition, pinned
  interpreter verification, real Composer 2.5 tool/edit/test and follow-up
  proof, locally accepted interruption, committed token usage, and exact broker
  cleanup. The Cursor package now owns the broker and atomic SDK install/update
  behavior; the no-network real-process gate covers account/reasoning/Fast,
  callback identity/results, truncation, parallel isolation, active-run reuse,
  feature isolation, cancellation, and teardown.
- **Remaining:** None for B14.
- **Non-goals:** New Cursor features, a live SDK installation, or provider-native
  continuation storage (B15).
- **Parallelism:** Can run with A1-A5 and V1-V2.
- **Stop condition:** Any provider-port or host-context contract change returns
  to the primary agent.

### A7 - External supervisor reliability

- **Outcome:** Installed feature/provider children have explicit health,
  restart, dependency-failure, resource, and owned-process behavior.
- **Original workstreams covered:** B18.
- **Ownership:** `python/src/model_deck/plugins/process_runtime/`, provider proxy,
  external-provider integration fixtures, and their guides/tests.
- **Reuse:** Existing lifecycle session, invocation/provider channels, exact child
  handles, activation tokens, and archived deterministic provider.
- **Dependencies:** A2 for any changed composition edge; otherwise none.
- **Acceptance:** Heartbeat, timeout, crash backoff, restart-loop stop,
  dependency failure, slow reader, malicious frame, credential scope,
  model-library selection, tool-result/cancel identity, and declared resume
  behavior pass through installed fixture packages.
- **Non-goals:** Public background jobs, package update/remove, sandboxing, live
  credentials, or vendor qualification.
- **Parallelism:** Can run with A1, A3-A6, A8, V1, and V2.
- **Stop condition:** A required provider/plugin protocol change or new process
  authority returns to the primary agent.

### A8 - Package update/remove lifecycle — advanced 2026-09-14

- **Outcome:** Public inspect/update/remove completes the existing
  install/enable/disable lifecycle without grant or plugin-data loss.
- **Original workstreams covered:** B20; unlocks B21 and B22 lifecycle acceptance.
- **Ownership:** `python/src/model_deck/plugins/{external_host,activation_lifecycle}/`,
  lifecycle dispatch/CLI, and focused lifecycle tests.
- **Reuse:** Artifact store, lifecycle repository/service, activation authority,
  versioned data freeze/migration, and current V2 install flow.
- **Dependencies:** A7 supplies settled supervisor behavior; record/transition
  implementation can proceed against its existing port.
- **Acceptance:** Inspect is non-executing; update validates non-serving state;
  failed update retains prior executable/data/grants; permission expansion
  requires renewed consent; in-flight jobs get explicit outcomes; remove retains
  data; restart completes or rolls back pending transitions safely.
- **Delivered evidence:** The public socket and CLI expose inspect/update/remove;
  immutable artifacts are resolved by the selected lifecycle record; Notebook
  A-to-B update, late candidate-startup failure, remove, fail-closed permission
  intersection, and durable `SWITCHED` restart recovery pass through real
  subprocesses and persistent SQLite stores. V2 supplies a generic update action
  with visible success/failure state.
- **Still required before closing B20:** trusted-executable/provenance and
  permission-renewal presentation with explicit operator consent, plus the
  remaining original in-flight-job, freeze-race, failed-token, and
  post-activation data-resolution acceptance at the composed boundary.
- **Non-goals:** Marketplace/network install, data deletion UX, native management
  UI, sandboxing, or live installation.
- **Parallelism:** Record/transaction tests can run with A7 until process handoff.
- **Stop condition:** Any change to extension mutation schemas, grant semantics,
  or post-activation data-loss policy returns to the primary agent.

## 5. Dependency-ordered assignments

### D1 - Codex host adapter — delivered

- **Outcome:** Public host preparation reports compatibility and maps Codex
  app-server behavior without global configuration changes.
- **Original workstreams covered:** B10.
- **Ownership:** Codex host integration packages, fake app-server fixtures, and
  host bootstrap only.
- **Reuse:** A4 projection, current bridge primitives, input/tool conversion,
  runtime discovery, and host-settings ports.
- **Dependencies:** A4.
- **Acceptance:** Fake app-server proves available/already-running-unverified/
  incompatible states, unknown-version refusal, method/event/projection parity,
  cancellation ownership, per-process overrides, and host-bound subscription.
- **Delivered:** Package-owned app-server/runtime adapters, the application-owned
  host port, conditional frozen `hosts.list`/`hosts.prepare` engine operations,
  real-adapter socket composition over a fake bundle, and a thin legacy
  entrypoint wrapper. The focused isolated suite passes 94 tests without
  launching Codex or reading live state.
- **Remaining:** None for B10's applicable fake-server acceptance. Live Codex
  Desktop attachment/reload is a separate authorized qualification boundary.
  The [2026-09-14 isolated run](CODEX-DESKTOP-QUALIFICATION-2026-09-14.md)
  proved that V2 agent projection alone does not add the configured model to
  native Desktop `model/list`.
- **Non-goals:** Live Codex, migration, provider execution, or native UI.
- **Parallelism:** Can run with D3-D5 after A4 settles shared host contracts.
- **Stop condition:** Any public host-contract expansion returns to the primary
  agent.

### D1a - V2-owned Codex Desktop attachment — qualified 2026-09-15

- **Outcome:** A V2-owned isolated launch/attachment composes the delivered
  `AppServerBridge` so V2 catalog entries appear in native Desktop `model/list`
  and selected turns route through V2 without displacing host-bound OpenAI
  subscription models.
- **Original workstreams covered:** Newly exposed platform-attachment gap in
  B25; prerequisite evidence for B27. This is not a reopening of B10's
  fake-server adapter acceptance.
- **Ownership:** V2 Desktop launcher/attachment composition, packaged bridge
  entrypoint, and real-host acceptance fixtures. Do not change provider or
  application storage ownership.
- **Evidence:** The [2026-09-14 isolated qualification](CODEX-DESKTOP-QUALIFICATION-2026-09-14.md)
  now records real installed-app-server discovery, V2 selection and routing,
  a disposable edit with passing tests, same-thread continuation, public
  registration reload, billing/tool ownership, and exact owned-process cleanup.
- **Dependencies:** Delivered D1 adapter/bridge and current V2 public catalog,
  router, and authenticated loopback contracts.
- **Acceptance:** A clean staged artifact exposes the V2 model alongside
  host-bound OpenAI models, completes an initial coding task and continuation,
  reloads a model registered after launch, records the correct billing route,
  and cleans up only exact owned processes in isolated state.
- **Non-goals:** Installed-app replacement, live-state mutation, a second
  provider architecture, or claiming an unsupported Codex discovery API.
- **Stop condition:** A required Desktop contract is unavailable or the design
  would bypass application-owned catalog/router ports.
- **Status:** Complete for isolated D1a acceptance. Installation and live
  cutover remain separately authorized B27 work.

### D1b - Native V2 setup and safe Codex connect — delivered 2026-09-15

- **Outcome:** Fresh V2 state can save an OpenRouter credential through the
  existing Keychain helper, compose the non-secret provider profile, restart
  only its owned engine, create and rename the model registration, prepare the
  Codex host, and enable Desktop connection only when attachment is safe.
- **Evidence:** The [D1a qualification record](CODEX-DESKTOP-QUALIFICATION-2026-09-14.md)
  records the isolated GUI setup, clean reopen persistence, rename, ready
  projection, protected-running-Codex refusal, and the unchanged D1a routing,
  coding, continuation, reload, ownership, and billing proof.
- **Original workstreams covered:** Additional B25 native composition evidence
  and B27 prerequisite evidence; neither workstream is complete.
- **Remaining boundary:** A visible picker click and live cutover require a
  separately authorized Codex restart or a separate supported Desktop session.
- **Status:** Delivered for the isolated GUI workflow. No installation or live
  configuration change was performed.

### D2 - Removed: prototype migration

B11 is removed from scope by [V2 starts fresh](V2-SCOPE.md). Do not schedule
prototype import or rollback work. V2's own schema evolution/recovery evidence
remains part of D7; plugin update recovery remains B20.

### D3 - Scoped continuation and compaction

- **Outcome:** Provider/Cursor continuation and both host compaction paths use one
  scope-safe application contract.
- **Original workstreams covered:** B15.
- **Ownership:** Continuation store/port, extracted continuation package, narrow
  provider/host consumers, and parity fixtures.
- **Reuse:** Complete B13 mapping/execution, A6 Cursor adapter, legacy compaction
  fixtures, and opaque host-context handling.
- **Dependencies:** A6.
- **Acceptance:** Provider/account/model/mode crossover refuses or strips private
  state explicitly; both legacy entry paths and SDK continuation pass; failed
  summaries never replace history.
- **Delivered:** Engine-issued route/session scope, private durable OpenAI-compatible
  continuation records, complete provider-item/signature restoration, explicit
  opaque-host stripping, both Codex compaction entry paths, failed-summary
  preservation, store reopen, Cursor's actual SDK callback/history mechanism,
  and a bridge-to-engine-to-provider-to-store post-compaction fixture.
- **Remaining:** None for B15. Job-backed public `sessions.compact` execution
  remains coupled to B19's generic job/history runner; live-provider cache/state
  qualification and unsupported native Cursor resume are not inferred.
- **Non-goals:** New summarization algorithms, cross-provider opaque state, live
  calls, or full UI.
- **Parallelism:** Can run with D1, D2, D4, and D5.
- **Stop condition:** Any proposal makes provider-private or encrypted host state
  portable across scopes.

### D4 - Usage, price, and benchmark sources

- **Outcome:** Cached public usage/evidence queries and explicit refresh jobs
  preserve provenance, age, billing scope, and unknowns.
- **Original workstreams covered:** B16; completes deferred B06 benchmark refresh.
- **Ownership:** Usage/evidence use cases and adapters, refresh-job handlers, UI/
  MCP read consumers, and focused tests.
- **Reuse:** Existing usage ledger/reconciliation/query, A3 jobs, V2 usage client,
  and legacy price/benchmark readers.
- **Dependencies:** A3.
- **Acceptance:** Duplicate events, expiry, failed refresh with stale-good value,
  provenance/time, unknown monetary cost, and subscription/estimate/settled
  separation pass; request paths never refresh implicitly.
- **Non-goals:** Invented provider prices, billed live requests, dashboard redesign,
  or account-wide subscription accounting.
- **Parallelism:** Can run with D1-D3 and D5.
- **Stop condition:** A source cannot expose provenance/scope or would require a
  hidden network action on a read path.

### D5 - External extension experience

- **Outcome:** Lifecycle management UI, complete Notebook behavior, independent
  Python SDK workflow, and JavaScript protocol proof close B21-B23.
- **Original workstreams covered:** B21, B22, and B23.
- **Ownership:** Split non-overlapping owners for native Extension UI, only
  `examples/session-notebook/`, independent SDK/tooling, and only
  `examples/protocol-fixture/` after its untracked diff is reviewed.
- **Reuse:** Generic panel renderer, A3 jobs, A7 supervision, A8 lifecycle,
  validate/pack/invoke commands, and Notebook CRUD/storage.
- **Dependencies:** A3, A7, and A8.
- **Acceptance:** Two unrelated panels and all five states; install/crash/update/
  grant management; Notebook metadata-unavailable manual notes plus cancellable
  export/update retention; fresh SDK project; JavaScript negotiate/invoke/cancel/
  disable without engine imports.
- **Non-goals:** Marketplace, transcript permission, native binaries, sandboxing,
  or shipping-app parity outside the Extensions surface.
- **Parallelism:** UI, Notebook, SDK, and JavaScript files are independent after
  the settled public contracts are recorded.
- **Stop condition:** Any example requires a kernel/engine/shell special case or
  public contract expansion.

### D6 - Native product parity

- **Outcome:** Remaining native pages/settings/platform behavior use public
  operations with preserved user-visible behavior.
- **Original workstreams covered:** B25.
- **Ownership:** Separate owners for overview/connections/registration, usage,
  host settings/launch, and platform attachment; one integration owner for the
  app entrypoint.
- **Reuse:** Existing presenters/clients, D1 host adapter, D3 continuation, D4
  usage sources, D5 management UI, and geometry/host-settings tests.
- **Dependencies:** D1, D3, D4, and the D5 management UI.
- **Acceptance:** Every required page proves five states, generation races,
  main-actor access, settings round-trip/conflict/backup, keyboard/accessibility,
  geometry/focus/appearance/process-lifecycle parity without live AX.
- **New prerequisite:** D1a owns the Desktop launcher/attachment gap exposed by
  the isolated qualification; D6 consumes that accepted public host surface.
- **Non-goals:** App installation, live host observation, visual redesign, or
  removal of legacy code before parity.
- **Parallelism:** Page owners run in parallel after shared client schemas freeze.
- **Stop condition:** A page needs private storage/provider access or a shared
  client contract change.

### D7 - Source-independent package and G7

- **Outcome:** Immutable app/engine/SDK artifacts pass the aggregate local gate
  without a private source checkout.
- **Original workstreams covered:** B26.
- **Ownership:** `scripts/package/`, verification gates, artifact manifests,
  compatibility wrappers, and root documentation/catalog integration.
- **Reuse:** Existing stager, V2 builder evidence, A5 MCP entrypoint,
  D3-D6 accepted components, and behavior matrix.
- **Delivered partial evidence:** the V2 app bundles application Python sources,
  Swift resources, and an external-runtime checker/contract; one moved copy ran
  from an unrelated directory without checkout imports and preserved model and
  Notebook state across normal quit/reopen. Continue from that builder instead
  of creating another V2 packaging path.
- **Dependencies:** A5 and D3-D6.
- **Acceptance:** Clean isolated build; exact resources/modules; actual signatures
  and helper bytes; no bytecode mutation; V2 schema-version, transactional-upgrade and recovery evidence;
  headless/plugin fixtures without source; G1-G7/all-local results recorded.
- **Non-goals:** Install, notarization, live providers/host/AX, or obsolete-code
  deletion without reference proof.
- **Parallelism:** Artifact inventory, compatibility fixtures, and documentation
  can proceed separately before the single aggregate run.
- **Stop condition:** Any command touches an installed bundle/live state or an
  artifact requires undeclared source-tree imports.

### D8 - Scheduled live qualification

- **Outcome:** The accepted staged artifact is installed with fresh state and qualified
  with recoverable real host/provider/device evidence.
- **Original workstreams covered:** B27.
- **Ownership:** Primary agent and one operational verifier; exact target/state/
  snapshot/runbook files only.
- **Reuse:** D7 artifact and checks, V2 recovery evidence, documented isolated coding and
  Notebook scenarios, the [2026-09-14 Desktop model-discovery record](CODEX-DESKTOP-QUALIFICATION-2026-09-14.md),
  and preserved fallback environment.
- **Dependencies:** D1a, D7, plus separate user
  authorization after impact disclosure.
- **Acceptance:** Recorded real route/billing, streaming/tools/cancel/compaction,
  restart state, plugin update, helper identity, host/AX behavior, and recovery
  without losing new data.
- **Non-goals:** Automatic merge, unannounced process termination, credential
  changes, unsupported platforms, or claims from local fixtures.
- **Parallelism:** Preparation is parallelizable; cutover and recovery rehearsal
  have one operational owner.
- **Stop condition:** Authorization is absent, fallback/tool access is uncertain,
  targets drift, or recovery proof fails.

## 6. Scope decisions

No product scope decision is currently required. B24 is resolved as trusted mode
only. A future request to advertise restricted execution would reopen its
qualified-helper/OS-denial work. B27 requires operational authorization after
B26, under the fresh-start scope in V2-SCOPE.md.

## 7. Critical-path wave (raised 2026-09-15)

Sequences the B16-B23/B25-B27 rows still "Implementation/Integration remaining" in STATUS.md. B24 stays closed (see the scope-decisions section above). Acceptance below is taken only from BACKLOG.md (B16-B27, lines 155-253) and STATUS.md's "Exact remaining material requirement" column; nothing is invented.

Lanes: Lane 1 = kernel -> supervisor -> brokers -> lifecycle -> management UI (sequential, shared composition/lifecycle surface). Lane 2 = SDK/tooling. Lane 3 = usage/pricing/benchmarks and the Codex-settings fixture, startable now.

### C0 - Stabilization outcome (placeholder)

- **Outcome:** Landed on 2026-09-15 as four commits on `refactor/plugin-architecture`:
  1. `bd2e224` fix(plugins): conform provider proxy to provider-neutral messages
  2. `a8982c0` fix(providers): keep run completion when continuation save fails
  3. `69163cc` test(plugins): commit JavaScript protocol fixture
  4. `9ccaeb0` feat(verify): implement G2-G6 gates and the test environment contract

  Final gate lines (all-local, 2026-09-15, before the docs commit):
  ```
  development-guard: 37 tests, 0 failures, 0 errors, 3.5s
  contracts: 0 tests, 0 failures, 0 errors, 0.4s
  engine: 1403 tests, 0 failures, 0 errors, 73.9s
  swift: 211 tests, 0 failures, 0 errors, 4.3s
  migration: 329 tests, 0 failures, 0 errors, 8.2s
  providers: 335 tests, 0 failures, 0 errors, 4.8s
  extensions: 362 tests, 0 failures, 0 errors, 14.6s
  ```

  Rejected untracked files from the same triage (moved, never deleted) live at
  `/private/tmp/claude-501/-Users-brytoncooper-Documents-Model-Deck/5fe229cc-3a9a-4a26-8bd8-f76448e26e5f/scratchpad/rejected-untracked/scripts/checks/editing_check.py`
  and `.../rejected-untracked/scripts/checks/__init__.py` (the tracked
  `scripts/editing_check.py` at `91d23f4` was judged authoritative over both).

  Main folder (/Users/brytoncooper/Documents/Model Deck, branch main): 35 stray untracked files (AGENTS.md, docs/, python/; 19 differing from the V2 copies) were moved into stash@{0} 'stray V2 copies moved out of main 2026-09-15' and the tree is clean; git push origin main was rejected with HTTP 403 because the active GitHub credential is brytoncoopertech, which lacks push rights to brytoncooper/model-studio-for-codex, so origin/main remains at 732b393 and main is still 9 commits ahead.
- **Note:** Replaced by a later unit's landing commit; reserves the slot only.

### C1 - B17 kernel: descriptors, vendor-free composition — Lane 1, S-M

- **Outcome:** Remaining built-in/specialized-provider descriptors register with required-capability enforcement at real startup; vendor-free minimal composition starts.
- **Original workstreams covered:** B17.
- **Ownership:** Kernel registry/grants/ports and builtin descriptors; only the composition modules descriptor migration touches — do not reopen A2's settled graph boundary.
- **Reuse:** A2's delivered composition boundary, layer model, plugin-runtime layer, public ports.
- **Dependencies:** None new; builds on delivered A2. First in Lane 1.
- **Acceptance (G1/G2):** built-ins/provider ports use descriptors, not privileged internal lookups; a generic namespaced fixture operation works without changing a kernel dispatch switch; collision/incompatible-version/absent-required-capability fail startup while optional-capability failure stays isolated; missing/cyclic dependency diagnostics fire; a fresh minimal composition starts without any vendor.
- **Non-goals:** Reopening A2's boundary; new operation semantics beyond descriptor migration; broad package moves.
- **Parallelism:** Lane 1; runs alongside C8 and the C9 fixture/parser (Lane 3).
- **Stop condition:** Any public kernel/port contract change beyond descriptor migration returns to the primary agent.

### C2 - B18 supervisor, continues A7 — Lane 1 after C1, L

- **Outcome:** Installed children get proven heartbeat, crash-backoff/restart-loop, dependency-failure, slow-reader/resource/owned-descendant, credential-scope and model-library-selection behavior, a shared built-in/external conformance suite, and declared resume.
- **Original workstreams covered:** B18.
- **Ownership:** `python/src/model_deck/plugins/process_runtime/`, provider proxy, external-provider integration fixtures/guides/tests (continues A7).
- **Reuse:** Existing lifecycle session, invocation/provider channels, exact child handles, activation tokens, archived deterministic provider.
- **Dependencies:** C1. Lane 1, after C1.
- **Acceptance (G6):** heartbeat, timeout, crash backoff, restart-loop stop, dependency-failure, slow-reader, resource, owned-descendant-teardown cases pass; external feature/provider fixtures run with private engine imports unavailable; the installed provider is selected through the model library and proves route/credential scope, tool-result identity, cancellation; a shared built-in/external conformance suite and declared resume exist.
- **Non-goals:** Public background jobs, package update/remove, sandboxing, live credentials, vendor qualification.
- **Parallelism:** Lane 1; C8 and the C9 fixture/parser continue in Lane 3.
- **Stop condition:** Any provider/plugin protocol change or new process authority returns to the primary agent.

### C3 - B19 brokers — Lane 1 after C2, M

- **Outcome:** Brokered storage/jobs/events support safe explicit resume, event-subscription revocation, and confused-deputy/content-grant protection, closing remaining B18 supervisor lifecycle guarantees.
- **Original workstreams covered:** B19.
- **Ownership:** Storage/job/event brokers and the grant schemas frozen in B01; extension-capabilities package only.
- **Reuse:** A3's job repository/broker hookup, activation identity, frozen job/grant schemas.
- **Dependencies:** C2. Lane 1, after C2.
- **Acceptance (G6):** quota, stale-revision, event-subscription revocation during an active subscription, worker-crash interruption, plugin-to-plugin confused-deputy denial, metadata/content-grant distinction pass; explicit safe resume proven without replaying interrupted work; remaining B18 supervisor lifecycle guarantees close.
- **Non-goals:** Notebook implementation, lifecycle update/remove, marketplace, sandboxing, live app installation.
- **Parallelism:** Lane 1; C8 and the C9 fixture/parser continue in Lane 3.
- **Stop condition:** Any change to frozen job/grant schemas or resume meaning returns to the primary agent.

### C4 - B20 lifecycle, continues A8 — Lane 1 after C3, M

- **Outcome:** Package update/remove presents trusted-executable/provenance and grant-renewal information with explicit consent, closing remaining in-flight-job, freeze-race, failed-token, and post-activation data-resolution acceptance.
- **Original workstreams covered:** B20.
- **Ownership:** `python/src/model_deck/plugins/{external_host,activation_lifecycle}/`, lifecycle dispatch/CLI, focused lifecycle tests (continues A8).
- **Reuse:** Artifact store, lifecycle repository/service, activation authority, versioned data freeze/migration, A8's delivered inspect/update/remove path.
- **Dependencies:** C3. Lane 1, after C3.
- **Acceptance (G6):** trusted-executable status/provenance displayed; permission expansion requires explicit renewed operator consent, not silent expansion; zip traversal/symlink/size rejection, failed-upgrade rollback, uninstall data retention, explicit in-flight-job outcomes, update-freeze write races, and failed staged-activation-token fallback pass at the composed boundary; updates keep failing closed to the intersection of prior approvals and newly requested scopes.
- **Non-goals:** Marketplace/network install, data-deletion UX, native management UI, sandboxing, live installation.
- **Parallelism:** Lane 1; C8 and the C9 fixture/parser continue in Lane 3.
- **Stop condition:** Any change to extension mutation schemas, grant semantics, or post-activation data-loss policy returns to the primary agent.

### C5 - B21 management UI — Lane 1 after C4, M

- **Outcome:** The Extensions screen delivers a complete management experience (install/enable/crash/update/grants) across all five panel states, with proven accessibility/focus and a second unrelated panel.
- **Original workstreams covered:** B21.
- **Ownership:** Shared component schema renderer, presenter, navigation/command registry (AppKit plugin renderer / Extensions screen).
- **Reuse:** Generic panel renderer, A3 jobs, A7/C2 supervision, A8/C4 lifecycle, Notebook's proven generic list/editor/action rendering.
- **Dependencies:** C4. Lane 1, after C4.
- **Acceptance (G3/G6):** two synthetic panels render without shell source edits; a malformed/unknown required component fails only one panel; disabled-plugin data is retained; no arbitrary selector/URL executes; the complete management experience, accessibility/focus, and all five panel states are added atop Notebook's proven rendering.
- **Pass condition to agree before start (proposed):** the screen drives install/enable/disable/crash/update/grant actions against real B20 operations; all five states render on Notebook's panel and one second unrelated synthetic panel; every control is keyboard-reachable with visible focus and coherent tab order; a malformed/unknown component fails only its own panel; disabled-plugin data is retained; no arbitrary selector/URL runs.
- **Non-goals:** Per-plugin app switching, third-party native binary loading, arbitrary selector/URL execution.
- **Parallelism:** Lane 1; C8/C9 fixture continue in Lane 3; C9's native integration waits for this unit.
- **Stop condition:** Any panel-schema/component contract expansion, or a required kernel/core/shell special case, returns to the primary agent.

### C6 - B22 optional session-metadata link — after C5, S

- **Outcome:** Session Notebook gains the originally scoped optional session-metadata link.
- **Original workstreams covered:** B22.
- **Ownership:** `examples/session-notebook` only, plus its external tests/docs.
- **Reuse:** Notebook's existing CRUD/editor/export, packaged A-to-B update, retained metadata/revisions, restart persistence (already delivered).
- **Dependencies:** C5. After C5.
- **Acceptance (G6):** the optional session-metadata link is added atop the already-proven install/write/export/disable/update/re-enable-with-data-preserved and metadata-unavailable manual-notes behavior, with zero kernel/core/shell edits; permission-renewal presentation stays a B20/B21 dependency and general job resume stays separate B19 work — neither reopens here.
- **Non-goals:** Broader lifecycle/permission-renewal presentation (B20/B21); general job resume (B19).
- **Parallelism:** Single small unit; does not block Lane 2 or Lane 3.
- **Stop condition:** Any required contract expansion stops and returns to the primary agent.

### C7 - B23 SDK and tooling — Lane 2 after C4, M

- **Outcome:** An independent Python SDK wheel (no engine imports) supports a documented init/dev/test workflow, proven by a fresh-project walkthrough outside the repo.
- **Original workstreams covered:** B23.
- **Ownership:** `sdk/python`, generator/template/CLI docs, `examples/protocol-fixture`.
- **Reuse:** Public generic invoke, committed validate/pack commands, authoring guide.
- **Dependencies:** C4. Lane 2, after C4; the walkthrough also waits for C6.
- **Acceptance (G6):** a fresh temp project builds/installs through documented steps; the SDK wheel has no engine imports; command discovery makes `model-deck invoke <operation>` work for unknown future extensions; the non-Python (JavaScript) fixture negotiates/invokes/cancels/disables correctly; no hidden runtime download occurs.
- **Note:** the JavaScript protocol fixture was accepted and committed on 2026-09-15 (previously flagged untracked/unaudited in STATUS.md); only the SDK/wheel, workflow, and fresh-project walkthrough remain open.
- **Non-goals:** Marketplace distribution, native binaries beyond the JS fixture, hidden runtime downloads, engine-coupled SDK packaging.
- **Parallelism:** Lane 2; independent of Lane 1 after C4 and of Lane 3.
- **Stop condition:** Any change coupling the SDK to engine internals, or a hidden runtime download requirement, returns to the primary agent.

### C8 - B16 usage, pricing, benchmarks — Lane 3, starts now, M

- **Outcome:** Usage/pricing/benchmark source adapters feed provenance- and age-bearing cached queries, served to UI/MCP via explicit refresh jobs on the public job path, with defined cache-failure behavior and settled/estimate/subscription separation.
- **Original workstreams covered:** B16.
- **Ownership:** Usage/evidence use cases and adapters, refresh-job handlers, UI/MCP read consumers, focused tests.
- **Reuse:** Existing usage ledger/reconciliation/query, A3's public job path, V2 usage client, legacy price/benchmark readers.
- **Dependencies:** None blocking. A3 delivered only the public *observation* half of the job path — `jobs.get` and `jobs.cancel`; job creation is plugin-owned through the broker, and there is no public job-create operation. C8d therefore adds a reserved first-party job owner (the `com.modeldeck.engine.` plugin_id prefix with the engine's boot identity as activation_id) so `prices.refresh` and `benchmarks.refresh` can be the public entry points that return an observable `job_id`, rather than exposing job creation. Lane 3, starts now, parallel with C1-C2.
- **Acceptance (G2/G5):** provenance/age-bearing cached queries; explicit refresh jobs on the public job path, never hidden refresh on request paths; UI/MCP consumers read the cache; cache-expiry and failed-refresh-with-stale-good-value proven; duplicated usage events handled; unknown provider monetary cost stays explicitly unknown; settled/estimate/subscription values stay separated.
- **Non-goals:** Invented provider prices, billed live requests, dashboard redesign, account-wide subscription accounting.
- **Parallelism:** Lane 3; fully parallel with Lane 1 (C1-C5) and Lane 2 (C7).
- **Stop condition:** A source that cannot expose provenance/scope, or needs a hidden network action on a read path, returns to the primary agent.

### C9 - B25 native completion — fixture Lane 3 now; native after C5, M

- **Outcome:** Remaining native pages, led by the Codex settings editor, reach five-state/generation/main-actor/geometry/focus/appearance parity and close remaining lifecycle acceptance; legacy UI orchestration is then removed.
- **Original workstreams covered:** B25.
- **Ownership:** Separate owners for overview/connections/registration, usage, host settings/launch (Codex settings editor per CODEX-SETTINGS.md), and platform attachment; one integration owner for entrypoint/legacy removal.
- **Reuse:** Extracted model/usage/settings/platform components, the V2 app, qualified D1a Desktop attachment, native fresh-state OpenRouter/model/Codex preparation.
- **Dependencies:** Codex-settings fixture adapter/parser may start now in Lane 3; native Extensions-screen integration depends on C5.
- **Acceptance (G3):** all five states per page, generation races, stale usage, main-actor access, geometry/recovery fixtures pass; the Codex settings editor is finished; generation/main-actor/geometry/focus/appearance parity and remaining lifecycle cases are proven; legacy orchestration removed only after parity; real AX/host observations stay deferred to G8; no running-app action in the local gate; the visible Desktop picker click stays outside this protected-session GUI run.
- **Pass condition to agree before start (proposed):** each remaining page (overview/connections/registration; usage; host settings/launch incl. Codex settings editor; platform attachment) independently proves all five states, survives a generation race without stale data, touches AppKit state only from the main actor, and preserves geometry/minimum-size/focus/appearance/process-lifecycle behavior under fixture-driven recovery, all without live AX or a running installed app; legacy orchestration is removed only once every page above passes.
- **Non-goals:** App installation, live host observation, visual redesign, removing legacy orchestration before parity is proven.
- **Parallelism:** Fixture/parser in Lane 3 alongside C8, starting now; native integration joins Lane 1's critical path after C5.
- **Stop condition:** A page needing private storage/provider access, or a shared client contract change, returns to the primary agent.

### C10 - B26 packaging — after C7, C8, C9, M

- **Outcome:** Engine and SDK artifacts join the app artifact under one aggregate inventory with real signatures and helper identity (personal Apple Development identity, no bytecode mutation), V2 schema-version/upgrade/recovery evidence, and G7 implemented so all-local runs G0-G7.
- **Original workstreams covered:** B26.
- **Ownership:** `scripts/package/`, verification gates, artifact manifests, compatibility wrappers, root documentation/catalog integration.
- **Reuse:** The legacy-compatible stager, V2 builder, catalog, and the one relocated checkout-independent V2 app with its external-runtime contract.
- **Dependencies:** C7, C8, C9 delivered.
- **Acceptance (G7):** clean isolated source builds produce the expected engine/SDK/app artifact inventory; actual signatures and helper identity use the personal Apple Development identity with no bytecode mutation of signed resources; V2 schema-version/upgrade/recovery evidence recorded; the Codex settings editor (C9) complete; the existing behavior matrix fully mapped; headless/plugin fixtures pass with no private source checkout; G1-G7/all-local gates implemented and run; local source success is not claimed as installation/live-provider proof.
- Pinned external runtime: the builder records the validated interpreter and the relocated app refuses any other; qualification records that path.
- **Non-goals:** Install, notarization, live providers/host/AX, obsolete-code deletion without reference proof.
- **Parallelism:** Single integration unit; waits for all three feeders.
- **Stop condition:** Any command touching an installed bundle/live state, or an artifact requiring undeclared source-tree imports, returns to the primary agent.

### C11 - B27 qualification — after C10 plus authorization, S plus authorization

- **Outcome:** After separate operator authorization and disclosure, a fresh-install qualification records real route/provider/device behavior and tested recovery.
- **Original workstreams covered:** B27.
- **Ownership:** Primary agent plus one independent acceptance reviewer; exact target/state/snapshot/runbook files only.
- **Reuse:** C10's artifact and checks, V2 recovery evidence, documented isolated coding/Notebook scenarios, the D1a Desktop-attachment record.
- **Dependencies:** C10 delivered, plus separate operator authorization.
- **Acceptance (G8):** before any action, exact app/state targets, snapshot recovery, and tool-availability impact are disclosed; only after separate authorization does a fresh-state install qualify real providers/host/AX and record actual billing route, streaming/tools/cancel, compaction, restart state, plugin update, and helper-identity observations, plus tested recovery without losing new data; nothing here triggers an automatic push, merge, install, or kill on its own.
- **Non-goals:** Automatic push, merge, install, or kill; credential changes; unsupported platforms; claims resting on local fixtures alone.
- **Parallelism:** Single terminal unit; nothing in this wave follows it.
- **Stop condition:** Authorization is absent, fallback/tool access is uncertain, targets drift, or recovery proof fails.

### Scope decisions to record in V2-SCOPE.md before C1 starts (approved 2026-09-15; recorded in V2-SCOPE.md)

- Python runtime: V2 keeps depending on a machine-installed Python 3.11 or newer, pinned. The V2 builder records the exact interpreter it validated, and the app refuses to start against any other interpreter. No bundled interpreter is planned; this pinning work belongs to B26 (unit C10 in NEXT-TASKS.md section 7) and B27 records the pinned path in its qualification.
- Signing: B26 signs with the personal Apple Development identity only (team 6W7ABL9KX8). No Developer ID and no notarization; distribution beyond the owner's machines is out of scope.
- Execution mode: B24 stays closed. Plugins run as trusted executable code and the UI states that; no restricted execution mode is claimed or planned.

These are proposals for the primary agent to confirm and record in V2-SCOPE.md; this wave does not edit V2-SCOPE.md itself.

### Gate discipline

Every unit above ends with `scripts/verify.py all-local` green (G7 stays pending until C10 implements it) and a STATUS.md update landing in the same commit as the unit. This wave does not edit STATUS.md directly; each unit's own commit carries that update.

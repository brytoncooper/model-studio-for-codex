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

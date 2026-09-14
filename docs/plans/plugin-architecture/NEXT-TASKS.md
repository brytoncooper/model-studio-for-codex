# B00-B27 bounded follow-up assignments

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
  update/re-enable preservation remain.

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

- **Outcome:** Current B04/B05 implementation has one revision-specific staged
  and cross-language acceptance record.
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

### V2 - Connection/model repository conformance

- **Outcome:** Existing B07 behavior is either closed or reduced to a reproduced
  defect.
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

- **Outcome:** Committed model/connection mutations reconcile to managed fixture
  TOML with durable receipts and conflict-safe rollback.
- **Original workstreams covered:** B09; unlocks B10 and B11.
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
- **Non-goals:** New Cursor features, SDK installation changes, live requests, or
  continuation storage.
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

### A8 - Package update/remove lifecycle

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
- **Non-goals:** Marketplace/network install, data deletion UX, native management
  UI, sandboxing, or live installation.
- **Parallelism:** Record/transaction tests can run with A7 until process handoff.
- **Stop condition:** Any change to extension mutation schemas, grant semantics,
  or post-activation data-loss policy returns to the primary agent.

## 5. Dependency-ordered assignments

### D1 - Codex host adapter

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
- **Non-goals:** Live Codex, migration, provider execution, or native UI.
- **Parallelism:** Can run with D3-D5 after A4 settles shared host contracts.
- **Stop condition:** Any public host-contract expansion returns to the primary
  agent.

### D2 - Offline migration and rollback

- **Outcome:** Fixture legacy state migrates atomically and can be recovered or
  rolled back without losing post-import data.
- **Original workstreams covered:** B11.
- **Ownership:** Migration coordinator, snapshot/journal tooling, schema-version
  ledger, and migration tests.
- **Reuse:** B08 preview, A4 projection receipts, D1 compatibility check, SQLite
  backup, and existing isolated roots.
- **Dependencies:** D1.
- **Acceptance:** Active-writer/fingerprint refusal, WAL-safe backup, interrupted
  apply, migration ID/checksum replay, newer-schema refusal, and rollback with
  conflicts/post-import writes all preserve one authority.
- **Non-goals:** Live data, cutover, downgrade, UI, or provider calls.
- **Parallelism:** Can run beside D3-D5 after D1.
- **Stop condition:** A migration needs destructive live behavior or changes an
  application-owned schema outside its versioned upgrade path.

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
- **Reuse:** Existing stager, V2 builder evidence, A5 MCP entrypoint, D2 migration,
  D3-D6 accepted components, and behavior matrix.
- **Dependencies:** A5 and D2-D6.
- **Acceptance:** Clean isolated build; exact resources/modules; actual signatures
  and helper bytes; no bytecode mutation; legacy launch/token compatibility;
  headless/plugin fixtures without source; G1-G7/all-local results recorded.
- **Non-goals:** Install, notarization, live providers/host/AX, or obsolete-code
  deletion without reference proof.
- **Parallelism:** Artifact inventory, compatibility fixtures, and documentation
  can proceed separately before the single aggregate run.
- **Stop condition:** Any command touches an installed bundle/live state or an
  artifact requires undeclared source-tree imports.

### D8 - Scheduled live qualification

- **Outcome:** The accepted staged artifact is migrated, installed, and qualified
  with recoverable real host/provider/device evidence.
- **Original workstreams covered:** B27.
- **Ownership:** Primary agent and one operational verifier; exact target/state/
  snapshot/runbook files only.
- **Reuse:** D7 artifact and checks, D2 rollback, documented isolated coding and
  Notebook scenarios, and preserved fallback environment.
- **Dependencies:** D7 plus separate user authorization after impact disclosure.
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
B26, not a change to the original scope.

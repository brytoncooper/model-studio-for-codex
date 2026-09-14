# B00-B27 completion audit

The bounded B01/B03/B19/B22 delivery round was integrated 2026-09-13, and the
B20/B22 package-update milestone was integrated 2026-09-14 on
`refactor/plugin-architecture`. Final push state is verified outside this
self-referential document after its commit. The preserved
`backup/pre-atomic-hih5xei2` ref remained at
`7660280cc61757b69fb58e8c15bfc67b691b819d`.

This file is the authoritative current checklist for the original 28
workstreams in [BACKLOG.md](BACKLOG.md). The older range audits
([B00-B09](status/B00-B09.md), [B10-B18](status/B10-B18.md), and
[B19-B27](status/B19-B27.md)) remain historical evidence pointers and are
superseded where they conflict with this audit. A component, test count, or
narrow milestone does not close a broader workstream.

## Result

- **Complete:** 8
- **Implemented; verification remaining:** 3
- **Integration or implementation remaining:** 16
- **Blocked:** 1
- **Scope decision required:** 0

The completed workstreams are B00, B01, B02, B03, B08, B12, B13, and B24. B24 is complete
because its original deliverable was a feasibility investigation and decision,
not a mandatory production sandbox. The decision is to ship trusted executable
mode only unless a separately qualified restricted helper/profile is later
requested; no restricted-mode claim is made.

## Master checklist

| ID | Original deliverable | Status | Current evidence | Exact remaining material requirement | Immediate dependency |
| --- | --- | --- | --- | --- | --- |
| B00 | Safe isolated development operation and G0 guard | **Complete** | [development guard](../../../development/README.md), [verification entrypoint](../../../scripts/verify.py), [guard tests](../../../test_development_guard.py), and [wrapper tests](../../../test_editing_check.py); current G0 ran the protected-path/symlink, unsupported-check, bounded-output, and child-timeout cases and passed 37 tests | None for B00. The broader staged artifact/package gate belongs to B26, not G0. | None |
| B01 | Frozen shared contracts and cross-language encoding | **Complete** | Canonical, Python, and Swift resources share the exact revision range `0...9_007_199_254_740_991`; boundary fixtures pass in both languages; `generate_contracts.py --check` and `scripts/verify.py contracts` compare 200 schemas plus fixtures/inventories without writing | None | None |
| B02 | Headless CLI -> socket -> model-library path | **Complete** | [bootstrap](../../../python/src/model_deck/bootstrap.py), [CLI subprocess tests](../../../python/tests/engine/test_engine_cli_subprocess.py), [transport tests](../../../python/tests/engine/test_engine_transport.py), and [model-library tests](../../../python/tests/engine/test_model_library.py); 41 focused tests passed, with source assertions for absent engine, incompatible handshake, typed fixture/legacy output, and independent clients | None | None |
| B03 | Enforced architecture boundaries on the real graph | **Complete** | The unchanged checker now scans 205 files with zero errors/warnings. Engine dispatch consumes an application gateway, external-host platform/storage dependencies are injected by bootstrap, and Codex/provider concrete factories are supplied by CLI composition. Negative fixtures remain active. | None for B03; B17 still owns broader built-in composition | None |
| B04 | Swift module extraction with staged app parity | **Implemented; verification remaining** | [Swift package](../../../macos/Package.swift), [app target](../../../macos/Sources/ModelDeckApp/README.md), [stager](../../../scripts/package/README.md) | Run one isolated release stage proving compilation, expected executable/assets/resource bundles, original self-test mapping, and helper-byte/signature behavior. No installation or launch is required. | Prepared helper/vendor fixtures; B26 owns the aggregate package gate |
| B05 | Models screen on typed engine client | **Implemented; verification remaining** | [client](../../../macos/Sources/ModelDeckClient/README.md), [presenter tests](../../../macos/Tests/ModelDeckPresentationTests/Models/ModelCatalogPresenterTests.swift), root app wiring | Record one Swift-client-to-Python-fixture run plus the loading/ready/empty/failure/unavailable, stale-generation, search/selection, and keyboard checks. | Isolated Swift/Python fixture |
| B06 | MCP convergence on application use cases | **Integration remaining** | [MCP adapter](../../../python/src/model_deck/integrations/clients/mcp/README.md), [MCP tests](../../../python/tests/engine/test_mcp_model_reads.py) | Route available add/remove/display-name/benchmark mutations through public application operations; prove Swift/CLI/MCP agreement and qualify the staged entrypoint. Reads are already composed. | B07 operations; B16 for benchmark refresh |
| B07 | Revisioned connection/model transactions | **Implemented; verification remaining** | [connections](../../../python/src/model_deck/engine/connections/README.md), SQLite repositories and focused tests | Run one shared fake/SQLite conformance contract and an admission scenario proving removal blocks new runs without changing a captured active route. | None |
| B08 | Deterministic redacted legacy-import preview | **Complete** | [preview guide](../../../python/src/model_deck/integrations/hosts/codex/migration_preview/README.md), [preview tests](../../../python/tests/integrations/hosts/codex/test_migration_preview.py); all 36 deterministic-repeat, hash, collision, malformed/foreign/symlink, preservation, and secret-redaction cases passed with `TMPDIR=/private/tmp` | None; apply/rollback is B11. | None |
| B09 | Conflict-aware host projection/outbox reconciliation | **Implementation remaining** | [projection consumer](../../../python/src/model_deck/integrations/hosts/codex/projection_consumer/README.md), snapshot/invalidation/recovery evidence | Compose the real consumer in bootstrap, re-drive affected registrations on `connection.saved`, and prove mutation -> snapshot -> render -> conditional file -> receipt plus compare-before-write rollback. | B07 state is available |
| B10 | Codex host adapter and compatibility profile | **Implementation remaining** | [Codex bridge primitives](../../../python/src/model_deck/integrations/hosts/codex/bridge.py), legacy bridge/runtime tests | Extract and compose app-server discovery/mapping/launch preparation, compatibility tri-state and unknown-version refusal; prove projection, cancellation ownership, per-process overrides, and host-bound subscription behavior with a fake app-server. The V2 loopback Responses bridge is narrower evidence. | B09 projection composition |
| B11 | Offline migration and rollback rehearsal | **Implementation remaining** | B08 preview and projection receipt migrations | Implement temp-DB apply, exclusive-writer/fingerprint guards, WAL-safe snapshot journal, atomic data/outbox commit, migration IDs/checksums/newer-schema refusal, interruption recovery, and rollback with post-import writes/conflicts. | B08-B10 |
| B12 | Durable sessions/runs state machine with fixture provider | **Complete** | [runs](../../../python/src/model_deck/engine/runs/README.md), [run use-case tests](../../../python/tests/engine/test_run_use_cases.py), [repository tests](../../../python/tests/engine/test_sqlite_session_run_repository.py), dispatch/replay/startup recovery, and V2 workflow; 83 current tests passed for admission replay, single dispatch, recovery, cancellation races, slow readers, capabilities, and route snapshots | None | None |
| B13 | HTTP provider execution and wire translation | **Complete** | Composed [execution port](../../../python/src/model_deck/integrations/providers/openai_compatible/execution.py), [profile composition](../../../python/src/model_deck/integrations/providers/openai_compatible/configuration.py), [execution tests](../../../python/tests/provider_openai_compatible/test_execution.py), request/event/stream tests, and V2 live coding evidence; 124 current tests passed, including pre-body fallback, no retry after a started/decoded response, malformed/truncated streams, tools, cancel, and real-engine injection. Host-native subscription passthrough remains host-bound rather than entering this general endpoint adapter. | None for the original HTTP-adapter slice. The legacy path is retained for rollback/compatibility until B26; Cursor and continuation remain B14/B15 and live cutover remains B27. | None |
| B14 | Cursor execution adapter | **Integration remaining** | [Cursor provider guide](../../../docs/providers/cursor.md), coordinator/process-runtime tests | Bind the extracted adapter to the real SDK/runtime root and prove account routing, reasoning/Fast validation, tool suspension/result identity, cancel/teardown, truncation, parallel sessions, active-run reuse, and feature-loading isolation without live calls. | B10 host/runtime seam |
| B15 | Continuation and compaction parity | **Implementation remaining** | [extracted helpers](../../../python/src/model_deck/integrations/providers/continuation/README.md) | Add the scoped continuation store/port and compose both legacy compaction entry paths, provider and Cursor continuation, encrypted host context, cross-provider/account/model refusal/stripping, and failed-summary preservation. | B14; B13 is complete |
| B16 | Usage, pricing, benchmark sources and refresh | **Implementation remaining** | [usage owner](../../../python/src/model_deck/engine/usage/README.md), public `engine.v1.usage.query`, V2 usage UI/live token evidence | Add pricing/benchmark source adapters, provenance/age-bearing cached queries and explicit refresh jobs, UI/MCP consumers, cache-failure behavior, and the settled/estimate/subscription separation. Provider monetary cost remains unknown. | B19 public job execution |
| B17 | Kernel registration and built-in composition | **Integration remaining** | [kernel](../../../docs/kernel.md), [composition](../../../python/src/model_deck/engine/kernel_composition.py), authenticated generic invocation tests; the former engine-to-plugin-runtime edge is removed behind the application gateway | Migrate static built-ins and specialized provider ports to descriptors, enforce required capabilities at real startup, and prove a minimal vendor-free composition. | None for boundary repair |
| B18 | Supervised external plugin/provider protocol | **Implementation remaining** | Process runtime/invocation channels, [provider proxy](../../../python/src/model_deck/plugins/provider_proxy/README.md), archived deterministic-provider integration | Add heartbeat, crash backoff/restart-loop and dependency-failure supervision; finish resource/slow-reader/owned-descendant guarantees, credential-scope/model-library-selection proof, shared built-in/external conformance, and explicit resume support. | B17 composition and B19 brokers |
| B19 | Brokered plugin data, jobs, and events | **Implementation remaining** | Serving external plugins receive storage/job brokers; public `jobs.get`/`jobs.cancel`, owner/activation persistence, bounded result retrieval, terminal exclusivity, cancellation acknowledgement, worker-loss interruption, and restart non-replay pass through the real socket/SQLite path | Add safe explicit resume/runner behavior, event-subscription revocation, confused-deputy/content-grant coverage, and the remaining supervisor lifecycle guarantees. The delivered path intentionally does not replay interrupted work. | B18 serving supervisor |
| B20 | External package lifecycle | **Implementation remaining** | [external host](../../../python/src/model_deck/plugins/external_host/README.md), public CLI/socket inspect/install/enable/disable/update/remove, generic V2 update control, immutable side-by-side artifacts, selected-artifact discovery, non-serving validation, failed-candidate rollback, and real switched-boundary restart recovery | Add trusted-executable/provenance and grant-renewal presentation, explicit operator consent for added permissions, and the remaining original in-flight-job, freeze-race, failed-token, and post-activation data-resolution acceptance. Updates currently fail closed to the intersection of prior approvals and newly requested scopes. | B18-B19 |
| B21 | Declarative extension UI and lifecycle management | **Implementation remaining** | [generic panel decoder/renderer](../../../macos/Sources/ModelDeckPresentation/ExtensionUI/README.md), [V2 app](../../../macos/Sources/ModelDeckV2/README.md) | Add the complete Extensions management experience (install/enable/crash/update/grants), accessibility/focus and all five panel states, and a second unrelated panel proof. Notebook already proves generic list/editor/action rendering. | B20 lifecycle for management states |
| B22 | Independently packaged Session Notebook | **Implementation remaining** | [Notebook](../../../examples/session-notebook/README.md) CRUD/editor/export plus packaged A-to-B update, retained metadata and revisions, post-update edits, failed-C fallback, restart persistence, and isolated native V2 editing/export are proven. Earlier disable/re-enable retention and deterministic cancellation/non-replay evidence remain valid. | Add the original optional session-metadata link. Public permission-renewal presentation remains a B20/B21 dependency; general job resume remains separate B19 work. | B20/B21 permission presentation for the broader management experience |
| B23 | Author SDK/tooling and second-language proof | **Implementation remaining** | Public generic invoke and committed validate/pack commands; [authoring guide](../../../python/src/model_deck/plugins/authoring/README.md) | Deliver independent Python SDK/wheel, init/dev/test workflow and fresh-project walkthrough; commit and prove the JavaScript negotiate/invoke/cancel/disable fixture. Existing untracked protocol-fixture work is not audited delivery. | B18/B20 stable lifecycle; B22 acceptance example |
| B24 | Decide optional restricted execution feasibility | **Complete** | [feasibility decision](../../../docs/architecture/restricted-execution-feasibility.md) | Complete only for the feasibility/ship-scope decision: trusted mode ships and restricted mode is not claimed. A future restricted-mode request would require the currently unsatisfied signed helper/profile, runtime qualification, sibling-file/socket/credential/network/subprocess denial, and descendant-inheritance evidence. | None |
| B25 | Native presenters and platform attachment parity | **Integration remaining** | Extracted model/usage/settings/platform components, [V2 app](../../../macos/Sources/ModelDeckV2/README.md) | Complete overview/connections/registration/usage/host-launch public-operation wiring; finish the Codex settings editor; prove all five states, generation/main-actor/geometry/focus/appearance/process-lifecycle parity; remove legacy orchestration only afterward. | B10, B15, B16, B21 |
| B26 | Immutable packaging, compatibility, docs, aggregate G7 | **Integration remaining** | [legacy-compatible stager](../../../scripts/package/README.md), [V2 builder](../../../scripts/v2/README.md), [catalog](../../architecture/CATALOG.md) | Produce source-independent app/engine/SDK artifacts; prove resource inventory, actual signatures/helper identity, no bytecode mutation, legacy launcher/token compatibility, completed settings editor, behavior-matrix mapping, and implemented G1-G7/all-local gates. V2 remains unsigned and depends on an external Python environment. | B06, B11, B15-B16, B23, B25 |
| B27 | Scheduled live migration and qualification | **Blocked** | V2 coding and Notebook are isolated milestone evidence only | After B26, separately authorize exact target/state paths, snapshot/recovery and tool availability; then qualify installed migration, real host/AX, routes/billing, streaming/tools/cancel/compaction/restart/plugin update/helper identity and recovery. | B26 plus separate operational authorization |

## Milestone evidence and limits

- `18d9b51`, `a0bf599`, and `8b4099b` establish the packaged Notebook,
  generic native rendering, repeated editing, persistence, clean shutdown, and
  disable/re-enable retention in an isolated V2 app. They materially advance
  B18-B22 and B25-B26 but do not supply Notebook jobs/update, the full lifecycle
  manager, the author SDK, native product parity, or distribution evidence.
- `61a105b`, `e017437`, `779a8da`, and `a504e75` establish the contract parity repair,
  zero-error dependency graph, serving public plugin-job path, and bounded
  Notebook Markdown export. An isolated V2 build at
  `/tmp/model-deck-v2-export.01T1L3` visibly installed/enabled the package,
  stored a note, showed two job identities/status/progress, and retrieved the
  expected Markdown. The manual cancel click lost the fast completion race;
  terminal cancellation is therefore supported by the deterministic
  broker/runtime acceptance test, not claimed from that UI click.
- The 2026-09-14 B20/B22 milestone adds public inspect/update/remove, immutable
  version lookup, fail-closed grant intersection, and startup recovery of the
  real lifecycle journal. Packaged Notebook A-to-B acceptance preserves note
  metadata/revisions, post-update writes, export output, and restart state. A
  valid archive with a deliberately mismatched worker version fails after
  staging without displacing B. A subprocess interruption at durable
  `SWITCHED` proves prior-engine authority revocation, fresh candidate identity,
  selected B data preservation, settlement, and exact request replay.
- An isolated unsigned V2 build under `/tmp/model-deck-b20.6PAKf0` visibly
  installed/enabled A, loaded two synthetic notes, saved native edits, updated
  to B with a `1.1.0` panel marker, exported both notes, reported failing C,
  edited again through B, and reopened B with the final edit. The second build
  also kept the successful version result visible after panel refresh. Both
  isolated app/engine process trees were stopped; no live installation or
  provider request occurred.
- `37378de` and `548b24d` establish a real isolated Codex CLI workflow through
  the V2 loopback bridge and composed OpenAI-compatible provider: tools, project
  mutation, passing test, same-thread continuation, usage, and disconnect-driven
  cancellation. The committed guide records `32,453` input, `509` output, and
  `12,800` cached-input tokens for the final reviewed run. The handoff's
  `41,009 / 575 / 20,480` figures were not found in committed evidence and are
  therefore not substituted. Remote termination remains unconfirmed where the
  terminal is `run.interrupted`.
- Codex Desktop UI, parallel tool calls, full opaque compaction,
  provider-reported monetary cost, signing/notarization, installation, and live
  cutover remain unqualified.

## Audit checks and findings

Current delivery checks:

- Contract generation was intentional (`--write`), followed by non-writing
  `--check`: 200 schemas and all bundled fixtures/inventories agree. The repair
  suite passed 24 Python tests plus 80 subtests and 25 Swift contract tests.
- The combined Python gate passed 184 tests plus 42 subtests. The focused
  public-job/Notebook set passed 92 tests plus 23 subtests, including the real
  Unix-socket, SQLite, subprocess, cancellation, result, worker-loss, and
  restart/non-replay scenario.
- The final durable cancellation-idempotency repair passed 41 focused job,
  SQLite, and real-socket tests; a principal/key now replays its stored result
  and conflicts if reused for another job.
- The architecture scan moved from 204 files/11 errors to 205 files/zero errors
  and zero warnings; focused boundary behavior passed 59 tests plus 8 subtests.
- Targeted native job observation passed 8 tests. The combined Swift run found
  one invalid new fixture among 77 tests; after adding its required `kind`, the
  invalidated 25-test contract slice passed. A release V2 artifact then built
  and completed the isolated UI walkthrough above.
- `git diff --check` passed. No live provider request, signing, permanent
  installation, migration, cutover, or protected-runtime action occurred.

Independent Luna reviews reported no material findings for the contract,
boundary, or product slices. Existing untracked protocol-fixture/provider-guide,
client-package, check-script, and lockfile work remains excluded.

## Next work

The remaining bounded assignments are in [NEXT-TASKS.md](NEXT-TASKS.md). B16 is
newly unblocked by the public job path. B17 no longer depends on boundary repair,
and B22 export no longer depends on result delivery; their original remaining
requirements stay explicit above.

# B00-B27 completion audit

Source and evidence were audited 2026-09-13 at pushed revision
`548b24de486c1d744847c5f881dd34a351a26f56` on
`refactor/plugin-architecture`; this status document was authored afterward and
does not change the audited source revision. At audit start, local `HEAD` and
`origin/refactor/plugin-architecture` were equal. The preserved
`backup/pre-atomic-hih5xei2` ref remained at
`7660280cc61757b69fb58e8c15bfc67b691b819d`.

This file is the authoritative current checklist for the original 28
workstreams in [BACKLOG.md](BACKLOG.md). The older range audits
([B00-B09](status/B00-B09.md), [B10-B18](status/B10-B18.md), and
[B19-B27](status/B19-B27.md)) remain historical evidence pointers and are
superseded where they conflict with this audit. A component, test count, or
narrow milestone does not close a broader workstream.

## Result

- **Complete:** 6
- **Implemented; verification remaining:** 3
- **Integration or implementation remaining:** 18
- **Blocked:** 1
- **Scope decision required:** 0

The completed workstreams are B00, B02, B08, B12, B13, and B24. B24 is complete
because its original deliverable was a feasibility investigation and decision,
not a mandatory production sandbox. The decision is to ship trusted executable
mode only unless a separately qualified restricted helper/profile is later
requested; no restricted-mode claim is made.

## Master checklist

| ID | Original deliverable | Status | Current evidence | Exact remaining material requirement | Immediate dependency |
| --- | --- | --- | --- | --- | --- |
| B00 | Safe isolated development operation and G0 guard | **Complete** | [development guard](../../../development/README.md), [verification entrypoint](../../../scripts/verify.py), [guard tests](../../../test_development_guard.py), and [wrapper tests](../../../test_editing_check.py); current G0 ran the protected-path/symlink, unsupported-check, bounded-output, and child-timeout cases and passed 37 tests | None for B00. The broader staged artifact/package gate belongs to B26, not G0. | None |
| B01 | Frozen shared contracts and cross-language encoding | **Implementation remaining** | [API](API.md), [contracts](../../../contracts/README.md), Python/Swift contract tests | Bound panel revisions consistently across schema/Python/Swift (the [native guide](../../../macos/Sources/ModelDeckPresentation/ExtensionUI/README.md) records the mismatch), add a non-writing bundle-parity check, and record a current cross-language freeze result. | Contract owner; no upstream code dependency |
| B02 | Headless CLI -> socket -> model-library path | **Complete** | [bootstrap](../../../python/src/model_deck/bootstrap.py), [CLI subprocess tests](../../../python/tests/engine/test_engine_cli_subprocess.py), [transport tests](../../../python/tests/engine/test_engine_transport.py), and [model-library tests](../../../python/tests/engine/test_model_library.py); 41 focused tests passed, with source assertions for absent engine, incompatible handshake, typed fixture/legacy output, and independent clients | None | None |
| B03 | Enforced architecture boundaries on the real graph | **Integration remaining** | [checker](../../../scripts/architecture_check.py), [rules](../../../development/architecture/README.md) | Make the actual graph pass. Current scan checked 204 files and reported 11 forbidden edges in dispatch, Codex bridge, provider configuration, and external-host composition; retain the negative fixtures while moving concrete composition to allowed boundaries. | Coordinate B17 composition ownership |
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
| B17 | Kernel registration and built-in composition | **Integration remaining** | [kernel](../../../docs/kernel.md), [composition](../../../python/src/model_deck/engine/kernel_composition.py), authenticated generic invocation tests | Migrate static built-ins and specialized provider ports to descriptors, enforce required capabilities at real startup, and prove a minimal vendor-free composition. Also remove the B03-forbidden engine -> plugin-runtime dependency. | B03 boundary repair |
| B18 | Supervised external plugin/provider protocol | **Implementation remaining** | Process runtime/invocation channels, [provider proxy](../../../python/src/model_deck/plugins/provider_proxy/README.md), archived deterministic-provider integration | Add heartbeat, crash backoff/restart-loop and dependency-failure supervision; finish resource/slow-reader/owned-descendant guarantees, credential-scope/model-library-selection proof, shared built-in/external conformance, and explicit resume support. | B17 composition and B19 brokers |
| B19 | Brokered plugin data, jobs, and events | **Implementation remaining** | [plugin authority/data](../../../python/src/model_deck/engine/plugin_authority/README.md), [jobs](../../../python/src/model_deck/engine/jobs/README.md), event broker; Notebook proves real storage transport | Compose all brokers behind the serving supervisor; add public job get/cancel and explicit resume/runner behavior; prove crash interruption, revocation during subscription, confused-deputy denial, content grants, and retained data through lifecycle changes. | B18 serving supervisor |
| B20 | External package lifecycle | **Implementation remaining** | [external host](../../../python/src/model_deck/plugins/external_host/README.md), activation/data composition, public install/enable/disable used by V2 | Implement inspect/update/remove, permission/provenance presentation, staged non-serving validation, grant-renewal rules, in-flight job outcomes, and failed-update/rollback guarantees. Current host explicitly lacks update and pending-work recovery. | B18-B19 |
| B21 | Declarative extension UI and lifecycle management | **Implementation remaining** | [generic panel decoder/renderer](../../../macos/Sources/ModelDeckPresentation/ExtensionUI/README.md), [V2 app](../../../macos/Sources/ModelDeckV2/README.md) | Add the complete Extensions management experience (install/enable/crash/update/grants), accessibility/focus and all five panel states, and a second unrelated panel proof. Notebook already proves generic list/editor/action rendering. | B20 lifecycle for management states |
| B22 | Independently packaged Session Notebook | **Implementation remaining** | [Notebook](../../../examples/session-notebook/README.md), commits `18d9b51`, `a0bf599`, `8b4099b`; V2 proves install, native repeated edit, restart persistence, disable/re-enable retention | Add optional session-metadata linkage and a cancellable export job with result/attachment handling; prove package update plus re-enable preservation. Synchronous Markdown preview is not the planned job. | B19 jobs and B20 update |
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

Focused checks run against the audited revision:

- `scripts/verify.py development-guard`: 37 tests passed.
- Model-library CLI/transport/use cases: 41 tests passed.
- Run/repository/dispatch/replay: 83 tests passed.
- OpenAI-compatible execution/events/mapping/streaming: 124 tests passed.
- Migration preview: 36 tests passed with `TMPDIR=/private/tmp`. The first run
  used the macOS `/tmp` symlink spelling and was rejected by the fixture-root
  guard; the canonical-root rerun passed.
- Architecture scan: 204 files checked, **11 errors**. This is a current B03
  integration failure, not a passed gate.

No Swift build, package build, contract generation, live provider request,
installation, signing, migration, app launch, or protected-runtime action was
performed. `scripts/generate_contracts.py --check` was not run because it writes
generated bundles.

Priority findings using the project-audit rubric:

1. **Watch - the enforced architecture graph is currently red.** Evidence: 11
   forbidden imports from engine/host/provider/plugin-runtime modules. Impact:
   B03 and B17 cannot close and future integration can deepen boundary drift.
   Confidence: high. Next module: implementation, followed by change review.
2. **Watch - broad milestone success masks specific unfinished original scope.**
   Evidence: Notebook lacks cancellable export/update proof; V2 lacks distribution
   and full native parity; usage lacks pricing/refresh sources. Impact: B18-B23
   and B25-B27 remain open despite working product paths. Confidence: high. Next
   module: task creation, using [NEXT-TASKS.md](NEXT-TASKS.md).
3. **Info - the handoff and committed coding-usage totals conflict.** Evidence:
   the V2 guide at `548b24d` records the lower totals above. Impact: no behavior
   verdict changes, but the larger totals cannot be cited as repository evidence.
   Confidence: high. Next module: context survey only if the missing artifact is
   later supplied.
4. **Info - unrelated untracked work exists and was excluded.** It includes the
   protocol fixture, job wire, provider execution guide, check scripts, and
   `python/uv.lock`. Impact: none on this documentation commit; none is counted
   as delivered. Confidence: high. Next module: none.

## Next work

The prioritized, bounded, parallelizable assignments are in
[NEXT-TASKS.md](NEXT-TASKS.md). The first three are contract parity, architecture
boundary restoration, and serving plugin jobs/lifecycle. No implementation was
started by this audit.

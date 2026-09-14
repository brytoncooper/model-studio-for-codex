# B00-B27 completion audit

> Current scope: [V2 starts fresh](V2-SCOPE.md). Prototype saved-state import
> and legacy-setup compatibility are not delivery requirements. Keep the
> prototype running solely to preserve development tool access.

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

- **Complete:** 16
- **Implemented; verification remaining:** 0
- **Integration or implementation remaining:** 10
- **Blocked:** 1
- **Removed from scope:** 1 (B11; not completed)
- **Scope decision required:** 0

The completed workstreams are B00, B01, B02, B03, B04, B05, B06, B07, B08,
B09, B10, B12, B13, B14, B15, and B24. B24 is complete
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
| B03 | Enforced architecture boundaries on the real graph | **Complete** | The unchanged checker now scans 213 files with zero errors/warnings. Engine dispatch consumes an application gateway, external-host platform/storage dependencies are injected by bootstrap, and Codex/provider concrete factories are supplied by CLI composition. Negative fixtures remain active. | None for B03; B17 still owns broader built-in composition | None |
| B04 | Swift module extraction with staged app parity | **Complete** | At `511dd11`, the legacy-compatible stager produced `/private/tmp/md-b04-acceptance.gReTAv/artifacts/qualification-511dd11/Model Deck.app` from fresh isolated roots. Its 1,805-entry inventory contains the expected executable alias, assets, legacy resources, vendor tree, and both Swift resource bundles; helper input/output SHA256 is `f425e401…0338f` and its ad-hoc identity is `com.cooper.codex-openrouter.credential-helper`. The safe companion, model-browser, and usage self-tests passed; source maps the prohibited live-Keychain scenario unchanged. Focused Swift compilation/tests passed 39 cases. | None for original B04. Production signing/notarization, aggregate G7, V2-required runtime qualification, and distribution remain B26/B27. | None |
| B05 | Models screen on typed engine client | **Complete** | `da32d4f` recorded the actual Swift client/Unix transport against the Python fixture engine: two-step authenticated handshake, four typed catalog items, repeated `models.list` on the same socket, and owned cleanup. Current focused tests at `511dd11` passed 39 cases including loading/ready/empty/failure/unavailable, cancellation, stale generations, search/selection, bootstrap fallback boundaries, framing, and transport. The staged browser self-test passed and the space-key handler remains source-equivalent to the pre-extraction implementation. | None for original B05. Broader V2 management/native parity and distribution remain B25/B26. | None |
| B06 | MCP convergence on application use cases | **Complete** | [MCP adapter](../../../python/src/model_deck/integrations/clients/mcp/README.md), engine-backed add/remove/display-name/list tests, and staged stdio entrypoint proof against an authenticated isolated engine | None for original B06. Benchmark refresh remains B16 and legacy fallback remains selected when engine paths are absent. | None |
| B07 | Revisioned connection/model transactions | **Complete** | The shared [repository conformance suite](../../../python/tests/engine/test_repository_conformance.py) runs the same lifecycle, stable-identity, revision-conflict, exact-replay/payload-conflict, detached-read, reference-only-record, and deterministic competing-update cases against behaviorally accurate fakes and SQLite. SQLite-only persistence/outbox/rollback coverage remains separate. The public socket test still proves removal rejects new admission while the already-admitted run completes on its captured route. | None | None |
| B08 | Deterministic redacted legacy-import preview | **Complete** | [preview guide](../../../python/src/model_deck/integrations/hosts/codex/migration_preview/README.md), [preview tests](../../../python/tests/integrations/hosts/codex/test_migration_preview.py); all 36 deterministic-repeat, hash, collision, malformed/foreign/symlink, preservation, and secret-redaction cases passed with `TMPDIR=/private/tmp` | None; prototype apply/rollback is removed from scope. | None |
| B09 | Conflict-aware host projection/outbox reconciliation | **Complete** | [projection composition](../../../python/src/model_deck/integrations/hosts/codex/projection_composition/README.md), bootstrap/dispatch coordinator, persisted status, and isolated full-path tests for add/rename/connection fanout/remove/restart/foreign conflict recovery | None for original B09. Live Codex Desktop reload remains separate qualification and authorized tool switching remains B27. | None |
| B10 | Codex host adapter and compatibility profile | **Complete** | [Codex host guide](../../../python/src/model_deck/integrations/hosts/codex/README.md), extracted app-server/runtime adapters, frozen engine host operations, thin legacy wrapper, and 94 focused fake-bundle/app-server tests | None for applicable B10 acceptance. Live Codex Desktop attachment/reload remains unqualified and is not inferred from isolated fixtures. | None |
| B11 | Prototype migration and rollback rehearsal | **Removed from scope** | [Fresh-start decision](V2-SCOPE.md) | No prototype import required. V2 schema evolution and recovery remain B26 acceptance; plugin recovery remains B20. | None |
| B12 | Durable sessions/runs state machine with fixture provider | **Complete** | [runs](../../../python/src/model_deck/engine/runs/README.md), [run use-case tests](../../../python/tests/engine/test_run_use_cases.py), [repository tests](../../../python/tests/engine/test_sqlite_session_run_repository.py), dispatch/replay/startup recovery, and V2 workflow; 83 current tests passed for admission replay, single dispatch, recovery, cancellation races, slow readers, capabilities, and route snapshots | None | None |
| B13 | HTTP provider execution and wire translation | **Complete** | Composed [execution port](../../../python/src/model_deck/integrations/providers/openai_compatible/execution.py), [profile composition](../../../python/src/model_deck/integrations/providers/openai_compatible/configuration.py), [execution tests](../../../python/tests/provider_openai_compatible/test_execution.py), request/event/stream tests, and V2 live coding evidence; 124 current tests passed, including pre-body fallback, no retry after a started/decoded response, malformed/truncated streams, tools, cancel, and real-engine injection. Host-native subscription passthrough remains host-bound rather than entering this general endpoint adapter. | None for the original HTTP-adapter slice. Existing legacy code need not be removed, but prototype compatibility is not a B26 gate; Cursor and continuation remain B14/B15 and live cutover remains B27. | None |
| B14 | Cursor execution adapter | **Complete** | [Cursor provider guide](../../../docs/providers/cursor.md), package-owned SDK broker/atomic installer, coordinator/process-runtime/profile tests, no-network real-process fake-SDK gate for account and reasoning/Fast routing, callback identity/results, truncation, parallel/reuse, feature isolation, cancellation and owned teardown; isolated live Composer 2.5 proof covers V2 editing, checks, Codex-thread follow-up, interruption, usage, and cleanup | None for original B14. Provider-native continuation remains B15 and remote cancellation confirmation remains unavailable rather than inferred. | None |
| B15 | Continuation and compaction parity | **Complete** | [continuation contract/store and compatibility matrix](../../../python/src/model_deck/integrations/providers/continuation/README.md), composed OpenAI-compatible execution, Codex bridge compaction, engine scope persistence, and Cursor guide/harness | None for the original parity milestone. Generic job-backed `sessions.compact` remains a B19-dependent public surface and is not fabricated; Cursor has no verified portable native SDK resume API. | None |
| B16 | Usage, pricing, benchmark sources and refresh | **Implementation remaining** | [usage owner](../../../python/src/model_deck/engine/usage/README.md), public `engine.v1.usage.query`, V2 usage UI/live token evidence | Add pricing/benchmark source adapters, provenance/age-bearing cached queries and explicit refresh jobs, UI/MCP consumers, cache-failure behavior, and the settled/estimate/subscription separation. Provider monetary cost remains unknown. | B19 public job execution |
| B17 | Kernel registration and built-in composition | **Integration remaining** | [kernel](../../../docs/kernel.md), [composition](../../../python/src/model_deck/engine/kernel_composition.py), authenticated generic invocation tests; the former engine-to-plugin-runtime edge is removed behind the application gateway | Migrate static built-ins and specialized provider ports to descriptors, enforce required capabilities at real startup, and prove a minimal vendor-free composition. | None for boundary repair |
| B18 | Supervised external plugin/provider protocol | **Implementation remaining** | Process runtime/invocation channels, [provider proxy](../../../python/src/model_deck/plugins/provider_proxy/README.md), archived deterministic-provider integration | Add heartbeat, crash backoff/restart-loop and dependency-failure supervision; finish resource/slow-reader/owned-descendant guarantees, credential-scope/model-library-selection proof, shared built-in/external conformance, and explicit resume support. | B17 composition and B19 brokers |
| B19 | Brokered plugin data, jobs, and events | **Implementation remaining** | Serving external plugins receive storage/job brokers; public `jobs.get`/`jobs.cancel`, owner/activation persistence, bounded result retrieval, terminal exclusivity, cancellation acknowledgement, worker-loss interruption, and restart non-replay pass through the real socket/SQLite path | Add safe explicit resume/runner behavior, event-subscription revocation, confused-deputy/content-grant coverage, and the remaining supervisor lifecycle guarantees. The delivered path intentionally does not replay interrupted work. | B18 serving supervisor |
| B20 | External package lifecycle | **Implementation remaining** | [external host](../../../python/src/model_deck/plugins/external_host/README.md), public CLI/socket inspect/install/enable/disable/update/remove, generic V2 update control, immutable side-by-side artifacts, selected-artifact discovery, non-serving validation, failed-candidate rollback, and real switched-boundary restart recovery | Add trusted-executable/provenance and grant-renewal presentation, explicit operator consent for added permissions, and the remaining original in-flight-job, freeze-race, failed-token, and post-activation data-resolution acceptance. Updates currently fail closed to the intersection of prior approvals and newly requested scopes. | B18-B19 |
| B21 | Declarative extension UI and lifecycle management | **Implementation remaining** | [generic panel decoder/renderer](../../../macos/Sources/ModelDeckPresentation/ExtensionUI/README.md), [V2 app](../../../macos/Sources/ModelDeckV2/README.md) | Add the complete Extensions management experience (install/enable/crash/update/grants), accessibility/focus and all five panel states, and a second unrelated panel proof. Notebook already proves generic list/editor/action rendering. | B20 lifecycle for management states |
| B22 | Independently packaged Session Notebook | **Implementation remaining** | [Notebook](../../../examples/session-notebook/README.md) CRUD/editor/export plus packaged A-to-B update, retained metadata and revisions, post-update edits, failed-C fallback, restart persistence, and isolated native V2 editing/export are proven. Earlier disable/re-enable retention and deterministic cancellation/non-replay evidence remain valid. | Add the original optional session-metadata link. Public permission-renewal presentation remains a B20/B21 dependency; general job resume remains separate B19 work. | B20/B21 permission presentation for the broader management experience |
| B23 | Author SDK/tooling and second-language proof | **Implementation remaining** | Public generic invoke and committed validate/pack commands; [authoring guide](../../../python/src/model_deck/plugins/authoring/README.md) | Deliver independent Python SDK/wheel, init/dev/test workflow and fresh-project walkthrough; commit and prove the JavaScript negotiate/invoke/cancel/disable fixture. Existing untracked protocol-fixture work is not audited delivery. | B18/B20 stable lifecycle; B22 acceptance example |
| B24 | Decide optional restricted execution feasibility | **Complete** | [feasibility decision](../../../docs/architecture/restricted-execution-feasibility.md) | Complete only for the feasibility/ship-scope decision: trusted mode ships and restricted mode is not claimed. A future restricted-mode request would require the currently unsatisfied signed helper/profile, runtime qualification, sibling-file/socket/credential/network/subprocess denial, and descendant-inheritance evidence. | None |
| B25 | Native presenters and platform attachment parity | **Integration remaining** | Extracted model/usage/settings/platform components, [V2 app](../../../macos/Sources/ModelDeckV2/README.md), and relocated native model/Notebook/export/restart/disable acceptance | Complete overview/connections/registration/usage/host-launch public-operation wiring; finish the Codex settings editor; prove all five states, generation/main-actor/geometry/focus/appearance parity and the remaining lifecycle cases; remove legacy orchestration only afterward. | B10, B15, B16, B21 |
| B26 | Immutable packaging, compatibility, docs, aggregate G7 | **Integration remaining** | [legacy-compatible stager](../../../scripts/package/README.md), [V2 builder](../../../scripts/v2/README.md), [catalog](../../architecture/CATALOG.md), and one relocated checkout-independent V2 app with a retained external-runtime contract | Finish the engine/SDK artifacts and aggregate inventory; prove actual signatures/helper identity, no bytecode mutation, V2 schema-version/upgrade/recovery evidence, completed settings editor, behavior-matrix mapping, and implemented G1-G7/all-local gates. V2 remains unsigned and depends on an external Python environment. | B06, B15-B16, B23, B25 |
| B27 | Fresh-install qualification and authorized tool switch | **Blocked** | V2 coding and Notebook are isolated milestone evidence only | After B26, separately authorize exact target/state paths, V2 recovery and tool availability; then qualify fresh installation, real host/AX, routes/billing, streaming/tools/cancel/compaction/restart/plugin update/helper identity and recovery. | B26 plus separate operational authorization |

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
- The 2026-09-14 relocated-artifact milestone built V2 in one fresh scratch
  root, copied it to a second directory, and launched it from `/private/tmp`
  with development Python/engine variables removed. Runtime imports resolved
  the three application packages from the moved app and only the declared
  third-party dependencies from external Python 3.12.13. Native acceptance
  created and renamed a synthetic model, installed/enabled a clean 21-entry
  Notebook archive, created a note and saved two later revisions, reopened the
  selected current revision, and retrieved a completed Markdown export. Normal
  quit reaped the exact app, engine, and plugin PIDs and removed the socket;
  reopening preserved the renamed model, enabled plugin, and revision-3 note.
  Disable removed panels and public invocation, and a controlled re-enable
  showed retained data before leaving the plugin disabled. The builder now
  rejects an invalid interpreter before creating output and retains its exact
  dependency checker/`pyproject.toml` in the app. This advances B04/B05/B25/B26
  but does not close any of their broader original requirements.
- `37378de` and `548b24d` establish a real isolated Codex CLI workflow through
  the V2 loopback bridge and composed OpenAI-compatible provider: tools, project
  mutation, passing test, same-thread continuation, usage, and disconnect-driven
  cancellation. The committed guide records `32,453` input, `509` output, and
  `12,800` cached-input tokens for the final reviewed run. The handoff's
  `41,009 / 575 / 20,480` figures were not found in committed evidence and are
  therefore not substituted. Remote termination remains unconfirmed where the
  terminal is `run.interrupted`.
- B07 repository conformance passed six shared behavior methods against both
  the behavioral fake and a temporary SQLite repository, plus one public-record
  shape guard. The adjacent SQLite repository suites and the active-route
  removal test remain the persistence, transaction/outbox, and admitted-run
  evidence that a nonpersistent fake cannot represent.
- The two reported provider-bootstrap failures were
  `test_injected_provider_receives_admitted_route_tools_and_socket_callbacks`
  and `test_cancel_and_restart_keep_application_recovery_and_caller_ownership`.
  A later shutdown composition had incorrectly closed a caller-owned injected
  `ProviderExecutionPort`; bootstrap now shuts down only the engine-owned
  external extension host. The complete provider-bootstrap and shutdown slice
  passed 13 tests after the repair.
- B15's deterministic integrated fixture crosses the loopback Codex bridge,
  real engine/socket admission, OpenAI-compatible execution, private SQLite
  continuation, a provider-private tool call/result, streamed compaction and a
  successful post-compaction turn. Focused tests separately prove unary
  compaction, failed-summary discard, session/route mismatch refusal before
  HTTP, store reopen, corrupt/missing-state errors, and Cursor SDK callback
  continuation. Engine-authorized model reset retires the old private binding,
  legacy session databases migrate additively, and provider response ordering
  is independent of wall-clock rollback. The combined gate passed 338 tests;
  contracts remained at 200
  synchronized schemas and the architecture scan passed 208 files with zero
  errors or warnings. This is fixture evidence, not a live provider claim.
- Codex Desktop UI, parallel tool calls, live-provider opaque compaction,
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
- From baseline revision `f97401b210b5a5c2d1a56c0e9624ef5c6804c67b`,
  `tests.engine.test_repository_conformance` passed 7 methods; the provider
  bootstrap/shutdown slice passed 13 tests; the OpenAI-compatible provider
  discovery suite passed 197 tests; and the integrated Python command covering
  the original broad suite plus repository, SQLite, and shutdown modules passed
  230 tests. `scripts/architecture_check.py` scanned 213 files with no findings,
  and `scripts/generate_contracts.py --check` confirmed 200 synchronized schemas.

Commands used for this closure:

```sh
PYTHONPATH=python:python/src:. /tmp/md-b18-venv/bin/python -m unittest -v \
  tests.engine.test_repository_conformance
PYTHONPATH=python:python/src:. /tmp/md-b18-venv/bin/python -m unittest -v \
  tests.engine.test_provider_bootstrap tests.engine.test_bootstrap_v2_shutdown
PYTHONPATH=python/src /tmp/md-b18-venv/bin/python -m unittest discover \
  -s python/tests/provider_openai_compatible -p 'test_*.py'
PYTHONPATH=python:python/src:. /tmp/md-b18-venv/bin/python -B -m unittest -v \
  test_model_deck_mcp python.tests.engine.test_mcp_model_reads \
  python.tests.engine.test_mcp_model_writes python.tests.engine.test_provider_bootstrap \
  python.tests.engine.test_bootstrap_v2_shutdown \
  python.tests.engine.test_repository_conformance \
  python.tests.engine.test_sqlite_model_repository \
  python.tests.engine.test_sqlite_connection_repository \
  python.tests.engine.test_cli_v2_provider_bridge \
  python.tests.engine.test_model_projection_invalidation \
  python.tests.engine.test_projection_dependency_expansions \
  python.tests.integrations.hosts.codex.test_projection_consumer \
  python.tests.integrations.hosts.codex.test_projection_snapshots \
  python.tests.integrations.hosts.codex.test_agent_materializer \
  python.tests.integrations.hosts.codex.test_agent_renderer \
  python.tests.integrations.hosts.codex.test_projection_full_path
PYTHONPATH=python/src /tmp/md-b18-venv/bin/python scripts/architecture_check.py
PYTHONPATH=python/src /tmp/md-b18-venv/bin/python scripts/generate_contracts.py --check
```
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

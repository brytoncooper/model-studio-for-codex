# Architecture delivery status

Baseline audited 2026-09-12 through `3551838`; accepted follow-ups are recorded below. This is the master
delivery checklist for the full B00–B27 plan. A component commit does not close
its parent task. The running application has not been replaced or reconfigured.

## How to read and maintain this record

**Written** means implementation exists. **Integrated** means the intended
components are connected in executable code. **Verified** requires relevant
execution evidence, including the acceptance requirements in BACKLOG.md.
Source-audit links list tests but do not imply those tests ran in the audit.

After each accepted commit, update its task row, the evidence below, and the
remaining action. Mark a task complete only when its entire acceptance scope is
proved. Keep incomplete requirements visible; do not derive a percentage from
commit counts or the number of task headings.

## Master checklist

| Task | Current implementation and integration | Remaining requirement |
| --- | --- | --- |
| B00 Isolation | Guards, isolated worktree tooling and staging packager reviewed | Real staged packaging and artifact qualification |
| B01 Contracts | Shared schemas, resources and language clients written; tool contract corrected | Complete cross-language gate at current revision |
| B02 Headless model library | CLI/socket/model read path integrated | Record full current G2 acceptance |
| B03 Architecture enforcement | Runtime classification repaired; parent verified 30 checker tests and full 156-file graph with zero findings | Keep graph gate current as integrations land |
| B04 Swift packaging | Native library targets extracted | Staged executable/assets/helper verification |
| B05 Models client | Typed client/presenter and app model path wired | Complete cross-language UI acceptance |
| B06 MCP | Real entrypoint read composition reviewed; existing registry/formatting/search reused | Converge available mutations; qualify packaged entrypoint |
| B07 Application state | Revisioned SQLite model/connection operations integrated | Shared repository conformance and active-run removal proof |
| B08 Import preview | Deterministic read-only preview written | Record current full preview acceptance |
| B09 Projections | Committed snapshots, invalidation receipts and historical dependency recovery reviewed | Complete fixture composition and conflict resolution |
| B10 Host bridge | Legacy bridge retained; new host helpers written | Extract and compose actual host bridge/launch policy |
| B11 Migration | Preview supports preparation | Offline apply, recovery and rollback rehearsal |
| B12 Sessions/runs | Durable run path, exclusive-lock recovery and injected provider bootstrap reviewed | Complete real-provider and run acceptance gate |
| B13 HTTP providers | Streaming, request helpers and owned HTTP transport reviewed | Provider execution composition, fallback and parity |
| B14 Cursor provider | Coordinator and injected legacy-process adapter independently reviewed | Root SDK composition and parity |
| B15 Continuation | Translation/compaction helpers extracted | Scoped store, both host paths and provider integration |
| B16 Usage/evidence | Durable reconciliation and authenticated public usage query integrated with fixture runs | Real-provider wiring, pricing and refresh jobs |
| B17 Kernel | Generic composed operations integrated with authenticated dispatch and discovery | Migrate static built-ins; external schema/principal lifecycle integration |
| B18 External runtime | Archived standalone provider proven through process channel, proxy and real engine stores | Credential-scope proof and full runtime integration |
| B19 Plugin capabilities | Storage/events/jobs brokers and durable stores written | Serving supervisor integration, operator jobs and explicit resume |
| B20 Package lifecycle | Artifact staging, lifecycle service, SQLite lifecycle transactions and versioned-data contract reviewed | Integrate real activation, versioned data and public lifecycle dispatch |
| B21 Extension UI | Declarative schema, immutable decoder and native renderer reviewed | Public panel delivery, app attachment and extension management |
| B22 Session Notebook | No implementation found | Independently packaged feature and lifecycle proof |
| B23 Author tooling | Generic authenticated CLI invocation reviewed and pushed | SDK, author workflow and JavaScript fixture |
| B24 Restricted execution | Reviewed feasibility document; no restricted runtime implemented | Isolated OS enforcement investigation and qualification |
| B25 Native parity | Catalog path, authenticated settings persistence and native settings screen reviewed | App attachment and remaining native features |
| B26 Distribution/docs | Isolated staging packager and subsystem catalog reviewed | Real staged packaging, compatibility wrappers and aggregate gate |
| B27 Live qualification | Not started | Complete staged readiness, then schedule protected-runtime cutover |

Detailed source audits: [B00–B09](status/B00-B09.md),
[B10–B18](status/B10-B18.md), [B19–B27](status/B19-B27.md).
These retain the inspection revision and distinguish evidence from missing work.

## Recent accepted evidence

| Commit | Accepted component | Verification and limit |
| --- | --- | --- |
| `2ad32a4` | Advertised tool definitions separated from emitted calls | Parent batch: 194 tests, 135 subtests passed across contracts, runs, Cursor, socket fixtures and jobs. Architecture check of affected tool modules: 16 files, zero findings. Socket fixture accepts the definition schema but its route does not support tools; complete socket-to-tool-provider proof remains. |
| `ef74e20` | Authorized durable plugin job followups | Included in the 194-test batch; independent review findings repaired and parent reviewed. Broker is not yet connected to serving supervisor. |
| `a143b51` | Immutable plugin artifact staging | 24 focused tests plus independent cleanup/replacement probes accepted. Failure before filesystem identity is established may leave an empty staging directory; no unknown replacement is deleted. |
| `3551838` | Codex settings-file persistence | Parent: 45 document/file tests, 8 subtests passed. Independent descriptor, backup and failure probes reviewed. Engine bootstrap composition remains in progress. |

All four commits above, plus the initial checklist `193e016`, were confirmed pushed.

Architecture follow-up: first-party plugin runtime has its own layer. Parent
verified 30 checker tests and the complete 156-file product scan with zero errors
or warnings. Core and external plugin/SDK imports of runtime remain forbidden;
new negative fixtures also reject runtime imports of hosts/providers/private core.

Settings composition follow-up: six independently run real socket tests verify
read/preview/save against temporary TOML files and SQLite ledgers, durable preview
and exact save replay across restart, one recoverable backup, stale edit rejection,
authentication and unchanged defaults. Configured operator identity is injected
by bootstrap; this is not per-client role enrollment or live settings discovery.

Startup recovery follow-up: independent acceptance ran 40 recovery, transport and
settings tests. Recovery occurs after the instance lock and before listening;
claimed active runs interrupt without provider retry, accepted/terminal states
remain, and a competing instance cannot mutate recovery state.

Provider transport follow-up: 28 focused lifecycle/channel tests and three
independent subprocess probes passed. One reader handles events and concurrent
replies; strict matching, bounded queues, whole-batch rejection, deadlines and
owned-child cleanup were verified. Run ownership and terminal semantics belong
to the separate proxy review; no real external provider package is qualified yet.

Usage records follow-up: parent ran 20 tests and six subtests after independent
review repairs. Single-query bounds, exact fractional timestamp comparisons,
schema-valid complete responses and optional-field presence/replay were verified.
Records require genuine run/session IDs; legacy ledger IDs and costs are not
invented. Event and public dispatch wiring remain pending.

Native settings follow-up: a fresh independent library stage passed 31 tests,
including control actions through actual Command-S, typed enum values, duplicate
labels, unknown choices and secret metadata exclusion. Parent inspected the
offscreen layout. This proves the reusable screen, not attachment to the running
app or an end-to-end Swift-to-file session.

MCP read composition follow-up: 40 tests and independent shared-account route
probes passed. Validated Deck.call reads traverse the application service while
reusing registry parsing and legacy output formatting/search. Captured account,
URL and wire participate in opaque connection identity. Writes remain legacy.
The Architecture entrypoint requires the engine package in source or staged
vendor; the new packager must supply it, with no silent service bypass.

Provider proxy follow-up: parent ran 19 tests after independent concurrency
review and wire-to-engine translation repair. A real coordinator/SQLite test now
proves waiting-for-tool, result submission, replay without duplicate forwarding,
and one completed terminal. Slow sinks and blocked acknowledgements remain
isolated per run. The archived external-process integration is still underway.

Tool-event integration `9e476f0`: independent acceptance passed 37 focused tests
and a real SQLite/authenticated-socket probe. Internal tool fields remain flat;
only the public notification nests `tool_call`. Submission returns the run to
running, arguments are detached, and invalid payloads do not escape on the wire.
This supersedes the earlier tool-capability limit on the socket fixture; durable
restart replay remains a separate requirement.

Archived provider `ec56ad1` and cancellation repair `74ef7ed`: independent
acceptance passed 11 integration/regression tests, followed by 10 cancellations
and 10 tool-result runs with zero failures. The real ZIP/staging/process pipeline
uses isolated imports and actual engine repositories. The terminal-race repair
preserves the committed winner without duplicate publication; unrelated failures
propagate. Credential brokering and live provider parity remain pending.

Cursor process adapter follow-up: independent acceptance passed 70 focused tests
and composed probes. Existing payload and usage logic remain injected; synchronous
tool-result callbacks, early failures, exactly-once cleanup and explicit false,
zero, empty and null arguments are preserved. Missing arguments fail closed.
Root SDK composition and live provider qualification remain pending.

Projection snapshots `340c8d6` and invalidation `5bca96a`: seven adapter tests plus
independent error-boundary probes passed; 49 transactional and outbox tests passed.
Connection changes advance only their active dependent model revisions in the
same transaction. Replay and tombstones remain protected. Dependency expansion
acknowledgments and the complete file projection loop remain pending.

Dependency receipts `4fec8a3`: 43 independently run tests passed. Exact expansion
proof is committed with connection/model changes; proven metadata is excluded
before pending-batch limits, without claiming a file was applied. Receipt failure
rolls back the save. Historical unexpanded rows remain visible and need recovery.

Historical dependency recovery follow-up: M3 reviewed the transaction, grouping,
replay and conflict behavior; parent independently ran all 29 related tests.
Valid historical events expand against current committed state without reviving
tombstones. Invalid rows retain explicit pending conflicts; no file projection
is claimed by recovery.

Usage reconciliation `64a283d`: 16 reader/reconciliation tests and independent
SQLite integration passed, alongside the previously reviewed ledger tests.
Bounded snapshot reads preserve timestamps and optional fields; partial writes
recover through idempotent reconciliation before queries return. Public socket
composition remains pending.

HTTP transport `e93e2ac`: nine independently run tests verify exact POST inputs,
failure cleanup and once-only concurrent close. The primitive remains unwired;
local close does not claim confirmed remote cancellation.

Kernel composition `300ce44`: 15 final composition tests and independent socket
probes passed after authorization/static regression checks. Unknown namespaced
operations are discoverable and invocable through trusted composition. Seven
invalid/oversized result probes return fixed errors and preserve connection
usability. Static built-ins and external schema/principal lifecycle remain outside
this integration.

Public usage query follow-up: 47 independently run tests and a real socket
timestamp probe passed. Configured fixture runs feed genuine committed events
through reconciliation; authentication, restart/retry, exact optional fields and
fixed error responses are verified. Pricing, refresh jobs and live billing parity
remain separate work.

Lifecycle contracts `280a578`: 11 tests and independent transition review passed.
Abort/rollback retain recoverable restoration claims until external synchronization
completes. This is a port freeze, not storage or crash-recovery implementation proof.

Panel contract `34fbb08`: 11 tests and independent strict offline schema validation
passed. The tree includes labeled inputs and explicit stale-state/binding rules.
Semantic validation, native rendering and public panel transport remain pending.

Cursor preparation `b66afc7`: 23 independently run tests and parity probes passed.
The existing manager reuses a pure payload helper; invalid new prompts preserve
paused sessions. Engine composition and live SDK qualification remain pending.

Staging packager `456a465`: independent review passed 28 tests on macOS Python
3.12, including benign child cleanup and timeout fixtures. Explicit offline
vendor bytes, supplied helper identity, fresh output publication and supported
resource layout are checked. Actual Swift compilation, signatures, resource
lookup without source, and staged application behavior remain unverified.

## Current independent work

- Versioned SQLite plugin data implementation against the accepted contract.
- Public socket asynchronous event-delivery repair; acknowledgements must not
  serve as polling requests for newly published provider events.
- Archived external provider integration through the public engine socket.
- Generic CLI invocation review and boundary repairs.
- Notebook protocol and HTTP provider composition boundary checks.
- Normalized run-options contracts for provider parity; persistence and consumers
  will follow the shared contract review.

SQLite lifecycle repository `755ef2a`: 19 focused repository/port tests passed
in implementation and independent Sol review. Claims, phase revisions, selected
state, restoration intent and exact retry receipts persist atomically. Service
composition, real activation/data effects and cross-process qualification remain
separate requirements; this commit does not close B20.

Extension wire contracts now consistently require revision and retry identity
for all six mutations, and expose revision on get. Independent Sol review passed
38 validation cases and verified canonical/Python/Swift copies match. The panel
schema is also bundled in both languages. These contracts do not establish that
extension commands are wired into public dispatch.

Provider composition `1ba61e9`: bootstrap accepts an injected execution provider
and copied route definitions, sharing the existing durable sessions, runs,
recovery and usage services. Independent repair review passed 43 focused tests;
the author ran 120 regressions. Invalid capability references fail before state
creation, caller mutations cannot alter captured routes, and the caller retains
provider cleanup ownership. Real archived-provider socket integration is next;
live SDK behavior remains unqualified.

Lifecycle coordination `ad2ae1a`: parent ran 45 service/port/SQLite tests after
independent review reproduced and verified both repairs. Abort now persists a
completed freeze with restoration intent, so restart thaws before admission.
Lease-owned execution rejects competing recovery across two repository objects
on the same database before effects. The production lease/activation/data
composition is still required; these tests use explicit temporary databases.

Versioned-data contract `74bf69a`: independent review passed three contract tests.
Bindings capture namespace, data reference and activation generation; storage
must share the broker mutation barrier and explicitly control writable state.
Real generation storage, freeze races and staged migration tests are underway.

Native panel decoding `9031b85`: isolated library compilation and 42 owned tests
passed after independent review and additional boundary probes. Values are
immutable; decoding enforces fields, Unicode scalar lengths, strict Booleans,
node/binding limits and ready-state requirements. Native revision values are
bounded by signed `Int`; the shared revision schema remains unbounded. B01 must
resolve that cross-system numeric limit before claiming exact numeric parity.

Contracts `f053c88` and `35ffb6c`: external invocation now requires supervisor
broker context, panel fetch returns a tree, and run options have shared schema
and Python types. Parent ran all 57 contract tests; all 200 canonical schemas
match both generated bundles. Runtime consumers remain in progress.

Native renderer `f63689b`: independent library-only compilation passed two
renderer tests, 42 decoder tests and two additional state/lookup probes. It uses
trusted operation lookup, emits action intents, rejects stale lookup results,
and disables stale/non-ready controls. Public fetch and native app attachment
remain separate work.

Cross-process lifecycle tests `cec7142`: five tests pass in about one second,
including distinct-key exclusion, pending/settled replay across processes and a
cleanup fault probe. Independent review verified all children are reaped and
temporary resources cleaned even after a worker failure.

Archived-provider socket qualification found a real streaming gap: the server
only drained notifications after client requests, and the fixture used repeated
acks to poll. The integration slice remains unaccepted until delayed events reach
an idle subscribed client without another request. Transport repair is assigned.

The source audit exposed real missing integration and verification work. Earlier
conversation percentages were estimates, not a measured delivery baseline; this
checklist replaces them as the source of status.

## Accepted run-options pipeline

`664fb02`, `b93968e` and `fba4688` add strict options conversion, versioned
SQLite persistence and admission/provider-request propagation. Independent Sol
reviews accepted each slice after malformed-value and recovery fixes. Parent
ran 70 combined codec, admission, persistence, use-case and startup recovery
tests successfully. Omitted options retain the legacy request hash; explicit
false/empty values survive; malformed stored options fail closed. Provider
specific mapping and enforcement remain unfinished; see [remaining parity
work](RUN-OPTIONS-GAP.md). No live application data was migrated.

CLI `bbf52d2` adds authenticated discovery and generic invocation of advertised
operations with bounded input and fixed failure handling. Independent review
passed 32 CLI tests. Plugin SDK, package authoring commands and the JavaScript
fixture remain B23 requirements.

Next / upcoming task: accept the idle-event disconnect repair and versioned
data freeze-ownership repair, then integrate external invocation and brokers.

## Accepted event delivery and data generations

`7be28e3` delivers events to idle subscribed sockets without polling RPCs and
removes both dispatch and replay subscriptions when their connection closes.
Independent review and the parent each passed 63 focused tests, including
partial frames, credit, ownership and real disconnect cleanup. Archived-provider
socket tests are being updated to consume notifications without the former
ack-as-poll workaround.

`150eb43` adds versioned SQLite plugin data. Independent review and the parent
passed 29 adapter, legacy CRUD and contract tests. Active freeze references
fence replay, thaw and staging even when two freezes have the same data revision;
migration incarnations fence callbacks from replaced stages. Lifecycle service
composition with this real store remains to be verified.

Generic external invocation remains unaccepted: final review reproduced hanging
unsolicited broker callbacks without deadline enforcement and oversized response
encoding that abandoned a worker without closing the runtime. Repairs are owned
by the process runtime worker; independent re-review is required.

Next / upcoming task: verify real lifecycle/data composition and repaired broker
deadlines, while plugin validate/pack authoring commands are implemented separately.

## Archived provider socket proof

`7a6e8a8` qualifies four isolated archived-provider scenarios over the real
authenticated engine socket: text, tool result roundtrip, confirmed cancellation
and durable usage query. Independent review and the parent each passed all four
tests. The child runs from an extracted ZIP under isolated Python imports. Event
consumption now waits for pushed notifications and acknowledges only consumed
events. This does not qualify live SDK credentials, full plugin supervision or
the packaged Mac app.

Next / upcoming task: review broker deadline repairs and real lifecycle/data
composition; continue authoring commands and HTTP provider composition planning.

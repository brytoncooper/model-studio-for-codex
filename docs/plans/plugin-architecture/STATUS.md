# Architecture delivery status

Updated 2026-09-12 against implementation through `3551838`. This is the master
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
| B00 Isolation | Guards and isolated worktree tooling written | Supported staged packaging and artifact checks |
| B01 Contracts | Shared schemas, resources and language clients written; tool contract corrected | Complete cross-language gate at current revision |
| B02 Headless model library | CLI/socket/model read path integrated | Record full current G2 acceptance |
| B03 Architecture enforcement | Runtime classification repaired; parent verified 30 checker tests and full 156-file graph with zero findings | Keep graph gate current as integrations land |
| B04 Swift packaging | Native library targets extracted | Staged executable/assets/helper verification |
| B05 Models client | Typed client/presenter and app model path wired | Complete cross-language UI acceptance |
| B06 MCP | Read adapter written | Wire real entrypoint and available mutations |
| B07 Application state | Revisioned SQLite model/connection operations integrated | Shared repository conformance and active-run removal proof |
| B08 Import preview | Deterministic read-only preview written | Record current full preview acceptance |
| B09 Projections | Outbox, conditional files, renderer and consumer written | Compose committed resolvers; handle connection changes |
| B10 Host bridge | Legacy bridge retained; new host helpers written | Extract and compose actual host bridge/launch policy |
| B11 Migration | Preview supports preparation | Offline apply, recovery and rollback rehearsal |
| B12 Sessions/runs | Durable fixture run path and exclusive-lock startup recovery integrated | Complete run/provider gate |
| B13 HTTP providers | Streaming and request helpers written | Actual HTTP execution, fallback and parity |
| B14 Cursor provider | Execution coordinator written and tested | Actual SDK channel binding and parity |
| B15 Continuation | Translation/compaction helpers extracted | Scoped store, both host paths and provider integration |
| B16 Usage/evidence | Genuine engine usage recording/query storage reviewed; legacy implementations retained | Public query/event composition, pricing and refresh jobs |
| B17 Kernel | Generic registry written | Built-in composition and generic engine dispatch |
| B18 External runtime | Manifest/lifecycle/process components and bidirectional provider channel reviewed | Provider proxy acceptance and real external provider proof |
| B19 Plugin capabilities | Storage/events/jobs brokers and durable stores written | Serving supervisor integration, operator jobs and explicit resume |
| B20 Package lifecycle | Archive validation and immutable staging reviewed | Install/enable/update/remove coordinator and transactional pointers |
| B21 Extension UI | Contracts exist | Declarative renderer and extension management |
| B22 Session Notebook | No implementation found | Independently packaged feature and lifecycle proof |
| B23 Author tooling | Protocol contracts exist | SDK, author workflow and JavaScript fixture |
| B24 Restricted execution | No implementation found | Feasibility investigation and qualified OS enforcement |
| B25 Native parity | Catalog path and settings components written; real settings persistence composed through authenticated engine | Native settings screen acceptance/app attachment and remaining native features |
| B26 Distribution/docs | Legacy packaging remains; subsystem catalog written | Supported staged packaging, compatibility wrappers and aggregate gate |
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

## Current independent work

- Native settings rendering, secret editing, keyboard shortcut and layout repair.
- Native settings screen independent acceptance and app attachment planning.
- Bidirectional external provider process channel.
- Engine usage recording from genuine run/session identities.
- Independent usage-recording review and external provider proxy implementation.

The source audit exposed real missing integration and verification work. Earlier
conversation percentages were estimates, not a measured delivery baseline; this
checklist replaces them as the source of status.

# Plan review and corrections

Status: **planning-ready** after independent re-review and canonical corrections. Review covers planning quality only, not implementation proof.

## Lead reconciliation before independent review

- Removed a proposed Codex-shaped public provider API; legacy Responses remains only inside a transitional compatibility adapter.
- Chose JSON-RPC schemas/local transport over a prematurely specified HTTP/OpenAPI service.
- Replaced external native UI factories with declarative panels; no third-party in-process code loading.
- Replaced multi-file CAS “atomic transaction” language with SQLite authority and explicit projection outbox.
- Removed uninstall data deletion; retained data is the default.
- Distinguished broker authorization/process containment from OS sandbox guarantees.
- Made session metadata optional for the example feature; current Swift access to selected host threads is not assumed.
- Kept an opaque credential/host context boundary; no promise of portable subscription authentication.
- Incorporated the user's correction: removed target-specific host/OS designs, their backlog and their research artifacts. Canonical BOUNDARIES.md specifies current and fixture adapters only.
- Kept all operating instructions away from the live app/toolchain. Proposed validation commands are not current commands or executed proof.

## Independent findings and verdict

Reviewed 2026-09-12 by the independent finalizer, using document reads and read-only static checks. No application code, test, build, provider call, credential/config access or runtime action was executed.

### R1 — Blocker: migration snapshot precedes writer quiescence

Evidence: PLAN.md:173 stages/migrates a plugin data copy before stopping admission and draining jobs. A note written after the copy and before drain can disappear when the new pointer is activated. BACKLOG.md:189 does not resolve the ordering.

Smallest correction: stage executable/schema validation first; stop admission, drain or explicitly interrupt jobs and freeze all old-activation storage writes; snapshot the final committed data revision; migrate the copy; validate a non-serving activation; atomically switch the executable/data/grant activation record; then admit work. Failure restores old admission only after new activation tokens are revoked. Add a fixture writing during update and prove the write is included or explicitly rejected. Offline engine schema upgrades also need version/checksum migration bookkeeping, transactional failure handling and rejection of unsupported newer state, not merely the one-time legacy import.

### R2 — Blocker: public/broker surface cannot yet support promised external feature

Evidence: API.md:39–50 catalogs public methods, but API.md:15 requires attachment chunk operations, API.md:52 requires credential enrollment, API.md:103–107 requires broker dispatch, and BACKLOG.md:177–199 requires scoped data/jobs/events and dynamic panel rendering. No operation family or owner is assigned for storage read/write/query, job creation/progress, event publication, attachment transfer, panel/schema discovery, or extension status/grant management. API.md:31 additionally makes panel subscriptions metadata-only; Notebook note bodies require an explicit authorized data-read path. PLAN.md:155 promises compaction as an operation, absent from the catalog.

Smallest correction: add a compact method/port inventory distinguishing public client operations, activation-only broker operations and privileged platform enrollment. Assign schema freeze to B01 and implementations to concrete backlog slices; define panel data fetches and opaque export-handle delivery. Exact field schemas can remain B01 work, but these capabilities cannot emerge silently after that freeze. If a promised method is intentionally deferred, remove the dependent acceptance claim.

### R3 — Blocker: principal establishment and delegated-call authority need a concrete contract

Evidence: API.md:17 negotiates versions; API.md:103 grants plugin tokens; PLAN.md:59 only promises private endpoint access. Neither says how a regular Swift/CLI/MCP connection becomes an authenticated principal or how a plugin is prevented at the API layer from simply claiming that client role. API.md:107 promises caller-scoped plugin-to-plugin authorization without defining propagation when the callee uses its own activation token for a nested broker call.

Smallest correction: specify connection authentication, token audience/role/expiry, trusted-client enrollment owner and default scopes; never trust a caller-provided role. Describe broker-authenticated invocation context and effective delegated authority for nested calls. Preserve the correct limitation: same-user unsandboxed plugins can bypass OS-private resources, so these checks enforce the broker contract, not hostile-code containment. Add negative fixtures for role spoofing, wrong audience, expired/revoked token and delegated privilege amplification.

### R4 — Blocker: external provider proof has no protocol implementation slice

Evidence: PLAN.md:179 promises a separately packaged provider with tool suspension/results. API.md:111 defines only an abstract execution port; plugin.v1 at API.md:103 describes generic invoke/jobs without provider registration/event/tool-result binding. B12 owns an in-process deterministic adapter, B18 an external generic feature fixture, and B22–B23 Notebook/second-language features. None explicitly owns bridging the provider port across plugin.v1 and installing/routing the packaged provider.

Smallest correction: specify a versioned provider contribution descriptor and wire binding (start/event/cancel/tool-result/resume capability), while keeping provider types application-owned. Assign that mapping and the packaged provider to B18 or a named dependent slice, with routing/credential scope tests and shared conformance. Do not collapse provider execution into generic background-job semantics.

### R5 — Required correction: parallel summary violates its own DAG

Evidence: BACKLOG.md:254 permits B06 after B02 although B06 depends on B03. BACKLOG.md:255 permits B16 after B07 although it depends on B12. BACKLOG.md:256 waits for merged interfaces correctly, but B13/B14 both own a “private translation helper”/Codex conversion area and need explicit non-overlapping paths at delegation.

Smallest correction: make the parallel map conditional on every listed prerequisite, freeze a shared conversion seam before B13/B14, and have PARALLEL-EXECUTION.md reference the authoritative DAG rather than create competing dependencies. The numbered slice DAG itself has 28 nodes, all dependencies exist and point backward; no cycle was found.

### Non-blocking observations and resolved research proposals

- Kernel/product separation, consumer-owned ports, composition-only adapter selection, capability tri-state, host-owned tools and continuation scoping are coherent at planning level.
- Trusted executable mode versus restricted OS execution is honestly distinguished. B24 is optional unless restricted-mode claims are made; do not require it for ordinary trusted extension delivery.
- Raw research retains rejected proposals: research/02-api.md:74 and :104 (Responses-shaped SSE/OpenAPI), research/01-kernel.md:34 and research/04-swift.md:138 (native UI), and research/05-providers.md:11 (Codex-shaped provider contract). README.md:12 and PLAN.md:185–196 explicitly supersede them. They are historical inputs, not implementation instructions.
- B01 is intentionally a contract/spike task; this review does not require implementing schemas or researching another target now.

### Static evidence and provisional verdict

Initial static check: all 27 local Markdown links in PLAN, BOUNDARIES, API, BACKLOG, VERIFICATION, README and BRIEF resolve. There were no local anchor references requiring validation. All 28 backlog IDs exist; no missing/forward dependency or cycle. Read-only `git status --short --untracked-files=all` showed only untracked Markdown under docs/plans/plugin-architecture; no tracked runtime diff. These are planning checks, not runtime evidence.

Verdict: **corrections required** for R1–R4; R5 must be reconciled before authorizing parallel execution. Awaiting the lead's canonical corrections and PARALLEL-EXECUTION.md. Finalizer will recheck only affected documents plus the final local-link/DAG/docs-only gate, then record the planning verdict here.


## Independent re-review of canonical corrections

Re-reviewed 2026-09-12 after the lead added API support contracts, parallel work packages and documentation requirements. Initial finding line numbers above identify the pre-correction text; current dispositions follow.

| Finding | Disposition and current evidence |
|---|---|
| R1 | Resolved: PLAN.md:172 specifies engine schema migration bookkeeping and compatible recovery; PLAN.md:176 and BACKLOG B20 freeze/drain writers before the final snapshot, validate a non-serving activation, switch authoritative activation state and revoke stale tokens. The write-race and failed-activation cases are acceptance requirements. |
| R2 | Resolved: API.md:133–153 inventories supporting public/broker methods, data reads, attachments/export, compaction, credentials, discovery and lifecycle/grants with explicit slice owners. Exact schemas remain the stated B01 deliverable. |
| R3 | Resolved: API.md:121–131 defines server-assigned principals, enrollment ownership, instance/audience checks, revocation, opaque invocation context and delegated authority, including the callee-owned storage exception bounded by declared effects. The unsandboxed same-user limitation remains explicit. |
| R4 | Resolved: API.md:155 onward specifies external provider descriptors and start/tool-result/cancel/resume/event/credit mapping. B18 owns the proxy and packaged provider conformance; fixture-harness installation is distinguished from B20's production lifecycle acceptance. |
| R5 | Substantively resolved: BACKLOG.md:258–259 fixes prerequisite ordering; PARALLEL-EXECUTION.md:59 assigns shared Codex conversion to the B10 host owner. B16 final acceptance now waits for B19 and its job capability. Queue B needs the last wording correction noted below. |

DOCUMENTATION.md makes per-subsystem purpose, invariants, contracts and extension recipes an acceptance requirement, assigns local guides to source owners and root/catalog navigation to a separate integrator, and includes editorial review beyond file-existence checks. Every JSON parent and leaf has a documentation delta. Parallel execution respects six writers including documentation writers, plus independent read-only work; shared legacy files retain a single owner. Proposed paths must still become exact checked ownership packets before implementation.

Final static gate used only inline standard-library documentation parsing and read-only git status:

- 36 local Markdown references across the canonical documents, README, BRIEF and REVIEW resolve; no local anchor references occur.
- work-packages.json parses and has 28 parent packages plus 13 leaves, all 41 IDs unique.
- All dependency/aggregation references resolve; combined graphs are acyclic both with and without the optional restricted-mode dependency.
- Every leaf belongs to an existing parent and is included in its acceptance aggregation. Markdown and JSON parent prerequisites agree.
- All 21 changed/untracked files are Markdown or JSON under docs/plans/plugin-architecture. No tracked application changes were present.
- No proposed application command, test, build, migration, provider request or live-runtime operation was executed.

Remaining wording correction: PARALLEL-EXECUTION Queue B lists B16 preparation among candidates after B07; align it with BACKLOG by explicitly requiring B12 before that preparation. This does not invalidate the already-passing JSON DAG. Pending that edit, no other planning blocker remains. Exact schema feasibility, implementation behavior and device/provider qualification remain future evidence, not certified by this review.


## Final verdict

**Planning-ready.** The lead corrected Queue B to require B12 acceptance before B16 cached-query/source preparation and B19 before full refresh-job acceptance; the finalizer read and verified that exact text. R1–R5 are resolved. The eight canonical documents and machine-readable work packages provide a coherent implementation plan, bounded parallel ownership and required human documentation. The static evidence above remains applicable; the final wording edit changes no links or dependency records.

This is approval of the planning artifact, not permission to begin implementation. Wait for the user's explicit implementation-start instruction. No application source, installed artifacts, state, credentials, running process or toolchain configuration was changed by this review; no live-runtime qualification is claimed. Implementation, packaging, installation and provider/device qualification require their separately specified future evidence.

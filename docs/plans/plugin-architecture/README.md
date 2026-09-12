# Model Deck extensible architecture plan

Planning only. No application rebuild, restart, installation, runtime configuration or implementation was performed.

1. [Architecture plan](PLAN.md) — goals, kernel/engine ownership, source layout and migration decisions.
2. [Provider, host and platform boundaries](BOUNDARIES.md) — independence using current adapters, without designing hypothetical ports.
3. [Public API and plugin protocol](API.md) — schemas, operations, streaming, lifecycle and SDK contracts.
4. [Implementation backlog](BACKLOG.md) — 28 dependency-ordered slices, ownership, acceptance and rollback.
5. [Verification strategy](VERIFICATION.md) — enforceable boundaries, conformance, external feature proof and protected cutover.
6. [Parallel execution map](PARALLEL-EXECUTION.md) — ready queues, package ownership, task packets and integration policy.
7. [Documentation contract](DOCUMENTATION.md) — subsystem guides, root README, catalog and extension tutorials.
8. [Independent review](REVIEW.md) — findings, corrections and planning-readiness verdict.

These eight documents are the canonical plan. Raw [research reports](research/) are non-normative proposals from ten parallel Composer 2.5 planning contributors; contradictions are resolved in the canonical documents. Composer 2.5 is the requested implementation-worker model. Fast tier was not exposed by the spawn interface and is unverified.

The user clarified that engine independence is the requirement. Building or designing another named agent host or another operating-system application is outside this plan. A fixture adapter demonstrates the boundary; it does not claim support for a future port.

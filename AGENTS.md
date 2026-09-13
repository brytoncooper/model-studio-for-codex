# Model Deck agent instructions

## Precedence and purpose

This project policy supersedes conflicting global and older planning workflow
rules: no six-writer cap, no sole subsystem finalizer, and no mandatory check
wrapper. Explicit user instructions remain authoritative.

## Independent evolution

Keep systems focused and independently replaceable through explicit public
contracts. Each owns its implementation, data, invariants, tests and docs.
Do not reach into another system's private code or storage. Keep the kernel
small; provider, host, platform and optional features stay behind boundaries.
Preserve existing app behavior. Do not implement other hosts or operating
systems without a request. Avoid abstractions that do not serve a real seam.

## Maximize useful parallelism

Parallel development and review are explicit objectives, across AND within
systems. A system is not automatically a single-agent assignment. Split at
independent interfaces, adapters, test suites and documentation boundaries.
Use available runtime capacity for ready work; no fixed writer cap. Reserve
capacity for review and repair and respect measured machine resource limits.
Do not invent tasks merely to fill slots.

Assign a clear outcome, ownership boundary, contracts, acceptance criteria and
checks. One active writer owns each file or tightly coupled unit. Preserve
others' edits. Workers may investigate and decide details within their scope.
Settle the minimum shared contract, then release dependent work against test
implementations. Integration dependencies need not block implementation.
Name concrete serial blockers; resolve the smallest blocking interface.
Coordinate public contract changes with affected owners before changing them.

## Independent testing and parallel review

Workers may run focused tests, lint and other appropriate isolated checks.
No special wrapper is required. Test observable behavior and meaningful failure
modes, not implementation details solely to increase test counts.
Review coherent slices concurrently with reviewers separate from implementers.
Review behavior, boundaries, maintainability, tests and documentation. Report
concrete findings; avoid speculative scope expansion. Consolidate persistent
failures with one capable owner instead of repeated repair handoffs.

Coordinate expensive builds, full suites and shared resources to prevent
competing duplicate runs. Isolate temporary data, sockets and output paths.
One integration owner coordinates aggregate checks per integration batch;
subsystem acceptance does not have to queue behind that owner. Rerun only
checks invalidated by changes or unresolved concerns. Local passes do not prove
full parity: maintain and verify cross-system behavior coverage.

## Delivery and context

Use fresh worker contexts with this policy and a concise assignment; avoid
inheriting long conversation histories or obsolete process instructions.
Use MiniMax-M3 for implementation subagents. GPT-5.6 Sol may be used when its
judgment is useful. Do not use Muse or Cursor models. If the MiniMax-M3 agent
route is unavailable, report that accurately and use permitted Sol workers for
appropriate bounded work.
No new synthetic comparisons unless requested.

The lead owns cross-system decisions and final integration review. Coordinate
shared-checkout Git operations through one owner. Commit and push accepted,
coherent pieces incrementally as authorized; exclude unrelated work.
No merge, installation or live cutover without applicable authorization.
Report verified milestones, pushed commits and concrete blockers, not guessed
completion percentages. Optimize for accepted delivery, not agent count.

## Protect the working environment

Never rebuild, replace, kill, restart or reconfigure the running Model Deck,
Codex, router or Cursor providing this session's tools. Use the Architecture
worktree for implementation and explicit isolated fixtures/output paths.
Do not mutate live credentials, host settings or application data. Staged
builds must not touch the installed app. Live qualification is separate.
Preserve the saved backup ref and unrelated work. Snapshot dirty files before
bulk transformations and independently verify the result.

## Documentation

Every system/subsystem has a concise Markdown guide: purpose, ownership,
contracts, invariants, extension instructions, tests and limitations.
Keep a catalog linked from the root README, explain why Model Deck exists,
and document external plugin authoring. Describe actual guarantees and label
unfinished work honestly.

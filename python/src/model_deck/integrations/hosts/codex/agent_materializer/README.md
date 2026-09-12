# Codex agent materializer

Actual `CodexProjectionMaterializer` adapter: turns one supported
`registered_model.upserted` outbox event into the owned agent-file write.

## Ownership

This package owns `resolution.py`, `materializer.py`, and its test only.
It never edits the consumer, renderer, engine ports, or storage adapters.

## Contracts

Resolution port (`resolution.py`) is caller-implemented: `ConnectionSnapshot`
and `ModelSnapshot` return frozen `ResolvedConnection` / `ResolvedModel`
records, or `None` when unknown. The root resolver maps sqlite connection
and model rows plus settings (endpoint decoding, billing text, subscription
mapping) onto these records. Settings carry only explicit values: a relative
`agents_rel_dir` and an absolute `token_helper_path`. No home discovery,
filesystem reads, credential reads, or legacy runtime imports happen here.

`AgentMaterializer.materialize` validates event identity (registration id
and revision equal the outbox envelope), snapshot identity (payload fields
equal the model snapshot; model connection equals the looked-up connection),
and revision freshness, then builds the accepted renderer `RenderRequest`
and reuses the real `render_managed_agent`. Output path is
`agents_rel_dir` joined with the renderer filename, kept relative and
traversal-free, so registration identity and collision handling stay with
the consumer and legacy IDs/content bytes are preserved.

## Invariants

Fail closed: missing snapshots, stale revisions, identity mismatches,
invalid settings, unsafe renderer output, renderer `RenderError`, and any
other resolver/renderer failure all raise fixed-message
`CodexProjectionMaterializationError` values (raised `from None`, so no
resolver/renderer text or traceback chains into the error).
`KeyboardInterrupt`/`SystemExit` propagate. The consumer records the fixed
error as a `materialization_invalid` conflict and never persists content.

## How to extend

Add new renderer inputs by extending the snapshot records and the
request-building branch; keep the lookup protocols read-only and the
materializer free of I/O. Subscription support is selected by
`ResolvedConnection.kind == "subscription"`.

## Limits

Connection revisions are carried on snapshots but not cross-checked
against the event envelope, which carries only the model revision. Same-path
collisions across registrations are decided by the consumer: its journal tracks
per-event intent, and its conditional file writer rejects unexpected existing
content (surfaced as a `file_unexpected_existing` conflict). There is no
cross-registration path reservation in this package.

## Tests

`test_agent_materializer.py` uses the real renderer plus fake snapshots:
byte-identical endpoint output, subscription output, and closed failures
for missing/stale/wrong snapshots, envelope mismatch, and bad settings.
Run from the Architecture worktree root with the vendor dir on the path
(see the renderer README); rendering requires tomlkit 0.13.3 per build.sh.

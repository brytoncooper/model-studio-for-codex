# Plugin jobs (B19 public-job slice)

The application owns durable job state through `engine/jobs/ports.py` and the
SQLite adapter in `adapters/storage/sqlite_plugin_jobs.py`. The serving external
host composes the authenticated worker broker and the public `engine.v1.jobs.get`
and `engine.v1.jobs.cancel` use cases against the same repository.

Jobs move from queued to running and then to exactly one of completed, failed,
cancelled, or interrupted. Progress is monotonic and bounded. Public cancellation
only records intent and returns whether that request was accepted; it does not
claim that the worker has stopped. A worker confirms cancellation when its next
`jobs.check_cancelled` poll atomically reaches `cancelled`. Losing an activation
marks its still-active jobs interrupted. Neither startup nor worker recovery
automatically replays a job.

Public cancellation idempotency is durable per originating principal. The first
`(principal, idempotency_key)` binds to one job and acknowledgement in the same
transaction as cancel intent. Exact replays return that stored result, including
after terminalization; reuse for another job is a conflict.

Creation persists the originating application principal as well as the owning
plugin and activation. Public reads and cancellation require that exact
principal, while worker mutations require the exact plugin activation and a
fresh authority check. Rows created before origin capture are not publicly
readable.

`jobs.complete` may atomically persist one application-bounded JSON result.
`jobs.get` exposes it only after completion and preserves the distinction between
an absent result and explicit JSON null. Results are ordinary local data: plugins
cannot choose filesystem paths, and this slice grants no filesystem or attachment
authority.

Checkpoints remain strict JSON bounded to 1 MiB and revision-CAS validated against
the schema declared at creation. They are retained on interruption, but explicit
resume and a runner that consumes them remain future B19 work. Events,
subscriptions, content grants, and full lifecycle/update acceptance also remain
outside this public-job slice.

## First-party (engine-owned) jobs

The engine also runs jobs of its own — today the evidence refreshes behind
`prices.refresh` and `benchmarks.refresh`. There is still no public job-create
operation: a first-party job exists because an engine operation started one.

They reuse everything above. `first_party.py` creates them under a reserved
`JobOwner` — `plugin_id` in the `com.modeldeck.engine.` namespace, the engine's
boot identity as `activation_id` — with the engine itself as
`origin_principal_id`. The broker refuses any external activation whose
`plugin_id` claims that namespace, so a plugin can never have the engine act on
its behalf. Because the engine is the originator, any authenticated engine
client may observe and cancel these jobs; one consequence is that a
`jobs.cancel` idempotency key for a first-party job is scoped to the engine
rather than to the calling client.

`runner.py` is the minimum needed to execute one inside the engine process: a
daemon thread per job, a cancellation probe that re-reads `cancel_requested`
from durable state rather than from memory, and exactly one terminal write per
job (a lost race is a result, not an error). It is not the plugin worker and it
resumes nothing. The runner dies with the process, so startup calls
`recover_first_party_jobs`, which marks whatever is still active as
interrupted — never replayed, matching the plugin path.

An engine operation that starts one maps its own `idempotency_key` to a job for
as long as that job is not terminal. That mapping is process-local by design:
after a restart every earlier first-party job is already terminal, so the same
key correctly starts fresh work instead of pointing at a job that will never
finish.

Both in-memory maps are bounded, because those keys are caller-chosen and are
usually a fresh UUID per call. The key map sweeps entries whose jobs are
terminal once it fills, then caps itself, so a flood of unique keys costs
de-duplication rather than memory; the runner releases a job's thread as soon
as that job's terminal write lands, so it only ever tracks jobs still running.

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

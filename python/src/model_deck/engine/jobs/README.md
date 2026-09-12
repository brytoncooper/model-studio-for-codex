# Plugin jobs state slice (B19, state only)

Durable job STATE repository: `engine/jobs/ports.py` contract plus
`adapters/storage/sqlite_plugin_jobs.py` SQLite implementation.

Scope: queued, running, completed, failed, cancelled, interrupted states;
atomic queued-to-running claim; monotonic bounded progress; terminal-once
outcomes; cancel request recorded separately from confirmed cancel; worker
crash marks queued and running jobs for the matching activation interrupted
once, including resumable jobs with retained checkpoints, with no auto rerun.
Checkpoints store exact strict JSON (objects with string keys, arrays, strings,
numbers, booleans, null only; no tuples, no NaN or infinity) bounded to 1 MiB,
validated only against the declared schema reference through a required injected
validator that rejects unknown schemas, with revision CAS. A checkpoint command
schema reference must match the declared schema, never replace it. Failures
persist a safe code from the domain_error_code enum only, never raw error content.
Mutations require exact plugin and activation ownership.

Pending outside this slice: public dispatch wiring and jobs schema surface
(root adds `interrupted` to the public jobs schema separately), operator
service, and the future explicit resume adapter that consumes retained
checkpoints. This slice grants no general resume permission.

# Plugin jobs (B19 public-job slice)

The application owns durable job state through `engine/jobs/ports.py` and the
SQLite adapter in `adapters/storage/sqlite_plugin_jobs.py`. The serving external
host composes the authenticated worker broker and the public
`engine.v1.jobs.get`, `engine.v1.jobs.cancel` and `engine.v1.jobs.resume` use
cases against the same repository.

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
the schema declared at creation. They are retained on interruption. Events,
subscriptions, content grants, and full lifecycle/update acceptance remain
outside this public-job slice.

## Explicit resume

An interrupted job runs again only because someone asked for it by id. Nothing
in startup, worker recovery, or the runner resumes anything, and there is no
automatic replay anywhere.

### What makes a job resumable

`JobRecord.resumable` is persisted at create time from the contributing
operation's manifest flag (`resumable: true`), threaded through
`CreateJobCommand.resumable`. Composition supplies that flag to
`PluginJobBroker(resumable_operations=...)`, a lookup from the trusted
`operation_id` to a bool; with no lookup composed nothing is resumable. A
worker cannot make its own job resumable by asking, and a job created before
the flag existed stays non-resumable.

A lookup that is composed but raises fails the create. Answering "not
resumable" there would write a permanent lie into the row — the job would
report `resume_unavailable` for life, indistinguishable from an operation
that really never declared the flag — where a failed create is simply
retried.

### Saving state: `plugin.v1.broker.jobs.checkpoint`

The sixth broker method. The worker passes `job_id`, the `checkpoint` value,
the `expected_revision` it believes is stored (`null` and `0` both mean
"nothing saved yet"), and an optional `schema_id`. It is a compare-and-swap: a
mismatched revision stores nothing and conflicts, so a worker that lost a race
never overwrites newer state. The job must be active and owned by the calling
activation, and the value must pass bounded-JSON validation and the 1 MiB
encoded cap. The result is the revision now stored, which the worker passes
back as its next `expected_revision`.

`schema_id` is optional, exactly as the published params schema says. Omitting
it keeps the schema the job declared at create, and storage still validates
the checkpoint against that declaration, so an unlabelled save is never an
unvalidated one. What is rejected is naming a schema that is not the declared
one, and introducing a schema on a job that declared none.

The `checkpoint` grant may be omitted from the broker's grants dict, in which
case it inherits the `progress` grant: the same write authority on the same
job.

### Resuming: `engine.v1.jobs.resume`

`ResumeJobUseCase` takes `{job_id, idempotency_key}` and answers
`{accepted, job_id, resumed_from_revision}`, where the revision is `null` when
the job was interrupted before saving any checkpoint. In order:

1. the job must exist — otherwise `not_found`, which is also how dispatch
   learns to try the next job directory;
2. the caller must be the principal that originated the job —
   `capability_denied`;
3. a settled `(principal, idempotency_key)` replays its original answer, and
   the same key against a different job is a `conflict`;
4. the job must be resumable and INTERRUPTED — `resume_unavailable`; except
   that an already-resumed job still working is a `conflict`, because a second
   resume would run the same work twice;
5. the owning plugin must be serving right now — `plugin_unavailable`, a
   temporary answer that leaves the job interrupted and the key unspent.

Then, atomically, `begin_resume` returns the row to RUNNING with
`resume_count + 1`, keeps the owning `plugin_id`, progress and checkpoint
exactly as they are, and rebinds `activation_id` and `invocation_id` to the
live ones. Rebinding is not optional: worker follow-ups re-authorize against
the row's `invocation_id`, and the interrupted run's authority belongs to an
activation that no longer exists. Only after that commit is the plugin
invoked.

### How the plugin receives a resume

A resumable operation `"<op>"` must also implement the sibling invocation
`"<op>.resume"` (`ports.resume_operation_id`). It is not a second contributed
operation: it is never listed, never publicly invocable, and only the
supervisor ever calls it, over the same invocation channel as `"<op>"`, with
input

```json
{"job_id": "...", "checkpoint_revision": 1, "checkpoint_schema_id": "ckpt.v1", "checkpoint": {}}
```

`checkpoint` is the decoded last checkpoint, re-validated on the way out, and
is `null` together with `checkpoint_revision` when none was saved. The plugin
continues from it and reports progress, checkpoints, completion or failure
through the ordinary broker path — the job is already RUNNING and bound to the
live activation before the call arrives.

The transport itself is not in the engine. `JobResumeInvoker` names the seam:
`prepare(request)` resolves the plugin's current serving activation and
captures a fresh invocation authority for `"<op>.resume"`, returning a
`ResumeTarget(activation_id, invocation_id)` or None when the plugin is not
serving; `invoke(request, target)` runs it.

An invocation that fails after the commit is taken back by hand:
`rollback_resume` returns the row to INTERRUPTED, decrements `resume_count`,
and deletes the receipt, in one transaction, before the caller is told
`plugin_unavailable`. Nothing else would undo it — no activation died, so
worker-loss recovery never fires — and the job would otherwise be RUNNING
with nobody on it forever, replaying the failed key as a false success and
conflicting on every fresh one. A job that is no longer RUNNING by then is
left alone: the worker evidently did receive the invocation, and its outcome
outranks the transport error.

### Known limits

- A worker that received the resume but whose reply was lost keeps running
  against a row that is INTERRUPTED again. Its progress and checkpoint calls
  are then refused as conflicts, so it corrupts nothing and simply fails, but
  it does keep working until it notices.
- If `rollback_resume` itself fails, the job really is wedged in RUNNING. The
  error is raised rather than swallowed, so it is visible, but nothing
  recovers it automatically.
- Two resumes of the same job with the same key racing in parallel can both
  reach the invoker, because the replay check reads before the transition
  commits. Serial repeats — what a client actually does — resume exactly once.
- Nothing resumes across an engine restart yet: the invocation authority a
  resume captures lives in supervisor memory.

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

**First-party jobs are not resumable in this wave.** They are created without
the resumable flag, so `engine.v1.jobs.resume` on one answers
`resume_unavailable`; `FirstPartyJobDirectory.job_resume` reaches that through
the ordinary use case rather than by a special case, and still reports an id it
does not own as `not_found` so dispatch keeps probing the next directory. There
is nothing to hand a checkpoint back to: the work lived in this process and
died with it, and the way to run it again is to ask for a new refresh.

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

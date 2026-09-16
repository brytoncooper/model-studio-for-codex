# Job Recovery fixture

A deterministic, stdlib-only packaged plugin that exists to give the job
**resume convention** something real to drive against. It has no engine
dependency: `tests/test_job_recovery_fixture.py` speaks `plugin.py`'s raw
stdin/stdout wire directly (hello/activate/invoke/heartbeat/drain/
deactivate), and answers the plugin's own `plugin.v1.broker.jobs.*` calls
with a small in-process fake broker. This mirrors
`examples/session-notebook/` structurally (same frame codec, same
`ProtocolChannel` request/response correlation, same isolated-imports
guard) but is deliberately narrower: four operations, no panels, no note
CRUD.

`scripts/verify.py`'s extensions gate discovers this fixture automatically
(`example_test_targets()` walks every `examples/*/tests/test_*.py`) — it
does not need to be added to any list by hand.

## Operations

Three operations are **contributed** (listed in `manifest.json`'s
`contributes.operations`, and therefore in a real host's
`engine.v1.operations.list`):

| Operation | Effect | Resumable | Input | Output (invoke ack) |
|---|---|---|---|---|
| `org.example.job-recovery.count.start` | write | yes | `{"steps": N}` | `{"job_id", "state": "running"}` |
| `org.example.job-recovery.stream.start` | write | no | `{"steps": N}` | `{"job_id", "state": "running"}` |
| `org.example.job-recovery.control` | write | no | `{"mode", "after_step"?}` | `{"applied": true}` |

`org.example.job-recovery.count.start.resume` is **not** in that list, and
never will be -- see "The resume convention" below for why and for its
input/output shape.

`count.start` and `stream.start` are async: the invoke call returns
immediately with `{"job_id", "state": "running"}` (the host reads `job_id`
generically at the invoke result's top level, per
`contracts/plugin.v1/lifecycle/invoke.result.schema.json`), and a
background thread performs the counting so the main request loop stays
free to answer heartbeats while a job runs.

Both `count.start` and `count.start.resume` finish through the exact same
internal completion path, so both call `plugin.v1.broker.jobs.complete`
with the **same output shape**:

```json
{"steps_performed_by_this_activation": <int>, "final": <int>}
```

That shared shape is the point: a real host (or this fixture's own test)
can sum `steps_performed_by_this_activation` across every activation of
one `job_id` and compare it to `final` to notice duplicate or missing
work directly, without any special-casing for which activation did which
part.

`stream.start` never calls `plugin.v1.broker.jobs.checkpoint` and its
`plugin.v1.broker.jobs.create` call omits `checkpoint_schema_id` — there
is nothing to resume.

## The resume convention

`count.start`'s manifest entry sets `"resumable": true` and its
`jobs.create` call declares
`"checkpoint_schema_id": "schemas/count.checkpoint.schema.json"`. After
every step it calls `plugin.v1.broker.jobs.checkpoint` with:

```json
{"job_id": "...", "checkpoint": {"next": <int>, "target": <int>}, "expected_revision": <int-or-null>, "schema_id": "schemas/count.checkpoint.schema.json"}
```

`expected_revision` is `null` on the very first checkpoint (nothing saved
yet) and the previous call's returned `revision` on every call after
that — a real compare-and-swap chain, not just a counter this fixture
invents on its own.

`schema_id` is required here, not optional: the engine's job broker refuses
a checkpoint that omits it when the job declared a checkpoint schema at
create, rather than store a payload it would have to keep unvalidated.
Omitting it fails the checkpoint, which this fixture turns into a failed
job — caught by `tests.engine.test_plugin_recovery_workflow` when this
fixture was first driven through the real engine.

`count.start.resume` is the convention itself, and the convention is that
it is **never a contributed operation**: `manifest.json` does not list it
under `contributes.operations`, it never appears in
`engine.v1.operations.list`, and no authenticated principal can reach it
through the public `engine.v1.operations.invoke` channel. Per the
`RESUME_OPERATION_SUFFIX` docstring in `engine/jobs/ports.py` (U0), a
"<operation>.resume" invoke target is only ever called directly by the
supervisor -- the one place that has already checked the caller owns the
job, that the job is `INTERRUPTED`, and that it is `resumable`. This
fixture's own `PackageContractTests.test_manifest_schemas_and_isolated_
worker_are_valid` asserts no operation id ending in `.resume` appears in
`manifest.json`'s declared operations or in the packed-and-validated
archive's operation list, precisely so a plugin author copying this
fixture does not also copy an accidental public entry point into a
supervisor-only ladder. `count.start.resume`'s input and output schemas
still ship in the archive under `schemas/` (this fixture's own tests
validate them directly) -- they are just not wired to a manifest
operation entry, because there is no public operation for them to
describe.

A host that reads a checkpoint back (via the repository's
`read_checkpoint` seam from U0/U4) invokes this operation directly --
never through `operations.invoke` -- with exactly:

```json
{
  "job_id": "<the interrupted job's id>",
  "checkpoint_revision": <the revision read_checkpoint returned>,
  "checkpoint_schema_id": "schemas/count.checkpoint.schema.json",
  "checkpoint": {"next": <int>, "target": <int>}
}
```

No `jobs.create` call happens on resume — the job already exists. Counting
continues from `checkpoint["next"]` to `checkpoint["target"]`, performing
**only the remaining steps**.

### Why the checkpoint carries `target`, not just `next`

The task text that specified this fixture describes the checkpoint as
`{"next": k}`. This fixture's checkpoint schema
(`schemas/count.checkpoint.schema.json`) also carries `"target"` (the
original `N`): resume's params are fixed to exactly four fields with no
room for the original `count.start` params, so the checkpoint has to be
self-sufficient for a resumed activation to know when to stop. This is a
deliberate, documented choice, not an oversight — flagged here because
U4 was handed the identical task text and may have made a different call
about what the checkpoint payload contains. If U4's checkpoint shape
differs, the fix is entirely in this fixture's schema and plugin.py; the
frozen `plugin.v1.broker.jobs.checkpoint` wire contract (owned by U0)
treats `checkpoint` as opaque JSON either way.

## Control (test-only fault injection)

`org.example.job-recovery.control` takes `{"mode": "normal"|"crash"|"hang",
"after_step"?: <int>}` and is **only** meaningful for this fixture's own
tests — it is not part of the resume convention and a real host has no
reason to call it. It is stored in memory for the current activation only
and is reset to `{"mode": "normal", "after_step": null}` on every
`plugin.v1.activate`.

`after_step` is the absolute step number (1-based, same numbering the
checkpoint uses) of whichever counting run is currently active
(`count.start`, its resume, or `stream.start`). After that step's work is
done — and, for a checkpointed run, after that step's checkpoint call has
already returned — the fixture applies the fault:

- **`crash`**: calls `os._exit(1)` immediately. The process disappears
  with no further checkpoint and no `jobs.complete`/`jobs.fail`. Because
  the crash fires *after* that step's checkpoint is durably recorded, a
  resume from the last checkpoint has no gap and no re-done step for this
  fixture's own happy path — a test that wants to demonstrate duplicate
  effects instead resumes deliberately from a checkpoint it captured
  earlier than the true last one (see
  `test_resume_continues_from_checkpoint_with_the_same_output_shape`),
  since the shared output shape is what makes that observable, not any
  cleverness in the crash timing itself.
- **`hang`**: the process stays alive but stops answering *any* further
  frame, including `plugin.v1.heartbeat` — the main request loop parks on
  a never-set `threading.Event` the moment hang fires. The counting
  thread stops too (no further steps, no completion, no failure).

## A shutdown gotcha this fixture's test had to work around

A worker that has already returned `{"deactivated": true}` should not be
left to exit on its own by waiting on the child process: its daemon
stdin-reader thread can still be blocked inside a `readline()` call when
CPython starts finalizing the interpreter, and CPython treats a daemon
thread holding the stdin buffer lock at that point as fatal
(`SIGABRT`/exit code `-6`, message `_enter_buffered_busy: could not
acquire lock ... at interpreter shutdown`) rather than as something to
wait out. This is not a bug introduced by this fixture; the exact same
`ProtocolChannel` reader-thread shape is copied from
`examples/session-notebook/plugin.py`, and the real engine's
`ProcessRuntime._stop` already accounts for it by unconditionally calling
`proc.terminate()` (escalating to `kill()`) after a worker is done, rather
than trusting a clean exit. This fixture's test harness (`_Session.close`)
does the same. Anyone driving a plugin process directly (outside
`ProcessRuntime`) should terminate it after deactivate, not just `wait()`.

## What is deliberately not here

- **No `jobs.progress` / `jobs.check_cancelled` usage.** Not required by
  this fixture's spec; omitted to keep `plugin.py` focused on the resume
  convention rather than reproducing every broker method notebook uses.
- **No "resume before any checkpoint was ever saved" path.** This
  fixture's resume input always requires a real `checkpoint` object; the
  `expected_revision: null` / "interrupted before first checkpoint" case
  from `contracts/engine.v1/methods/jobs.resume.result.schema.json`
  (`resumed_from_revision: null`) is a host/engine-level concern for
  U4, not something this fixture's plugin needs to special-case.
- **`plugin.py` is ~660 lines**, over the ~400-line guideline given for
  this fixture. The four operations plus the full frame codec
  (duplicate-key/non-finite rejection, depth/node budget, `ProtocolChannel`
  correlation) copied faithfully from the notebook model, plus the
  crash/hang fault injection, did not compress further without either
  cutting required behavior or the wire hardening every other fixture in
  this repo relies on. Documented here rather than silently accepted.

## Running the tests

```
cd examples/job-recovery-fixture
PYTHONPATH=../../python/src python3 -m unittest tests.test_job_recovery_fixture -v
```

`PackageContractTests` packs the project with `pack_project_archive`,
validates the resulting archive with `validate_project_archive`, and
checks every schema file is valid Draft 2020-12 and accepts a
representative payload. `JobRecoveryFixtureTests` drives the live
subprocess through every scenario described above.

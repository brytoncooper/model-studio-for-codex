# External extension host

`ExternalExtensionHost` is composed by the outer bootstrap through
`HostDependencies`, keeping platform and storage adapters outside this layer.
It owns private SQLite lifecycle, catalog, authority, job, data, and
invocation-idempotency records plus an immutable digest-addressed artifact
store.

It inspects, installs, updates, enables, disables, removes, and invokes packed
Python extensions. Discovery resolves the lifecycle-selected artifact. Broker
methods and invocation authority derive only from selected `approved_scopes`;
manifest permissions alone never grant access. Updates retain only the
intersection of old approved and newly requested scopes. Disable/remove retain
data and artifact records.

## Startup

Startup recovers pending lifecycle operations under the exclusive lease before
re-admitting settled enabled records. It sweeps first: every catalogued record's
prior activation has its queued and running plugin jobs interrupted, because a
previous engine process can only have ended while they were open and nothing
will ever report on them again. A reopened engine therefore never shows a plugin
job as running from a process that is gone. Only then does it revoke each
durable identity from the prior epoch, so recovery can register a fresh
non-serving candidate without reviving an old token. `RESOLUTION_REQUIRED`
remains pending and non-serving. Pending invocations and jobs are never replayed
automatically; data conflicts require explicit resolution.

The sweep reaches the prior activation of each record's *current* selection,
which is what `ActivationAuthorityController.identity_for` can resolve. Jobs
owned by an activation of a selection that has since been replaced (an update
between processes) are not reached; closing that gap needs a
`list_active_for_plugin` on the jobs repository, which does not exist.

## Plugin jobs and explicit resume

The per-activation broker allowlist covers six job methods — `create`,
`progress`, `checkpoint`, `complete`, `fail`, `check_cancelled` — each granted
`write` / `jobs.own` / `jobs.own` when the selected scopes include `jobs.own`.
A job is created resumable only when the manifest operation that created it
declared `resumable: true`; the host answers that question from the operation
catalog, so a plugin cannot make its own jobs resumable over the wire. A job
created with a declared checkpoint schema must name that schema on every
`checkpoint` call — the engine refuses an unlabelled save rather than store it
unvalidated.

`job_resume` completes the `job_get` / `job_cancel` trio that `EngineDispatch`
looks for, and is backed by `ResumeJobUseCase` over a host-side
`_ResumeInvoker`. The invoker resolves the activation serving *now* and
captures a fresh invocation authority for `"<operation>.resume"` before the
durable row moves, because the repository rebinds the job to both; the resumed
worker then authorizes against the live activation instead of the dead one.
Nothing resumes automatically — a resume happens only because a caller asked
for that job by id, and an interrupted job left alone stays interrupted.

That authority is live from the moment `prepare` returns, while the durable
row has not moved yet, so the window between `prepare` and `invoke` has to give
it back on every way out. `begin_resume` raising is the ordinary way in: a lost
idempotency race, or a job that turned out not to be resumable. `job_resume`
therefore brackets the whole use case — `begin_request` before, `end_request`
in a `finally` — and anything prepared but never invoked is abandoned: the
captured context is dropped, which permanently denies the handle that named it,
and the `"<operation>.resume"` operation authority is removed unless another
resume still needs it. `_ResumeInvoker.abandon(request)` is the same undo by
hand, for a caller that knows a prepared resume will not happen.

Known limits, inherited from the use case: if the invocation fails *after* the
durable transition commits, the job stays RUNNING with no worker until
worker-loss recovery interrupts it again, the idempotency key it used is spent,
and its invocation authority deliberately stays installed — the row is bound to
it, and a worker that did receive the invocation must still be able to report.
`ResumeJobUseCase` itself does not call `abandon`; the bracket in `job_resume`
is what covers it, so a different composition of that use case would leak the
prepared authority again.

## Worker supervision

A crashed or hung child is settled by the activation lifecycle and then handed
to an `ActivationRestartSupervisor` the host owns. `restart_policy` on the
constructor tunes it; the default is three attempts with 0.5s backoff doubling
to a ceiling of 8s, forgiven after 60s healthy. `heartbeat`
(`WorkerHeartbeatSettings`) tunes how each worker is proved alive — interval
5s, timeout 2s, three missed beats by default — and is what a test shortens to
watch a hung worker be declared unresponsive in about a second. A replacement runs the ordinary
validate/admit path, so it gets a fresh activation identity and a fresh
revocation generation, and the dead worker cannot complete its jobs with the old
one. Interrupted jobs are never resumed by a restart.

`supervision_report(extension_id)` and `supervision_reports()` expose worker
health state, restart attempts, last failure code, and `gave_up`. That data does
**not** ride on `engine.v1.extensions.get`: its result schema is a closed object
with `extension_id`, `status`, `version`, and `revision` only, and no free-form
status or diagnostics member to put it in. Surfacing it to a UI needs a contract
addition.

## Shutdown

`close()` returns a `ShutdownReport` with one `ExtensionShutdownEntry` per
catalogued extension: `quiesced` when a live child was drained and reaped,
`settled` when the worker had already died and only its jobs needed
interrupting, `failed` with the exception type when quiesce raised. Each record
is isolated, so one failure never skips the rest, and the instance lock is
released either way. The report is also retained on the host as
`last_shutdown_report`, so the engine's shutdown callback keeps it without this
layer choosing a logging story. `close()` is idempotent and returns the same
report on a second call.

Focused verification:

```sh
PYTHONPATH=src python -B -m pytest -q tests/plugins/test_external_extension_host.py
PYTHONPATH=src python -B -m unittest tests.plugins.test_worker_loss_supervision
PYTHONPATH=src python -B -m unittest tests.plugins.test_worker_restart_backoff
PYTHONPATH=src python -B -m unittest tests.engine.test_external_extension_transport
PYTHONPATH=src python -B -m unittest tests.engine.test_plugin_recovery_workflow
```

`tests.engine.test_plugin_recovery_workflow` is the end-to-end proof of the
whole recovery workflow — crash, detect, settle, restart, explicit resume, hung
worker, exhausted restart budget, shutdown and reopen — through real child
processes and public engine operations only. Budget 45s for it.

The current boundary supports local Python process entrypoints. It does not
provide a permission-renewal/presentation UI, automatic replay of interrupted
plugin work, or non-process runtimes. `external_host/__init__.py` is owned
elsewhere and does not re-export `ShutdownReport`, `ExtensionShutdownEntry`, or
the shutdown/child constants; import them from `.host`.

# Process extension activation lifecycle

`ProcessExtensionActivationLifecycle` is the composition-owned implementation of
the engine's `ExtensionActivationLifecycle` port. It owns exact `ProcessRuntime`
children, their `LifecycleSession`, validation freezes, serving lookup, and
cleanup of jobs owned by each authenticated activation.

The constructor receives three local boundaries. `ArtifactLaunchResolver`
resolves an exact immutable `ExecutableArtifact` to `ResolvedArtifactLaunch`.
`ActivationAuthorityController` creates a non-serving authority record, revokes
it under the shared data mutation barrier, admits it only after data selection,
and resolves the last identity for restart-safe job cleanup.
`ActivationBrokerFactory` binds that trusted identity and its
`PluginDataBinding` to the runtime callback. These objects are supervisor-owned;
the worker cannot supply or replace them.

The job repository is gated on `ActivationJobSettlement`, a narrow local
protocol naming only the two methods this adapter calls
(`mark_worker_crashed` and `list_active_for_activation`). The annotation is
still `PluginJobRepository`; the check is deliberately narrower so the adapter
does not reject a real repository because that protocol grew a method the
adapter never uses.

Validation starts a fresh process, verifies hello identity and version, activates
the private session, registers authority as non-serving, binds brokers, then
freezes candidate data and records its final revision. The returned activation
reference is lookup evidence, never a bearer token. If the lifecycle coordinator
already froze a same-data prior generation, validation reuses that exact freeze;
staged data reuses the lifecycle operation's sealed dataset receipt. A fresh
validation of an already-selected generation uses a new freeze receipt so its
later thaw and activation cannot replay a consumed selection transition.

Admission holds `VersionedPluginDataStore.mutation_barrier()` while it revokes
the prior authority, thaws the exact validation freeze, checks and activates the
selected dataset revision, admits authority, and publishes the serving view.
Disabled and removed records publish no runtime and keep data non-writable.
`serving(extension_id)` returns only immutable identity, selection, and
activation-bound channels; it exposes neither activation tokens nor child
process handles.

Quiesce removes serving admission and revokes authority before asking the child
to drain. It then marks every queued or running durable job for that exact
activation interrupted and closes/reaps the owned child, even when drain fails
or reports incomplete work. A recovered enabled selection requires
`ActivationAuthorityController.identity_for` to resolve its prior activation ID;
absence fails closed because the jobs repository cannot query by activation
generation.

## Unexpected worker loss

Each validated activation gets a `_WorkerLossGuard`, passed to `ProcessRuntime`
as `worker_loss_listener` and also used by this adapter's own supervision
thread. Two detectors converge on the same latch:

* the runtime reports a lost child from its stop path, with the loss code it
  can actually distinguish (`exited`, `timeout`, `unresponsive`, `killed`);
* a poll every `DEFAULT_LIVENESS_POLL_S` asks each *serving* runtime for its
  invocation channel, which fails once that runtime has stopped. This is the
  independent slow path, and it is what settles a loss when the installed
  `ProcessRuntime` reports none.

Settlement is quiesce without the drain: revoke the identity, unpublish,
interrupt that activation's jobs, deactivate the session, close the runtime,
and record a `WorkerLossNotice`. It runs under the same `_effects` lock as
every operator operation, so an operator quiesce or disable racing a death is
mutually exclusive with it — whichever arrives first settles the activation and
the other finds it gone. A loss reported while this adapter is deliberately
closing a runtime is ignored, and a loss before the activation exists is left to
validation's own failure path. Interrupted jobs are never replayed.

`worker_supervision(extension_id)` reports the health state and last loss.
`settle_orphaned_jobs(record)` interrupts jobs owned by a prior activation
nothing is serving — the startup sweep and the shutdown path for a worker that
already died. `set_worker_loss_observer` closes the composition edge to the
restart supervisor after both objects exist. `close()` stops supervising.

## Bounded restart

`restart.py` holds `ActivationRestartSupervisor`: one `RestartLedger` per
extension, one timer thread, and replacement through the ordinary
`validate` -> `admit` path, so a replacement gets a fresh `ActivationIdentity`
and a fresh revocation generation while the dead worker's identity stays
revoked forever. `DEFAULT_RESTART_POLICY` is three attempts, 0.5s backoff
doubling to a ceiling of 8s, forgiven after 60s healthy; the host takes a
`restart_policy` to override it.

Giving up is visible, not silent: `report(extension_id)` returns
`supervision_status` (`healthy`, `restarting`, `degraded`, `idle`), the attempt
count, the last failure code, and `gave_up`. A replacement that stays healthy
for `reset_after_healthy_s` clears the history; so does an operator disabling
and re-enabling the extension. A failure of the replacement itself counts
against the same budget.

This package does not implement artifact persistence, authority persistence,
broker method routing, registrar/bootstrap integration, or job resume. Restarts
never resume interrupted work; explicit `engine.v1.jobs.resume` is a separate
slice. A production broker factory and artifact resolver are separate
composition slices. Stale activation references remain process-local and are
rejected after adapter restart; the durable lifecycle coordinator recovers by
performing a fresh non-serving validation against the persisted data revision.
If restart occurs after rollback has thawed a retained prior dataset, validation
reads that sealed dataset's exact revision and leaves the restoration transition
to `activate_selected` under the shared barrier.

`activation_lifecycle/__init__.py` is owned elsewhere and does not yet re-export
the supervision names; import `WorkerLossNotice`, `WorkerSupervisionState`, and
`ActivationJobSettlement` from `.adapter`, and the supervisor from `.restart`.

Focused tests use an isolated synthetic subprocess plus real SQLite versioned
data and job repositories:

```sh
PYTHONPATH=src python -B -m unittest tests.plugins.test_extension_activation_lifecycle
PYTHONPATH=src python -B -m unittest tests.plugins.test_worker_loss_supervision
PYTHONPATH=src python -B -m unittest tests.plugins.test_worker_restart_backoff
```

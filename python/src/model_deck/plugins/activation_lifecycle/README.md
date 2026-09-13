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

This package does not implement artifact persistence, authority persistence,
broker method routing, registrar/bootstrap integration, job resume, or process
restart. A production broker factory and artifact resolver are separate
composition slices. Stale activation references remain process-local and are
rejected after adapter restart; the durable lifecycle coordinator recovers by
performing a fresh non-serving validation against the persisted data revision.
If restart occurs after rollback has thawed a retained prior dataset, validation
reads that sealed dataset's exact revision and leaves the restoration transition
to `activate_selected` under the shared barrier.

Focused tests use an isolated synthetic subprocess plus real SQLite versioned
data and job repositories:

```sh
PYTHONPATH=src python -B -m unittest tests.plugins.test_extension_activation_lifecycle
```

# Plugin job wire adapter

## Purpose and ownership

`PluginJobWireAdapter` connects the process runtime's generic broker callback
shape to `PluginJobBroker`. Supervisor composition binds the adapter to one
trusted `ActivationIdentity` and one broker. The engine package does not
import the process runtime, open job storage directly, or issue authority.

## Callable contract

The adapter is called as:

```python
handler(authenticated_activation_id, method, params)
```

It supports exactly the five inventory methods
`plugin.v1.broker.jobs.{create,progress,complete,fail,check_cancelled}`. The
broker folds QUEUED -> RUNNING into the create path internally, so the
runtime never observes a QUEUED job past the create call; worker loss or a
crash between create and the internal claim transitions the row to
INTERRUPTED on restart, never auto-replayed.
Before validating parameters or entering the broker, it requires the
runtime-authenticated activation id to match the bound identity. Request
parameters cannot supply or replace the trusted identity.

Each request and result is validated against its exact bundled
`contracts/plugin.v1/broker/jobs.*` schema. Validation failures become
fixed `PluginJobWireRequestError` or `PluginJobWireResultError` messages
that do not echo worker data. An activation mismatch raises the fixed
`PluginJobWireActivationError`.

After validation, fields are forwarded without coercion. `create` passes
`invocation_handle`, `operation_id`, and optional `checkpoint_schema_id`
through to `PluginJobBroker.create`. The progress / complete / fail /
check_cancelled calls pass only the frozen `job_id` plus the per-method
payload and forward exactly. Owner, revocation, mutation guard, and
repository work all stay in `PluginJobBroker`; the wire never re-checks
identity, never opens SQLite, and never reaches into the broker's private
state.

## Verification

From `Architecture/python`:

```sh
PYTHONPATH=src /tmp/md-b18-venv/bin/python -m unittest tests.engine.test_plugin_job_wire
```

The tests use the real authority service, job broker, and SQLite
repository. They cover the full lifecycle
(create → progress → check_cancelled → complete), activation and
revocation denials with no repository effect, the typed broker errors,
and the frozen schema validation failures.

## Extension limits

Adding a method requires a committed inventory entry and matching parameter and
result schemas before this adapter changes. This boundary does not mint grants,
infer jobs, retry mutations, expose public retrieval helpers, run jobs, or
inspect private broker or database state. Public retrieval and cancellation are
separate application use cases. There is no public runner or explicit resume
implementation.

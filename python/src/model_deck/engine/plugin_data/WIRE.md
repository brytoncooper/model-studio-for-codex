# Plugin data wire adapter

## Purpose and ownership

`PluginDataWireAdapter` connects the process runtime's generic broker callback
shape to `PluginDataBroker`. Supervisor composition binds the adapter to one
trusted `ActivationIdentity` and one broker. The engine package does not import
the process runtime, open plugin-data storage directly, or issue authority.

## Callable contract

The adapter is called as:

```python
handler(authenticated_activation_id, method, params)
```

It supports exactly the four inventory methods
`plugin.v1.broker.storage.{get,list,put,delete}`. Before validating parameters
or entering the broker, it requires the runtime-authenticated activation ID to
match the bound identity. Request parameters cannot supply or replace the
trusted identity.

Each request and result is validated against its exact bundled
`contracts/plugin.v1/broker/storage.*` schema. Validation failures become fixed
`PluginDataWireRequestError` or `PluginDataWireResultError` messages that do not
echo worker data. An activation mismatch raises the fixed
`PluginDataWireActivationError`.

After validation, fields are forwarded without coercion. Omitted list fields
use the frozen `prefix=""` and `limit=200` defaults. Omitted compare-and-swap
revisions use `None`. The broker remains responsible for namespace isolation,
live invocation-handle authority, revocation checks, its mutation guard,
revision conflicts, quotas, and repository errors; those exceptions pass
through unchanged.

## Verification

From `Architecture/python`:

```sh
PYTHONPATH=src /tmp/md-b18-venv/bin/python -m unittest tests.engine.test_plugin_data_wire
```

The tests use the real authority service, data broker, and SQLite repository.
They cover all four method mappings, schema failures, forged activation and
namespace attempts, and a handle invalidated by revocation.

## Extension limits

Adding a method requires a committed inventory entry and matching parameter and
result schemas before this adapter changes. This boundary does not mint grants,
infer namespaces, retry mutations, or inspect private broker or database state.

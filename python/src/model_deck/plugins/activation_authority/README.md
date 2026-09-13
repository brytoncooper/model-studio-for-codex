# Activation authority controller

## Purpose and ownership

`SQLiteActivationAuthorityController` is the supervisor-owned bridge between
process activation and `PluginAuthority`. It owns durable activation identities,
their exact selected-installation bindings, admission state, and monotonic
revocation generation. The SQLite path is explicit and remains private to the
supervisor.

## Contracts

The controller implements the structural `ActivationAuthorityController` port:
`register_non_serving`, `revoke`, `admit`, and `identity_for`. It also provides
the `TrustedAuthorityState` methods used by `PluginAuthority`. Origin and
operation records come from injected trusted readers. Activation permissions and
expiry come from an injected policy called with the authenticated identity and
the selected installation's approved scopes.

Composition must inject the same reentrant mutation barrier used by versioned
plugin data and plugin jobs. Every activation, origin, operation, identity, and
mutation path enters that barrier. Broker operations can therefore hold it
across authorization and their data or job mutation.

## Invariants

- Registration is non-serving. Admission is the only transition that enables
  authority.
- The selected binding covers artifact hash, extension id, version, requested
  and approved scopes, data reference, grant generation, and activation
  generation.
- Restart retains the selected-to-identity mapping for cleanup, while a new
  runtime epoch cannot use the old serving state.
- Revocation increments once and is permanent for an activation identity.
  Rollback must register a fresh identity before admission.
- The controller stores no process handle, activation token, or other bearer
  credential. Plugin-supplied documents never choose permissions.

## Extension

Add permission vocabulary in the caller-owned policy and public operation
contracts. Add new durable activation fields only when an authority decision
needs them, and keep migration within this package. Other storage, lifecycle,
job, and broker modules should consume this public controller surface instead
of reading its tables.

## Tests

From `python/`:

```sh
PYTHONPATH=src /tmp/md-b18-venv/bin/python -m unittest \
  tests.plugins.test_activation_authority_controller
```

The suite uses a real `PluginAuthority`, an explicit temporary SQLite file, and
the shared barrier. It covers candidate admission, revocation, exact selection
matching, restart fail-closed behavior, fresh-identity rollback, and absence of
credential storage.

## Limits

This package does not launch processes, create activation tokens, derive grant
semantics, manage origin or operation records, or compose lifecycle/data/job
services. Each controller instance generates a fresh private runtime epoch.

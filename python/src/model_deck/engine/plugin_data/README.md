# Plugin data (B19)

Revisioned per-plugin key/value repository behind `plugin.v1/broker/storage.*`.
The trusted caller supplies `namespace`; a future broker derives it from the
authenticated activation. This repository is not an authorization gate.

## Contracts

`ports.py` declares `PluginDataRepository`, `PluginDataEntry`,
`PluginDataListItem`, `PluginDataQuota`, and typed errors
`PluginDataNotFoundError`, `PluginDataRevisionConflictError`,
`PluginDataQuotaExceededError`. Wire shapes match frozen
`contracts/plugin.v1/broker/storage.{get,list,put,delete}.*.schema.json`:
get returns `{value, revision}`, put returns `{revision}`, delete returns
`{deleted}`, list returns `{items: [{key, revision}]}` with no cursor field.

## Invariants

- Namespaces are transactionally isolated; one namespace never affects another.
- `expected_revision` omitted means unconditional within the authorized
  namespace. Provided `0` means never-created. Provided `n` requires the
  latest revision to equal `n`.
- Revisions are monotonic per namespace/key across delete and recreate.
  Tombstones prevent ABA: recreating after delete requires the tombstone
  revision, never `0`.
- Absent or tombstoned get raises typed not-found.
- Delete of absent or already-tombstoned keys returns `deleted: false` when
  unconditional or when the expectation matches; otherwise revision conflict.
- `null` JSON is a valid stored value, distinct from missing.
- `NaN`, infinities, and `bool` expected revisions are rejected as invalid.
- Quota defaults: 10 MiB logical bytes, 10000 live keys, 1 MiB per value.
  Logical usage per live key is `len(namespace UTF-8) + len(key UTF-8) +
  len(canonical JSON value UTF-8)`. Quota and compare-and-swap apply in one
  atomic transaction.
- List is deterministic by key ascending, applies exact case-sensitive
  prefix filtering, and honors limit 1..200. The frozen list wire has no
  cursor token; ``prefix`` plus ``limit`` only scopes a single query and is
  not a pagination mechanism. Do not invent a cursor.

## Extension and limits

- Storage adapter owns SQLite schema, canonical JSON, and quota accounting.
- Quota is configured per repository instance; wire contracts carry no quota.
- This package performs no IO, auth, or broker dispatch.

# Storage adapters

This directory owns the durable SQLite implementations of Model Deck's
engine ports. The engine calls repository contracts; these adapters own SQL,
serialization and transactions. The JSON catalog cache is a read-only file
adapter. Composition supplies explicit paths to both kinds of store.

## Storage ownership

Three deep-dive guides cover the intricate slices:

- [`HOST_SETTINGS.md`](HOST_SETTINGS.md) — preview tokens and save-receipt
  idempotency ledgers (`sqlite_host_settings.py`).
- [`SQLITE_EXTENSION_LIFECYCLE.md`](SQLITE_EXTENSION_LIFECYCLE.md) —
  durable claims, phase evidence, rollback intent and idempotency receipts
  for extension installation (`sqlite_extension_lifecycle.py`).
- [`MODEL_PROJECTION_INVALIDATION.md`](MODEL_PROJECTION_INVALIDATION.md) —
  one-transaction invalidation of connection-dependent models and the
  historical dependency-expansion recovery path
  (`sqlite_model_projection_invalidation.py`,
  `sqlite_projection_dependency_expansions.py`,
  `sqlite_projection_dependency_recovery.py`).

Cross-cutting collaborators (`sqlite_model_schema.py`) share model DDL between repositories. `json_catalog_cache.py` is the non-SQLite exception: it backs `CatalogCacheRepository` from a fixture on disk.

## Public engine ports fulfilled

| Adapter | Engine port |
|---|---|
| `sqlite_host_settings.py` | `host_settings.ports.PreviewStorePort`, `SaveReceiptStorePort` |
| `sqlite_extension_lifecycle.py` | `extensions.ports.ExtensionLifecycleRepository` |
| `sqlite_connection_repository.py` | `connections.ports.ConnectionRepository` |
| `sqlite_model_repository.py` | `model_library.ports.ModelRepository` |
| `sqlite_session_run_repository.py` | `sessions.ports.SessionRepository` and `runs.ports.RunRepository` |
| `sqlite_usage.py` | `usage.ports.UsageRepository` |
| `sqlite_plugin_jobs.py` | `jobs.ports.PluginJobRepository` |
| `sqlite_plugin_data.py` | `plugin_data.ports.PluginDataRepository` |
| `sqlite_outbox.py`, `sqlite_projection_intents.py`, `sqlite_projection_receipts.py`, `sqlite_projection_outbox.py` | `projections.ports.ProjectionOutboxReader` plus writer seams |
| `json_catalog_cache.py` | `model_library.ports.CatalogCacheRepository` |

## Invariants

- Keep a state change and its associated receipt or outbox event in the same
  transaction. Connection saves also invalidate dependent model projections
  inside that transaction; the linked guide explains this shared ownership.
- Retry identities and revision rules belong to each port. For example, extension
  lifecycle receipts use principal, action and idempotency key. Do not assume
  another repository has the same key or that a read can replace a mutation's CAS.
- Persist the data the subsystem owns: run events, plugin values and usage records
  are different from credential references. These stores are not a credential
  vault. A generic claim that every table contains only metadata would be false.
- Recovery follows each subsystem's contract. Claimed provider runs are interrupted
  after restart; extension lifecycle operations retain restoration intent until
  explicit recovery completes. Never apply one subsystem's recovery policy to all.

## Adding adapter behavior

1. Change the immutable engine port first. The Protocol lives under
   `engine/<subsystem>/ports.py`; port changes need coordinator and
   adapter owner review before schema work begins.
2. Update the owning schema and serialization together. Use explicit columns for
   indexed identities and concurrency checks; structured payloads must retain
   their domain validation and exact replay semantics.
3. Add a focused repository test under `python/tests/engine/`. Use a
   temporary real SQLite database; never connect to a live one.
4. Schema evolution needs an explicit migration before the adapter is
   used with a previously released database. `CREATE TABLE IF NOT EXISTS`
   only covers first creation.

## Tests

From `python/`, using Python 3.11 or newer with the project dependencies installed:

```sh
PYTHONPATH=src python -B -m unittest \
    tests.engine.test_repository_conformance \
    tests.engine.test_sqlite_host_settings tests.engine.test_sqlite_extension_lifecycle \
    tests.engine.test_sqlite_model_repository tests.engine.test_sqlite_connection_repository \
    tests.engine.test_sqlite_session_run_repository tests.engine.test_sqlite_plugin_jobs \
    tests.engine.test_sqlite_plugin_data tests.engine.test_sqlite_projection_intents \
    tests.engine.test_sqlite_projection_outbox tests.engine.test_sqlite_projection_receipts \
    tests.engine.test_model_projection_invalidation
```

## Current integration limits

- Repository transactions serialize database writes. They do not grant ownership
  of external effects; lifecycle composition also requires the exclusive engine
  instance lease. Individual repository tests are not full application proof.
- The schema is created lazily for a new, explicit database path.
  Opening a previously released database requires a versioned migration.
- Backup or copy only while no writer holds the file, or use the SQLite
  backup API. A naive copy may capture a torn state.
- Storage adapters validate their inputs and persisted records. Provider execution,
  transport and caller authorization remain outside storage. See the delivery
  checklist for which repositories are connected to the running engine composition.

# Connection-dependent model revisions

This storage collaborator keeps connection saves and dependent model projection
intent in one SQLite transaction. `SQLiteConnectionRepository.save` acquires
`BEGIN IMMEDIATE`, applies the connection change, queues `connection.saved`,
calls `invalidate_connection_models`, records a dependency-expansion receipt,
stores its idempotency receipt and commits.
Any failure rolls back that complete mutation unit.

`invalidate_connection_models(connection, *, connection_id)` requires the
caller's active write transaction. It increments every active registration on
that exact connection and queues a normal `registered_model.upserted` with its
new revision and unchanged identity, model name and display name. It returns
the updated records. It never opens another connection, commits, initializes
schemas, touches another connection's registrations or changes tombstones.
`ensure_model_schema` shares the unchanged model DDL between both repositories;
it refuses initialization inside a transaction because SQLite `executescript`
can otherwise commit the caller's work.

A model revision now covers its effective connection-dependent configuration.
Consequently a connection save can make a previously read model revision stale:
rename/remove with that revision must conflict and the caller must refresh.
Every newly admitted connection save advances revisions, including an accepted
save whose fields equal their prior values, consistent with connection revision
behavior. Exact idempotency replay returns its stored result without advancing
connection/model revisions or adding outbox events. Historical model mutation
receipts similarly remain historical; listing gives current revisions.

SQLite serializes save, rename and removal transactions. Removal before a
connection save excludes the inactive row; removal after a save must use the
advanced revision and emits a still-newer tombstone. Existing consumer revision
guards therefore continue to reject earlier upserts after applying deletion.
No synthetic revision is invented outside committed desired state.

Public command/dataclass signatures, SQL table shapes and outbox payload shapes
are unchanged. The generic storage layer coordinates its own transaction;
host consumers do not inspect model or connection private storage.

`projection_dependency_expansions` stores one receipt per connection outbox ID,
binding the canonical source payload, connection identity/revision and the exact
list of expanded model IDs/revisions. `record_connection_expansion` requires an
active transaction and checks every supplied model against its newly queued
normal upsert. The connection repository supplies the complete return value of
its invalidation step. Empty expansion is valid when no active models depend on
the connection. The helper opens no second connection and commits nothing.

The outbox reader excludes only `connection.saved` rows whose expansion receipt
matches the source row. Their stored state remains `pending`; no file-applied
receipt or artifact is invented. New connection metadata therefore cannot fill
every consumer batch and starve its generated model events. Unknown event kinds
and connection rows without matching proof remain visible. A historical database
without the receipt table uses the original read-only pending query; reading
never creates tables or acknowledges old events.

Historical unexpanded rows need a separate recovery policy. Do not silently
acknowledge them or create an empty receipt based on a later observation. A
future transactional handler must recheck committed current connection/model
state, generate fresh monotonic model invalidations and record expansion once;
the policy for superseded connection revisions must be explicit. This handler
is not implemented. Materialization, consumer scheduling and filesystem/SQLite
atomicity remain outside this storage slice.

Focused checks from `python/`:

```sh
PYTHONPATH=src python -B -m unittest tests.engine.test_model_projection_invalidation
PYTHONPATH=src python -B -m unittest tests.engine.test_projection_dependency_expansions
```

Tests use temporary real SQLite databases and separate connections. They cover
active-only scope, initial missing connections, idempotency, rollback at enqueue
and receipt boundaries, rename/removal ordering and concurrent saves/mutations.

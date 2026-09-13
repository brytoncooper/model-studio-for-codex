# Connection-dependent model revisions

This storage collaborator keeps connection saves and dependent model projection
intent in one SQLite transaction. `SQLiteConnectionRepository.save` acquires
`BEGIN IMMEDIATE`, applies the connection change, queues `connection.saved`,
calls `invalidate_connection_models`, stores its idempotency receipt and commits.
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

The connection metadata event still needs a separate explicit acknowledgment
contract. This module neither fabricates a file receipt nor solves pending-row
starvation, materialization, consumer scheduling or filesystem/SQLite atomicity.

Focused checks from `python/`:

```sh
PYTHONPATH=src python -B -m unittest tests.engine.test_model_projection_invalidation
```

Tests use temporary real SQLite databases and separate connections. They cover
active-only scope, initial missing connections, idempotency, rollback at enqueue
and receipt boundaries, rename/removal ordering and concurrent saves/mutations.

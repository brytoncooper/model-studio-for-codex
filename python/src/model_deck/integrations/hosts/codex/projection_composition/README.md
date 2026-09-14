# Projection composition snapshots

Composition of committed repository snapshots and the existing Codex managed
agent projection pipeline.

## Ownership

`projection_composition` owns only the snapshot adapters. The executable
composition boundary owns `CodexProjectionCoordinator` because it is the one
place allowed to wire host code to concrete storage and filesystem adapters.
The coordinator receives explicit repositories, database path, projection
root, token-helper path, and connection metadata resolver.

## Contracts

- Models: `PUBLIC ModelRepository.list_registered()` scanned for the requested
  `registration_id`. Missing returns `None`; conflicting duplicates raise
  `CommittedSnapshotError`.
- Connections: `PUBLIC ConnectionRepository.list_connections()` scanned for the
  requested `connection_id`. Missing returns `None`; duplicates raise
  `CommittedSnapshotError`. The injected `ConnectionMetadataResolver` maps the
  committed `ConnectionRecord` (opaque `endpoint_config_ref` / `credential_ref`)
  to render metadata. The adapter verifies the returned `connection_id` and
  `revision` match the committed record. Every resolver failure mode
  (arbitrary exception including `CommittedSnapshotError`, `None` or
  wrong-typed result, identity/revision mismatch) raises the single fixed
  `CommittedSnapshotError("connection metadata unavailable")` with
  `from None`, so no resolver message or traceback chain can leak secret
  material.
- Credential values are never read; opaque refs are decoded only by the caller
  that owns them. No home discovery, no SQL or private-storage imports.

## Invariants

- Adapters implement the `lookup` protocols structurally consumed by
  `AgentMaterializer`; `None` surfaces as snapshot-missing, raised
  `CommittedSnapshotError` surfaces as snapshot-unavailable.
- Removed registrations disappear from `list_registered`, so lookups return
  `None` and materialization refuses instead of rendering stale data.
- `SQLiteConnectionRepository.save` atomically expands `connection.saved` into
  new revisions for only the affected active registrations. The coordinator
  consumes those model events; it does not implement a second fanout.
- Engine mutation results describe committed state only. Reconciliation runs
  after commit and never changes a successful mutation into a projection claim.
- `engine.v1.hosts.projection_status` exposes `ready`, `pending`, or `failed`.
  Unresolved conflicts are derived from durable receipts, so failure survives a
  restart. A later successfully applied model revision resolves the older
  conflict for status purposes.

## Tests

`python/tests/integrations/hosts/codex/test_projection_snapshots.py` uses real
`SQLiteModelRepository` / `SQLiteConnectionRepository` plus the real
`AgentMaterializer` and renderer over temp-dir fixtures: updated and removed
records, resolver identity/revision mismatch rejection, resolver failures
carrying no secret material, and fixed-message/`from None` normalization
(typed error, runtime error, `None` return) with traceback sentinel checks.
`test_projection_full_path.py` uses a real isolated engine socket and temporary
host root for create, rename, connection fanout, removal, restart, foreign-file
conflict/recovery, and the staged MCP entrypoint.

## Limitations

- Full-table scans per lookup; suitable only for the bounded composition path.
- The resolver is deliberately bound to the one provider profile supplied by
  isolated V2. A committed connection whose opaque references no longer match
  that profile fails projection instead of guessing metadata.
- Host reload in an actual Codex Desktop process remains live qualification.
  B10's isolated adapter tests cover discovery and pure launch preparation;
  these projection tests verify the exact isolated `CODEX_HOME/agents`
  materialization contract without launching or modifying live Codex.

# Projection composition snapshots

Read-only adapters mapping committed repository state onto the
`AgentMaterializer` snapshot protocols (`ConnectionSnapshot`, `ModelSnapshot`).

## Ownership

`projection_composition` owns only these adapters. It never writes projections,
never fans out `connection.saved`, and never touches shared engine ports, the
projection consumer, or storage internals.

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
- `connection.saved` fanout is out of scope; revision cross-checks between
  model events and connection records are settled separately by root.

## Tests

`python/tests/integrations/hosts/codex/test_projection_snapshots.py` uses real
`SQLiteModelRepository` / `SQLiteConnectionRepository` plus the real
`AgentMaterializer` and renderer over temp-dir fixtures: updated and removed
records, resolver identity/revision mismatch rejection, resolver failures
carrying no secret material, and fixed-message/`from None` normalization
(typed error, runtime error, `None` return) with traceback sentinel checks.

## Limitations

- Full-table scans per lookup; suitable only for the bounded composition path.
- No policy for re-driving models affected by a `connection.saved` event yet.

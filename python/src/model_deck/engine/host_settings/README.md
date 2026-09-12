# Host settings (engine slice)

`model_deck.engine.host_settings` is the generic settings engine. It owns
authorization, schema validation, structural bounds, preview-token binding, and
save idempotency receipts. The host adapter owns parsing, protection policy,
and lossless file writes. The engine never touches the filesystem, network, or
Git.

Spec: `docs/plans/plugin-architecture/SETTINGS-API.md`. Schemas live under
`contracts/common/host_settings.schema.json` and
`contracts/engine.v1/methods/hosts.settings.{read,validate,preview,save}.{params,result}.schema.json`.

## Files

- `__init__.py` — re-exports the public ports, errors, and service.
- `ports.py` — grants, `CallerContext`, `PreviewRecord`, `SaveClaimBinding`,
  claim outcomes, the four port Protocols, and the domain errors.
- `service.py` — `HostSettingsService` (`read`, `validate`, `preview`, `save`)
  plus frozen bounds and safe-error mapping.

Only this package, its test (`python/tests/engine/test_host_settings.py`), and
this guide are owned here. Shared schema, bootstrap, dispatch, host-key lists,
and adapters are out of scope.

## Ports

`SettingsDocumentPort` (host adapter: parsing, protection, lossless write):

- `read(host_id) -> snapshot`
- `validate(host_id, document_id, expected_content_hash, context_revision, draft) -> candidate`
- `preview(host_id, document_id, expected_content_hash, context_revision, draft) -> candidate`
- `save(host_id, document_id, expected_content_hash, context_revision, candidate_content_hash, candidate_raw_toml) -> result`

`PreviewStorePort` (atomic single-use preview ledger):
`admit(record)`, `lookup(preview_id)`, `is_consumed(preview_id)`,
`consume(preview_id)`.

`SaveReceiptStorePort` (atomic idempotency ledger):
`lookup(principal, key, fingerprint)`, `store(...)` (compatibility),
`claim(binding) -> (CLAIM_ADMITTED | CLAIM_REPLAY | CLAIM_IN_PROGRESS, settled?)`,
`settle(principal, key, fingerprint, result)`,
`release(principal, key, fingerprint)`.

`TokenFactory`: injected `() -> str` opaque preview-token source.

`CallerContext` (frozen, slots): server-assigned `principal` plus
`grants: frozenset[str]`. `PreviewRecord` binds one issued token to
principal, host, document, base hash, context revision, and candidate hash.
`SaveClaimBinding` persists principal, idempotency key, request fingerprint,
host, document, base hash, context revision, candidate hash, and preview id.
It carries hashes only, never raw TOML.

## Authorization

Every call checks identity before any port is touched: the principal must
equal the injected `local_operator_principal` and the grants must contain
`hosts.settings.read` (`read`, `validate`, `preview`) or
`hosts.settings.write` (`save`). Anything else raises `SettingsDeniedError`
without invoking a port. Grants are plain string constants (`READ_GRANT`,
`WRITE_GRANT`).

## Save flow

1. Schema-validate params, enforce UTF-8 source bounds, fingerprint the exact
   request, and `lookup` the receipt: a settled entry replays the exact stored
   result.
2. Resolve the preview token: it must exist, be live (not consumed), and match
   the caller principal, host, document, base hash, context, and recomputed
   UTF-8 SHA-256 candidate hash.
3. `claim(binding)`: exactly one concurrent same-key writer is admitted; a
   settled claim replays; any other live claim reports `in_progress` and the
   loser gets a safe conflict with no second save.
4. Revalidate the raw candidate through the adapter. If invalid, `release` the
   claim (save was never invoked, so release is provably safe) and raise
   `SettingsInvalidError`.
5. Invoke `document.save` exactly once. Any exception after invocation begins
   — safe or unknown — preserves the uncertain claim and raises a safe
   conflict (`save outcome uncertain; retry with same key`). The service never
   automatically re-saves.
6. `settle` the receipt, then `consume` the preview. A settle failure keeps
   the claim uncertain and the preview live, and reports the same safe
   conflict.

## Claim recovery limitations (read this before building the real ledger)

- The fake ledger in tests is in-memory only. The real adapter ledger must
  implement `claim`/`settle` atomically and durably: concurrent identical
  claims admit one writer, unsettled claims report `in_progress` and are never
  re-admitted, settled claims replay the exact stored result.
- Once `document.save` invocation starts, the claim stays uncertain until
  `settle` succeeds. A crash, settle failure, or ambiguous adapter error means
  the write may or may not have landed; the service returns a safe conflict
  and requires the operator to retry with the same idempotency key. It never
  re-saves on its own and never pretends revalidation proves the outcome.
- Failed pre-write validation is the only path that releases a claim, and it
  is allowed only because `save` was provably never invoked.
- The persisted binding holds hashes for future adapter reconciliation of the
  exact bytes; raw TOML is never stored in the ledger.
- Every raised error uses `from None` and every adapter, schema, and ledger
  failure is mapped to a fixed safe message, so tracebacks and secrets never
  leak.

## Invariants and bounds

- Request and response envelopes are schema-validated on both sides.
- Context (`context_revision`) is required and bound into preview issuance,
  save binding, and hash verification.
- Frozen structural bounds: 262144 UTF-8 bytes raw source, 131072 diff,
  1048576 frame, 256 descriptors, 256 entries, max entry depth 4. Oversize
  request sources are rejected before any port; oversize adapter output is an
  internal error.
- Preview tokens are single-use, principal-bound, collision-retried (3
  attempts), and consumed only after a settled save.
- Save is exactly-once per admitted claim: replay returns the identical
  result, `in_progress` never triggers a second `document.save`.

## Extension

- Add a host by implementing `SettingsDocumentPort` against its on-disk
  format; no engine change is needed. Keep parsing, protection checks, and
  backup/lossless-write behavior inside the adapter.
- Provide durable `PreviewStorePort` and `SaveReceiptStorePort` adapters with
  the atomic claim/settle semantics above before any production use.
- Never store raw TOML in the receipt ledger and never widen bounds without
  updating the shared contract tests.

## Dispatch pending

The `hosts.settings.*` wire methods are specified and schema-validated but not
wired into `engine/dispatch.py` in this slice. Dispatch registration, host-key
lists, and bootstrap changes belong to a separate task.

## Tests

Run `PYTHONPATH=python/src /tmp/md-b18-venv/bin/python -m unittest discover -s
python/tests/engine -p "test_host_settings.py"` (23 tests). Coverage:
read_ok, read denied principal and missing grant (port skipped), validate raw
ok, preview-then-save ok, tampered candidate conflict, replay after stale
returns original, consumed preview with new key conflicts, same key with
changed request conflicts, unknown preview rejected, schema-invalid adapter is
internal, oversize source rejected before port, oversize adapter diff is
internal, adapter and schema exceptions leak no secret, oversize snapshot and
descriptor flood rejected, receipt store failure surfaces uncertainty, preview
ledger failure sanitized, settle-failure retry writes once (one `save`, preview
live), concurrent same-key second gets `in_progress` with zero saves,
pre-write validation failure releases the claim, binding persists hashes with
no raw TOML.

## Repair3 (claim recovery hardening)

- Preview reservation is atomic across ALL keys: `claim()` reserves `preview_id`
  to exactly one `(principal, idempotency_key, fingerprint)`. Same key replays
  (settled) or reports `in_progress` (unsettled, never re-admitted); a
  different key reusing the same preview gets `ReceiptConflictError` -> safe
  `conflict`. `release()` before any save invocation frees both key and
  preview indexes; `settle()` retains the preview reservation permanently, so
  a settled preview can never write again. Restart proof is owned by the
  ledger; revalidation alone is not recovery.
- Pre-write failures always release: any exception from the save-time
  revalidate (including adapter `RuntimeError`) releases the admitted claim
  and re-raises the sanitized original. Release failure is reported as safe
  uncertain `conflict` (retry same key), never as freed.
- Adapter typed errors never leak text: every `Settings*Error` raised by the
  adapter is remapped to a fixed safe message (reason allowlist `conflict` /
  `preview_consumed` only); `__cause__` is always `None`.

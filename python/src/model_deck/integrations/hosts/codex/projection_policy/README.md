# Projection policy (B09)

Pure, filesystem-free decision planner for host projection reconciliation.

`plan_projection` takes observed state and engine-owned state and returns a
`ProjectionDecision` describing what the caller should do. It performs no I/O:
it does not read, write, hash, stat, or import concrete storage, and it never
mutates anything. It is a plan, not an action.

## Contract

```python
plan_projection(
    *,
    operation: "upsert" | "remove",
    current_sha256: str | None,
    previous_owned_sha256: str | None,
    desired_sha256: str | None,
    managed_marker_matches: bool,
) -> ProjectionDecision
```

All hashes are bare lowercase 64-character hex sha256 digests (the output of
`hashlib.sha256(...).hexdigest()`), or `None`.

- `current_sha256` — hash of the file as observed now, or `None` if absent.
- `previous_owned_sha256` — hash the engine last recorded as owned, or `None`
  when previous ownership is absent or unknown.
- `desired_sha256` — content the engine wants for `upsert`. Must be `None` for
  `remove`.
- `managed_marker_matches` — whether the file carries the engine's managed
  marker. Must be a strict `bool`.

`operation` must be exactly `"upsert"` or `"remove"`. Malformed hashes,
operations, and non-bool marker values raise `ValueError`.

## Decision result

`ProjectionDecision` is frozen and carries:

- `action` — one of `CREATE`, `REPLACE`, `DELETE`, `NOOP`, `CONFLICT`.
- `expected_sha256` — the precondition the caller must verify atomically before
  mutating. For `REPLACE` and `DELETE` it is the current hash observed when the
  plan was made. For `CREATE` it is `None`, meaning the path must still be absent
  at the mutation boundary; that absence is itself an atomic precondition. For
  `NOOP` and `CONFLICT` it is also `None`, and no write is performed, so there is
  no precondition to check.
- `reason` — a stable machine-readable code.

Reason codes:

| reason | action | meaning |
| --- | --- | --- |
| `create_absent` | `CREATE` | Upsert target does not exist. |
| `noop_already_desired` | `NOOP` | Owned target already matches desired bytes. |
| `noop_remove_absent` | `NOOP` | Remove target does not exist. |
| `replace_owned_stale` | `REPLACE` | Owned target matches prior ownership but not desired. |
| `delete_owned` | `DELETE` | Owned target matches prior ownership; safe to remove. |
| `conflict_unmanaged_existing` | `CONFLICT` | Existing file lacks the managed marker. |
| `conflict_missing_ownership` | `CONFLICT` | Marker present but no prior ownership evidence. |
| `conflict_foreign_edit` | `CONFLICT` | Owned file was changed since last recorded hash. |

## Rules

Upsert:

- Absent target -> `CREATE` with `expected_sha256=None`, meaning the caller must
  still confirm the path is absent atomically at the mutation boundary.
- Existing target must have a matching managed marker and non-`None` prior
  ownership evidence; otherwise `CONFLICT`, even when the bytes already equal
  `desired_sha256`.
- `current == desired` (with marker + ownership) -> `NOOP`, covering crash
  recovery where the write landed but the ownership receipt did not.
- `current == previous` and `current != desired` -> `REPLACE` with
  `expected_sha256=current`.
- `current != previous` (and `current != desired`) -> `CONFLICT`.

Remove:

- Absent target -> `NOOP`.
- Existing target is `DELETE` only when the marker matches, prior ownership is
  known, and `current == previous`; otherwise `CONFLICT`.

`NOOP` never grants new ownership for a foreign file.

## Caller contract

A projection is only safe when the caller treats this plan as advisory and
re-checks the precondition at the moment of mutation.

1. At the mutation boundary, check the precondition atomically: for `REPLACE`
   and `DELETE`, re-hash the target and compare it to `expected_sha256`; for
   `CREATE`, confirm the path is still absent. If the precondition no longer
   holds, discard the plan and re-plan.
2. Perform the file mutation and record the applied revision/ownership receipt
   separately, so a crash between the two converges on the next run rather than
   silently losing ownership.
3. Keep a foreign file untouched on any `CONFLICT`; surface it for explicit
   resolution.

The planner alone cannot prevent filesystem races. Between the plan and the
mutation another writer can change the file; only the caller's atomic
compare-and-apply closes that window.

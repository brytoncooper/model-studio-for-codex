# Development guard and verification entrypoints

## Purpose

B00 makes isolated architecture work safe beside a live Model Deck install. The guard refuses default bundle output, live Application Support, `~/.codex`, and user-created symlink aliases. Verification commands require explicit temporary `state_root` and `artifact_root` values before running gate logic.

## Invariants

- Protected paths include sibling `Model Deck.app` build output, `~/Library/Application Support/Model Deck` (and legacy names), `~/.codex`, and `/Applications/Model Deck.app`.
- `state_root`, `artifact_root`, and optional `socket_root` must be absolute directory paths, pairwise disjoint, outside protected locations, and must not be overly broad ancestors of protected data.
- macOS `/tmp` → `/private/tmp` is a legitimate temp alias; user-created parent symlink aliases are rejected.
- Worker editing checks use `scripts/editing_check.py --files <exact test paths>` only (at most three `test_*.py` files, no forwarded commands).
- The editing wrapper uses a hard 30 second deadline including cleanup reserve, bounded nonblocking output reads, allowlisted environment variables, and process-group termination.
- Timeout or output-cap results are **inconclusive** (exit 4): defer to the finalizer; do not narrow scope or retry batches to evade limits.
- Validation mistakes (globs, wrong modules, too many files) are unsupported (exit 2).

## Contracts

| Surface | Responsibility |
| --- | --- |
| `development.guard.validate_isolated_roots` | Reject unsafe or colliding roots |
| `development.guard.reject_source_tree_usage` | Reject source checkout roots; allow only `work/worktrees` and `work/isolated` under the active repo |
| `development.guard.env.isolated_subprocess_env` | Allowlisted HOME/state/artifact env for verify and editing checks |
| `development.inventory.baseline_record` | Record Python/Swift self-test inventory without changing tests |
| `scripts/editing_check.py` | Bounded worker unittest runner (not finalizer-approved until G0 passes) |
| `scripts/verify.py` | Gate entrypoint documented in `docs/plans/plugin-architecture/VERIFICATION.md` |
| `scripts/recipe_isolated_worktree.sh` | Print isolated worktree and environment recipe |

## Verify and troubleshoot

Print an isolated worktree recipe:

```bash
zsh scripts/recipe_isolated_worktree.sh my-slice
```

Run the G0 gate with disjoint temporary roots (finalizer-owned):

```bash
python3 scripts/verify.py development-guard \
  --state-root /tmp/model-deck-state \
  --artifact-root /tmp/model-deck-artifacts
```

Bounded worker check (after finalizer accepts the wrapper):

```bash
python3 scripts/editing_check.py --files test_development_guard.py
```

Common outcomes:

- Exit **2** — unsupported worker invocation (fix arguments).
- Exit **4** — inconclusive (timeout/output cap/busy): stop and defer to the finalizer; do not split or retry the same batch to evade limits.
- `verify: gate package status=pending (B26)` — `all-local` ran G0–G6; the package gate is not implemented yet.

## Parent links

- [Architecture plan](../docs/plans/plugin-architecture/PLAN.md)
- [Verification gates](../docs/plans/plugin-architecture/VERIFICATION.md)
- [Implementation backlog](../docs/plans/plugin-architecture/BACKLOG.md)

## Extend the checks

Add a fixed gate to `scripts/verify.py` only after its isolated fixture suite exists. Pass the validated roots and allowlisted environment through to every child. Keep unfinished gates explicitly unavailable; passing one gate cannot imply the others passed. Add negative path or process fixtures when changing guard behavior, and obtain finalizer acceptance before widening the worker wrapper.

The legacy `build.sh` ignores the recipe's proposed `MODEL_DECK_APP_OUTPUT` variable. Do not run it for isolated architecture work; staged packaging will have a separate implemented command.

# Architecture import checker (B03)

Static AST checks for the planned `python/src/model_deck/` dependency graph. The checker inspects import statements and a small set of dynamic-import and service-locator patterns. ## Kernel/engine stdlib allowlist

Kernel and engine may import only:

- `model_deck_contracts` and other architecture-managed `model_deck.*` targets (layer rules apply)
- stdlib roots listed in `forbidden.py` (`KERNEL_ENGINE_STDLIB_ALLOWLIST`)
- explicit forbidden roots (platform, network SDKs, routers, etc.) are always rejected

The allowlist is a **reviewable static import boundary**. It does not prove that allowed stdlib modules cannot perform I/O or other effects at runtime, and it does not catch indirect imports, reflection, or dynamic loads except where `dynamic_import` rules apply.

Networking stdlib such as `http` is intentionally **not** on the allowlist; use adapters/integrations for HTTP clients.

It does **not** prove runtime sandboxing, side-effect purity, or that code cannot reach forbidden modules indirectly.

## Entrypoint

```bash
python3 scripts/architecture_check.py --roots python/src
```

Optional flags:

- `--include-baseline-legacy` — also scan repo-root modules listed in `baseline_legacy.json` (report-only entries emit warnings until `enforce` is true).
- `--fail-on-warnings` — treat baseline warnings as failures.

## Layer rules (summary)

| Layer | May import |
| --- | --- |
| kernel | contracts, kernel |
| engine | contracts, kernel, engine (public cross-feature via ports) |
| adapters | contracts, engine, adapters |
| integrations/providers/hosts/clients | contracts, engine, same integration family |
| bootstrap | concrete implementations across layers |
| plugins | contracts and plugin SDK surface only |

Additional rules:

- Kernel and engine reject listed platform/product roots (`fcntl`, `sqlite3`, router modules, Cursor/Codex runtime modules, etc.).
- Providers must not import router authority modules (`local_router`, `routing_registry`, `provider_bridge`).
- Cross-feature private imports inside `model_deck.engine.*` are rejected.
- Dynamic imports require an explicit entry in `dynamic_import_allowlist.json`.
- Service locator calls (`get_service`, `resolve_implementation`, …) are rejected outside bootstrap/legacy report-only files.
- Legacy root modules are **named individually** in `baseline_legacy.json`; there is no blanket repo-root ignore.

## Extend the rules

1. **New layer or package prefix** — update `graph.py` (`layer_for_module`, `ALLOWED_LAYER_IMPORTS`).
2. **New forbidden platform/product root** — append to `forbidden.py` (`KERNEL_ENGINE_FORBIDDEN_ROOTS`).
3. **Loader/composition exception** — add module pattern or file glob to `dynamic_import_allowlist.json` and a positive fixture under `fixtures/positive/`.
4. **Legacy ratchet** — add or flip `enforce` in `baseline_legacy.json` when a root module migrates into `python/src/`.
5. **Regression tests** — add a fixture under `fixtures/negative/` or `fixtures/positive/` and assert rule ids in `test_architecture_check.py`.

When the B02 vertical path lands, point `--roots python/src` at the real package tree. Acceptance still waits on that graph; this checker can run earlier against fixtures and partial trees.

## Parent links

- [BOUNDARIES.md](../../docs/plans/plugin-architecture/BOUNDARIES.md)
- [VERIFICATION.md](../../docs/plans/plugin-architecture/VERIFICATION.md) (architecture rejection tests)
- [BACKLOG.md](../../docs/plans/plugin-architecture/BACKLOG.md) (slice B03)

# Verification, readiness and protected-runtime rules

> Current scope: [V2 starts fresh](V2-SCOPE.md). Prototype saved-state import
> and legacy-setup compatibility are not delivery requirements. Keep the
> prototype running solely to preserve development tool access.

Status: planned checks. No application test, build, installation, migration or live provider qualification was run to author this plan. Requested Composer planning agents used the existing routing toolchain; those planning turns are not product conformance evidence. Previously observed Python/Swift test results are historical baseline evidence, not proof of this new architecture.

## 1. Verification ownership and commands

One independent finalizer owns aggregate evidence; the lead owns final diff review. Implementation workers use only a repository-approved Editing wrapper on <=3 exact owned files with a 30-second total deadline including children and bounded output. The wrapper does not yet exist. Timeout/busy/unsupported is inconclusive, not pass; do not split or retry batches to evade limits. After two failed repairs, consolidate diagnosis with one capable owner and return to the same finalizer.

The entrypoint is `python3 scripts/verify.py <gate> --state-root <temporary> --artifact-root <temporary> [--socket-root <temporary>]` with the fixed supported gates below. It validates the temporary roots before executing any code; the roots must be absolute, disjoint, outside the source tree and free of user-created symlink parent aliases (the macOS `/tmp` → `/private/tmp` and `/var` → `/private/var` aliases are the only permitted ones). No arbitrary command forwarding. G0–G6 are implemented; G7 `package` stays pending B26 and `all-local` reports it as pending rather than passing it. Counts are discovered from the tree at run time, never hard-coded.

### Environment contract

Every Python gate runs from `python/` with the engine reached through `PYTHONPATH=<repo>/python/src` and **no installed or editable `model-deck` distribution**. The process-isolation fixtures (external extension transport, `tests/plugins/*`, the notebook update tests and the `examples/session-notebook` worker) spawn `sys.executable -I -c ...` children and refuse to run when `importlib.util.find_spec("model_deck")` is not `None`; that refusal is the B18 acceptance proof that external code runs without private engine imports, so an editable install turns it into `malformed_eof` child failures rather than a weaker test. `python/pyproject.toml` therefore declares `[tool.uv] package = false`, so `uv sync --project python` installs only `jsonschema[format]==4.23.0` and `tomlkit==0.13.3`. No gate requires pytest; every Python gate uses the stdlib `unittest` runner. Run `scripts/verify.py` with that environment's interpreter (`python/.venv/bin/python`), because every gate child is spawned as `sys.executable` and needs the two runtime dependencies. The gates also need a symlink-free temporary directory, because the Codex migration-preview fixtures reject a fixture root containing a symlink: the runner sets `TMPDIR` to the real path of the isolated temporary directory produced by `isolated_subprocess_env`, and `python/tests/__init__.py` repeats both checks at import (raising with the remediation command `uv pip uninstall --python python/.venv/bin/python model-deck`). G2, G4, G5 and G6 run the same pre-flight first and exit 2 with that remediation text when the interpreter can still see the engine.

### Exact command per gate

| Gate | Command | What it runs |
|---|---|---|
| G0 | `python3 scripts/verify.py development-guard --state-root <s> --artifact-root <a>` | `python -m unittest test_development_guard test_editing_check` from the repository root |
| G1 | `python3 scripts/verify.py contracts --state-root <s> --artifact-root <a>` | `python scripts/generate_contracts.py --check` |
| G2 | `python3 scripts/verify.py engine --state-root <s> --artifact-root <a>` | one `python -m unittest` subprocess per directory over `tests/engine`, `tests/kernel`, `tests/contracts`, `tests/scripts`, `tests/host_codex`, `tests/integrations`, then `python scripts/architecture_check.py` (section 2) |
| G3 | `python3 scripts/verify.py swift --state-root <s> --artifact-root <a>` | `swift test --package-path macos` (`shutil.which("swift")`, else `/usr/bin/swift`); a missing toolchain is recorded explicitly as `status=unavailable` and exits 3 rather than passing |
| G4 | `python3 scripts/verify.py migration --state-root <s> --artifact-root <a>` | the modules under `tests/engine` and `tests/integrations` whose filename contains `repository`, `outbox`, `projection`, `migration`, `recovery`, `schema` or `upgrade`; the selected list is printed so it can be audited, and overlap with G2 is intentional |
| G5 | `python3 scripts/verify.py providers --state-root <s> --artifact-root <a>` | `tests/provider_openai_compatible`, `tests/provider_cursor`, `tests/providers_continuation` |
| G6 | `python3 scripts/verify.py extensions --state-root <s> --artifact-root <a>` | `tests/plugins`, then every `examples/*/tests` directory that exists, each run from its own example directory |
| G7 | pending B26 | not implemented; `all-local` prints it as pending |
| all-local | `python3 scripts/verify.py all-local --state-root <s> --artifact-root <a> --socket-root <k>` | G0, G1, G2, G3, G4, G5, G6 in that order, every gate run even after a failure, then the summary table |

Each gate prints one summary line — gate name, tests run, failures, errors, seconds — and `all-local` repeats them as a table before exiting nonzero if any gate failed. Unix socket paths must stay short, so keep the roots shallow, for example `--state-root /private/tmp/md-gate/state --artifact-root /private/tmp/md-gate/artifacts --socket-root /private/tmp/md-gate/sockets`.

| Gate | Subcommand | Required evidence |
|---|---|---|
| G0 | `development-guard` | Protected path/symlink refusal, worker wrapper boundaries, fixture child timeout cleanup |
| G1 | `contracts` | JSON schemas, valid/invalid examples, Swift/Python round-trip, version compatibility and import-negative fixtures |
| G2 | `engine` | Headless fixture CLI, operation consistency across clients, state transitions, scoped dispatch, no host launch |
| G3 | `swift` | Swift package tests/compilation, presenter five-state tests, window geometry and staged UI render inspection |
| G4 | `migration` | V2 authority/outbox recovery, CAS conflicts, schema-version bookkeeping, transactional upgrades, newer-schema refusal and V2 data recovery; prototype import excluded |
| G5 | `providers` | Shared provider conformance and legacy parity; fake HTTP/SDK/host only |
| G6 | `extensions` | Separate archive/SDK installation, manifest rejection, lifecycle/quotas/grants, Notebook and other-language fixture |
| G7 | `package` | Immutable staged output, signed helper identity, resource inventory, no private source import requirement |
| G8 | separate scheduled qualification | Actual installed host/provider/macOS behavior; explicit operational authorization required |

G0–G7 are intended isolated local/CI checks. They are not automatically harmless before the protected-path guard exists. Fixture substitution proves architectural independence; it is not qualification of an unimplemented host or operating system. `all-local` runs the implemented gates once after final integration; unavailable platform/tool checks are recorded explicitly instead of being counted as passes.

## 2. Architecture rejection tests

Positive examples alone do not prove a boundary. Include deliberate invalid fixtures and assert failure with useful file/import diagnostics:

- Kernel imports a provider SDK, product feature, Codex type, OS credential implementation or AppKit.
- Engine imports `fcntl`, Darwin, a concrete SQLite adapter, local socket implementation or any concrete OS API.
- External plugin imports a private engine implementation or registers another namespace's operation.
- Swift presenter constructs a process/network client or imports provider DTOs.
- MCP or UI bypasses use cases and writes connection state directly.
- A capability fetches another implementation through a global service locator.
- Permission/schema changes silently broaden an existing descriptor.

Import enforcement is limited evidence. Pure rules also get deterministic clock/UUID fixtures and tests that avoid filesystem/network side effects. Public interfaces may expose values and documented handles, not adapters disguised as `Any`/untyped dictionaries.

## 3. Behavior preservation matrix

| Existing behavior | Required preserved proof |
|---|---|
| Managed model registration and route identity | Old fixture imports retain provider/account/model identity and aliases; unknown/foreign files untouched |
| ChatGPT versus API/Cursor billing | Host subscription context never exported as arbitrary endpoint auth; unknown prices/costs remain unknown |
| Model catalog and capability metadata | Exact IDs, provenance/age, unknown support, stale response guards and reserved host aliases |
| Responses/chat streams | Ordered deltas, terminal event required, malformed/truncated tool streams fail |
| Cursor harness | Tool suspension/result identity, Fast/reasoning validation, cancel, SDK subprocess isolation |
| Continuation | Cross-account/model/provider refusal or explicit stripping, private signatures retained only in scope |
| Compaction | Existing trigger and unary paths; failed summaries never installed; host opaque content handled explicitly |
| Agent tools | Host remains approval/execution owner; no automatic extension tool injection |
| Usage | Subscription allowance versus estimates/settled costs; stale good values retained; no key/content logging |
| Companion | Permission absent/revoked, minimum size, full screen/display/focus, lost host, resize recovery, shutdown |
| Packaging | Credential helper exact identity, Python/resource completeness, V2-required entrypoint behavior |

Map existing named tests to these outcomes before moving files. Add tests only for new contracts, missing edge cases or changed boundaries; avoid duplicate tests that merely mirror new wrapper functions.

## 4. External extensibility acceptance

A clean external project, with the private engine package absent, must be able to:

1. Validate and package a manifest and declared schemas without executing plugin code.
2. Install and activate through the public CLI/API against a staged engine.
3. Register a genuinely new operation, panel and event under its own namespace.
4. Store owned user notes and execute a cancellable export job.
5. Handle unavailable optional session metadata without breaking basic notes.
6. Fail attempts at unauthorized broker calls and cross-plugin storage access.
7. Survive disable/re-enable and successful/failed updates with user data preserved.
8. Expose operations to a generic CLI with no plugin-specific shell/core changes.
9. Implement the protocol in a second language.
10. Produce the same relevant conformance results as a built-in implementation.

Review a git diff proving the sample feature did not require kernel/engine/shell source changes. If it did, classify whether the public contract was incomplete and repair/freeze it before declaring SDK v1 ready.

## 5. Cross-platform and host acceptance

Platform adapter tests cover private data paths, locks, atomic replacement semantics, IPC permissions, child ownership/cancellation, credential references and startup discovery. Keychain/AX, POSIX locks and other concrete platform APIs must not appear in engine imports. The engine can start with a fixture platform, no Codex registration directory and no native UI installed.

Agent host adapters expose capabilities rather than assuming all hosts support catalog injection, model replacement, tool callbacks, transcript export or attached windows. The existing Codex adapter gets a compatibility profile; a small synthetic host proves that core operations do not rely on it. Unsupported host capabilities fail explicitly. MCP integration alone does not prove replacement of the host's inference model or compatibility with Cursor's harness.

No alternate OS app, new host integration or live non-macOS qualification is part of this plan. Demonstrate that replacing fixtures/adapters requires no engine changes. Attached-window behavior is optional: lack of attachment must not prevent a standalone client. A future port still needs its own implementation and platform evidence.

## 6. Operational cutover guard

The user explicitly prohibited rebuilding or killing the active tool-providing application. That prohibition persists through planning and ordinary local implementation. A Git backup is not permission to disrupt runtime state.

Before G8, present exact target bundle/state paths, current-to-new version, recovery artifact, state snapshot method, process impact and how agent/tool access survives. Schedule the cutover separately. Never kill by name, overwrite a discovered active bundle, mutate its vendored resources, or alter SDK/global credentials during isolated verification. Compare source and artifact bytes without treating source state as installed proof.

Qualification records distinguish: implemented, locally verified, packaged/signed, installed, provider-qualified, host-version-qualified, committed, pushed and deployed. A failed/unavailable gate cannot be relabeled pass because a related gate succeeded.

## 7. Human documentation acceptance

Every slice updates its owned human guide as specified in [DOCUMENTATION.md](DOCUMENTATION.md). G1/G7 check subsystem coverage, live local links, catalog completeness and authoritative contract references. Editorial review checks purpose, invariants and extension recipes, not merely file existence. Root README must explain why Model Deck, supported current behavior and reader paths; the plugin quickstart runs from a fresh external project. Missing documentation or contradictory contracts block slice acceptance.

## 8. Planning artifact review (this task)

The plan finalizer reviews PLAN/API/BACKLOG/VERIFICATION together, tests dependency order and coverage, checks Markdown local references/anchors where practical, verifies only documentation files changed, and flags contradictory assertions in raw research. It must not execute app code or planned commands. Review findings and disposition belong in REVIEW.md. This establishes planning quality only; it cannot certify implementation, platform feasibility or provider behavior.

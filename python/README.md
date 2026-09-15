# Model Deck engine

The Python package contains the headless engine, its public contracts and the
adapters that connect it to storage, hosts and providers. The native Mac interface
uses those boundaries; provider execution is injected when the engine is composed.

## Environment contract

Every test module and every verification gate runs against an interpreter that
cannot import `model_deck` on its own: the engine is reached through
`PYTHONPATH=src`, never through an installed or editable distribution. The
process-isolation fixtures spawn `sys.executable -I -c ...` children and assert
that external extension, provider and notebook code runs with private engine
imports unavailable; an editable install puts `model_deck` back on the isolated
child's path and turns that acceptance proof into `malformed_eof` child
failures. The same contract needs a symlink-free temporary directory, because
the Codex migration-preview fixtures refuse a fixture root that resolves
through a symlink (the macOS default `/var/folders` resolves through
`/private`). `python/tests/__init__.py` enforces both: it fails the import with
a remediation command when `model_deck` is visible under `-I`, and pins
`tempfile.tempdir` and `TMPDIR` to the real path of the temporary directory so
child processes inherit it. `scripts/verify.py` applies the same pre-flight and
the same environment before each gate.

## Develop

Use Python 3.11 or newer. `[tool.uv] package = false` keeps the project itself
out of the environment, so `uv sync` installs only the two runtime dependencies
and leaves `model_deck` invisible to the isolated fixture children:

```sh
uv sync --project python
PYTHONPATH=python/src python/.venv/bin/python -m model_deck.cli.main --help
```

Without uv, create the virtual environment and install the same two runtime
dependencies — never the project itself:

```sh
python3 -m venv python/.venv
python/.venv/bin/python -m pip install "jsonschema[format]==4.23.0" "tomlkit==0.13.3"
PYTHONPATH=python/src python/.venv/bin/python -m model_deck.cli.main --help
```

Do not run `pip install -e ./python`. The `model-deck` console script exists
only in a packaged install; during development invoke the same entry point as
`python -m model_deck.cli.main`.

The package includes its JSON schemas. Runtime validation must use the packaged
`model_deck_contracts` API, not discover a source checkout or read its `contracts/`
folder. Contract-generation tools own synchronization of the bundled copies.

## Command line

- `model-deck engine serve` starts an engine with explicit state, artifact, socket
  and legacy-agent fixture paths. Run `model-deck engine serve --help` for the
  required arguments and optional application-state/fixture-run switches.
- `model-deck models list` reads registered models from an authenticated engine.
- `model-deck invoke <operation>` discovers and invokes an advertised operation,
  including operations added without changing the CLI.
- `model-deck runs fixture-text` exercises the deterministic fixture run path.

Client commands take explicit `--rendezvous` and `--credential` paths. Generic
invocation optionally reads a JSON object from `--input-file`; it prints the
result as JSON and returns a nonzero exit status on failure. It does not retry a
mutation automatically. See the [CLI guide](src/model_deck/cli/README.md).

Engine roots must pass the isolated-root guard. Development fixtures must not
point at live Codex settings, credentials or application data.

## Compose the engine

`build_engine_server(...)` returns an `EngineRuntime` containing the server,
rendezvous path, enrollment result and optional application database path.
Application-state mode connects revisioned model and connection repositories.
Supplying both `provider_execution` and `provider_route_definitions` also composes
durable sessions, runs, recovery and usage reconciliation. The caller owns
provider cleanup. The [runs guide](src/model_deck/engine/runs/README.md) explains
that boundary.

`kernel_composition` adds operations through the shared kernel registry. Its
handlers remain subject to schema validation and grants after authentication.
The [system catalog](../docs/architecture/CATALOG.md) links the individual
contracts, implementations, tests and extension instructions.

## Verify a change

From `python/`, with the non-editable environment above, run the relevant test
module against the source tree:

```sh
cd python
PYTHONPATH=src .venv/bin/python -m unittest tests.engine.test_provider_bootstrap
```

`PYTHONPATH=src` is the only supported way to reach the engine from a test: the
process-isolation fixtures require `model_deck` to be absent from the
interpreter itself. Importing `tests` refuses to proceed otherwise and prints
the remediation command.

Whole gates run through the verification entrypoint, which sets the same
environment (and a symlink-free `TMPDIR`) for every child:

```sh
python3 scripts/verify.py engine \
  --state-root /private/tmp/md-gate/state \
  --artifact-root /private/tmp/md-gate/artifacts
```

Use temporary databases, sockets and deterministic providers. A focused passing
test proves its covered behavior; it does not qualify the packaged app or a live
provider. The [delivery checklist](../docs/plans/plugin-architecture/STATUS.md)
records remaining integration and qualification work.

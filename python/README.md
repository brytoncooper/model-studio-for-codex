# Model Deck engine

The Python package contains the headless engine, its public contracts and the
adapters that connect it to storage, hosts and providers. The native Mac interface
uses those boundaries; provider execution is injected when the engine is composed.

## Develop

Use Python 3.11 or newer. From the repository root, install into a virtual
environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ./python
model-deck --help
```

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

From `python/`, with the virtual environment active, run the relevant test module:

```sh
PYTHONPATH=src python -m unittest tests.engine.test_provider_bootstrap
```

Use temporary databases, sockets and deterministic providers. A focused passing
test proves its covered behavior; it does not qualify the packaged app or a live
provider. The [delivery checklist](../docs/plans/plugin-architecture/STATUS.md)
records remaining integration and qualification work.

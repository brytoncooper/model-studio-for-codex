# Model Deck Python engine (B02)

Headless `engine.v1` slice: private Unix socket transport, two-step `hello`, and `models.list` over a read-only legacy Codex agent fixture port.

## Commands

- `model-deck engine serve --state-root <abs> --artifact-root <abs> --socket-root <abs> --legacy-agents-dir <abs> [--catalog-cache <abs-json-fixture>]`
- `model-deck models list --rendezvous <abs> --credential <abs>`

State, artifact, and socket roots must pass packaged isolated-root validation (`model_deck.adapters.platform.macos.isolated_roots`). No live `~/.codex` or Application Support paths.

## Layout

- `src/model_deck/engine/model_library/` — `ModelRepository` port and `ListModelsUseCase`
- `src/model_deck/adapters/transport/` — newline-framed JSON-RPC over Unix domain sockets
- `src/model_deck/integrations/hosts/codex/legacy_models.py` — fixture-only managed TOML reader


`build_engine_server` returns an `EngineRuntime` handle with `server`, `rendezvous_path`, and `enrollment`. Catalog listing requires `--catalog-cache`; without it, `collection=catalog` returns an explicit unavailable error (no fallback).

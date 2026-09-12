# ModelDeckClient

Typed `engine.v1` JSON-RPC client, catalog service adapters, and bootstrap selection between the B02 engine path and the legacy `codex_settings.py` catalog bridge.

## Bootstrap

- Default: `LegacyModelCatalogService` when `MODEL_DECK_ENGINE_RENDEZVOUS` is unset.
- Engine: set `MODEL_DECK_ENGINE_RENDEZVOUS` to B02 `rendezvous.json` with exactly `transport`, `socket_path` (absolute), `engine_instance_id`, `instance_nonce`, and `api_profile` (`major`/`minor` compatible with engine 1.0).
- Credential: `MODEL_DECK_ENGINE_CREDENTIAL` or sibling `operator_credential` beside the rendezvous file. The rendezvous file never contains credentials.
- Misconfiguration returns `UnavailableModelCatalogService`; there is no hidden legacy fallback when the engine env is set.

## Transport framing

`EngineTransport.receiveFrame()` returns one complete decoded JSON payload (newline-delimited framing is handled inside transports such as `UnixSocketEngineTransport`). Encoded outbound frames are capped at 1 MiB.

## Handshake

Two-step `engine.v1.hello` verifies `engine_instance_id` and `instance_nonce` against the rendezvous file before reading the operator credential. The authenticated hello must return the same instance identity and `api_profile` as the rendezvous descriptor.
## Catalog cancellation

`EngineModelCatalogService.cancel()` snapshots the in-flight client under `NSLock` and calls `cancelInFlight()` without `workerQueue.sync`, so a blocked socket read cannot deadlock cancellation. Each `loadCatalog` request id delivers exactly one terminal completion; superseded or cancelled work finishes with `.cancelled`.

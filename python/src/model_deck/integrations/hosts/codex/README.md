# Codex host integration

This package owns Codex-specific host behavior. The application and engine own
model records, provider routes, public operations, persistence, and projection
receipts; this package translates those contracts to Codex without becoming a
second authority.

## Public seams

- `CodexHostAdapter` implements the application-owned `HostIntegrationPort`.
  It discovers an injected Applications directory, reports compatibility, and
  prepares an app-server command without starting a process.
- `AppServerBridge` maps Codex app-server JSON-RPC methods, events, catalog
  rows, configuration overlays, approvals, tool results, and cancellation to
  injected catalog/router ports.
- `runtime.py` owns typed bundle discovery, protocol classification, capability
  status, and pure launch argument assembly.
- `provider_bridge.py` remains the legacy entrypoint wrapper. It supplies the
  existing root catalog/router implementations to the extracted package.
- [projection composition](projection_composition/README.md) independently
  delivers committed model and connection state to an isolated Codex home.

`build_engine_server(host_integration=...)` publishes the frozen
`engine.v1.hosts.list` and `engine.v1.hosts.prepare` operations only when an
adapter is injected. The engine validates their request/result envelopes; the
adapter owns discovery, compatibility, and launch preparation.

## Invariants

- Discovery and tests use caller-supplied roots. No adapter test reads the live
  Applications directory, process table, credentials, or Codex state.
- Preparation never launches Codex and never rewrites global configuration.
  Model Deck MCP and provider settings remain per-process `-c` overrides.
- Bundle version is descriptive. Compatibility uses an independently observed
  app-server protocol version and explicitly refuses unknown or unsupported
  versions.
- Capability is `available`, `already-running-unverified`, or `incompatible`.
- Host-bound `gpt-*` subscription routing remains in host context and does not
  enter the generic HTTP-provider adapter.
- The bridge forwards approvals and tool results unchanged. It cancels the
  matching routed turn only after Codex accepts the interruption.

## Extending the adapter

Add host-specific behavior behind these injected ports. Extend the frozen
engine contracts only when a real consumer requires new public information.
Do not import application storage, provider implementations, or root-private
composition into this package.

## Focused verification

From the repository root:

```sh
PYTHONPATH=python/src /tmp/md-b18-venv/bin/python -B -m unittest \
  python/tests/engine/test_host_operations.py \
  python/tests/engine/test_host_bootstrap.py \
  python/tests/integrations/hosts/codex/test_host_adapter.py \
  python/tests/integrations/hosts/codex/test_runtime.py \
  python/tests/integrations/hosts/codex/test_app_server.py \
  test_provider_bridge.py test_codex_runtime.py -q
```

The fixtures create a fake app bundle and app-server streams. They verify
public socket composition, all compatibility states, unknown-version refusal,
catalog projection, event/method parity, cancellation ownership, approvals,
per-process overrides, and host-bound subscription behavior.

## Limitations

This acceptance does not launch, attach to, or reload Codex Desktop. It proves
the independently testable adapter and preparation boundary. Actual Desktop
discovery/attachment/model reload remains a separate, authorized live
qualification and must not be inferred from fake-server, CLI, or projection
file evidence.

# Model Deck subsystem catalog

Concise index of actually existing systems. Every link resolves to an existing guide. Unwritten guides are marked as text, never linked.

## Kernel

| Subsystem | Guide |
|---|---|
| Kernel registry (`kernel/registry.py`): feature descriptors, dependencies and operation registration | [kernel guide](../kernel.md) |

## Engine features

| Subsystem | Guide |
|---|---|
| Connections: connection records port, list/save use cases, revision and idempotency conflicts | [connections README](../../python/src/model_deck/engine/connections/README.md) |
| Sessions: session records bound to a model registration (`sessions.create/get/select_model`) | [sessions README](../../python/src/model_deck/engine/sessions/README.md) |
| Routing port: resolves a registration to the provider/connection/model snapshot; concrete resolvers live in adapters | [routing port README](../../python/src/model_deck/engine/routing/README.md) |
| Model library: registered-model read port, catalog-cache port, `models.list` use case | [model library README](../../python/src/model_deck/engine/model_library/README.md) |
| Runs: application port and use cases for model runs bound to a session and route (`runs.start/get/cancel/submit_tool_result`) | [runs README](../../python/src/model_deck/engine/runs/README.md) |
| Usage records: exact engine event recording, duplicate handling and bounded chronological queries | [usage README](../../python/src/model_deck/engine/usage/README.md) |
| Host settings engine: generic settings engine owning authorization, schema validation, preview-token binding, save idempotency receipts; never touches filesystem/network/Git | [host settings README](../../python/src/model_deck/engine/host_settings/README.md) |

### Plugin jobs and authority

| Subsystem | Guide |
|---|---|
| Model runs: live model-run lifecycle bound to session and route (see Runs above) | [runs README](../../python/src/model_deck/engine/runs/README.md) |
| Plugin jobs state slice: durable job STATE repository only (queued/running/completed/failed/cancelled/interrupted, claim, progress, terminal-once, checkpoints) | [jobs state README](../../python/src/model_deck/engine/jobs/README.md) |
| Plugin job broker: supervisor-side dispatch over settled authority plus committed job repository; wire shapes mirror frozen `plugin.v1/broker/jobs.*` | [job broker guide](../../python/src/model_deck/engine/jobs/BROKER.md) |

### Plugin authority, data, events

| Subsystem | Guide |
|---|---|
| Plugin invocation authority: shared B19 authorization decision used by the data, job, and event brokers. Not install/enable state, not transport auth, not lifecycle | [authority README](../../python/src/model_deck/engine/plugin_authority/README.md) |
| Plugin data repository: revisioned per-plugin key/value store behind `plugin.v1/broker/storage.*`; not an authorization gate | [data README](../../python/src/model_deck/engine/plugin_data/README.md) |
| Plugin data broker: supervisor-side dispatch over settled authority plus committed data repository | [data broker guide](../../python/src/model_deck/engine/plugin_data/BROKER.md) |
| Plugin event broker: in-memory publish/subscribe for plugin activations over exact registered descriptor event IDs | [events README](../../python/src/model_deck/engine/plugin_events/README.md) |

### Projections: host file synchronization

| Subsystem | Guide |
|---|---|
| Projection outbox read seam: read-only poll of the existing `projection_outbox` table in stable order; producers, receipts, and schema untouched | [projections README](../../python/src/model_deck/engine/projections/README.md) |
| Projection policy: pure filesystem-free decision planner (`plan_projection`); a plan, not an action | [projection policy README](../../python/src/model_deck/integrations/hosts/codex/projection_policy/README.md) |
| Codex projection consumer: consumes `registered_model.upserted/removed` outbox events into consumer-owned projection files via intents, conditional file ops, and receipts | [projection consumer README](../../python/src/model_deck/integrations/hosts/codex/projection_consumer/README.md) |
| Codex agent materializer: turns one supported upserted event into the owned agent-file write | [agent materializer README](../../python/src/model_deck/integrations/hosts/codex/agent_materializer/README.md) |
| Codex agent renderer: pure construction of managed-agent TOML; rendering only, no filesystem/network/credentials | [agent renderer README](../../python/src/model_deck/integrations/hosts/codex/agent_renderer/README.md) |

## Adapters (storage, events, routing, filesystem)

| Subsystem | Guide |
|---|---|
| Live run event replay: in-memory live fan-out and short replay window per run; engine owns durability | [events adapter README](../../python/src/model_deck/adapters/events/README.md) |
| Fixture conditional projection files: `ConditionalProjectionFiles` over an explicit on-disk fixture root | [filesystem adapter README](../../python/src/model_deck/adapters/filesystem/README.md) |
| Registered route resolution: joins model repository, connection repository, and route-definition table; no I/O | [routing adapter README](../../python/src/model_deck/adapters/routing/README.md) |
| Adapters overview | [adapters README](../../python/src/model_deck/adapters/README.md) |
| Host-settings storage adapter | [storage HOST_SETTINGS](../../python/src/model_deck/adapters/storage/HOST_SETTINGS.md) |
| SQLite storage adapters (outbox, plugin jobs, plugin data implementations) | Guide not yet written |

## Hosts and providers

| Subsystem | Guide |
|---|---|
| Host settings dispatch (engine wiring) | [host settings dispatch](../engine/host-settings-dispatch.md) |
| Explicit settings composition and authenticated file persistence | [settings bootstrap](../engine/host-settings-bootstrap.md) |
| Codex settings document: pure TOML parse, lossless edits, known-field validation, protected-field checks; never finds or writes files | [settings document README](../../python/src/model_deck/integrations/hosts/codex/settings_document/README.md) |
| Codex settings file: filesystem persistence implementing `SettingsDocumentPort`; engine keeps authz, tokens, receipts | [settings file README](../../python/src/model_deck/integrations/hosts/codex/settings_file/README.md) |
| Legacy import preview: deterministic, redacted, read-only fixture-only preview of legacy routing state | [migration preview README](../../python/src/model_deck/integrations/hosts/codex/migration_preview/README.md) |
| Cursor provider guide | [Cursor provider](../providers/cursor.md) |
| OpenAI-compatible streaming boundary: decodes stream bytes, checks run-event ordering; no HTTP/credentials/history | [streaming README](../../python/src/model_deck/integrations/providers/openai_compatible/README.md) |
| OpenAI-compatible request translation: pure Responses-to-chat-completions translation | [translation guide](../../python/src/model_deck/integrations/providers/openai_compatible/TRANSLATION.md) |
| Provider continuation helpers: verbatim-behavior extracts for compaction and continuation translation | [continuation README](../../python/src/model_deck/integrations/providers/continuation/README.md) |

## Native client and presentation

| Subsystem | Guide |
|---|---|
| macOS app target (`ModelDeckApp`) | [app README](../../macos/Sources/ModelDeckApp/README.md) |
| macOS client target (`ModelDeckClient`) | [client README](../../macos/Sources/ModelDeckClient/README.md) |
| macOS platform target (`ModelDeckPlatform`) | [platform README](../../macos/Sources/ModelDeckPlatform/README.md) |
| macOS presentation target (`ModelDeckPresentation`) | [presentation README](../../macos/Sources/ModelDeckPresentation/README.md) |
| Native settings window and form/TOML controls | [settings window guide](../../macos/Sources/ModelDeckPresentation/HostSettingsWindow/README.md) |
| MCP client adapter: model-read orchestration converged onto `model_library` use cases | [MCP client README](../../python/src/model_deck/integrations/clients/mcp/README.md) |
| macOS host-settings service and wire notes | [HOST_SETTINGS_SERVICE](../../macos/Sources/ModelDeckClient/HOST_SETTINGS_SERVICE.md), [HOST_SETTINGS](../../macos/Sources/ModelDeckClient/HOST_SETTINGS.md) |

## Plugin system

| Subsystem | Guide |
|---|---|
| Plugin contracts (`contracts/plugin.v1/`) | [contracts README](../../contracts/README.md) |
| Manifest inspection: pure in-memory validation against the frozen manifest schema | [manifest inspection README](../../python/src/model_deck/plugins/manifest_inspection/README.md) |
| Lifecycle session: pure in-memory activation state (`created -> hello_verified -> active -> draining -> inactive`, plus `failed`); no transport/process | [lifecycle session README](../../python/src/model_deck/plugins/lifecycle_session/README.md) |
| Process runtime: bounded real-subprocess adapter driving lifecycle JSON-RPC over stdio | [process runtime README](../../python/src/model_deck/plugins/process_runtime/README.md) |
| Stdio codec: bounded newline-delimited UTF-8 JSON framing for private stdio pipes | [stdio codec README](../../python/src/model_deck/plugins/stdio_codec/README.md) |
| Archive inspection: in-memory ZIP entry summary primitive; no extraction/execution | [archive inspection README](../../python/src/model_deck/plugins/archive_inspection/README.md) |
| Artifact store: stages inspected ZIP bytes into a SHA-256-named directory; no trust/activation/install | [artifact store README](../../python/src/model_deck/plugins/artifact_store/README.md) |
| Plugin distribution/install flow beyond inspection plus staging | Guide not yet written |

## Contracts, development, planning

| Subsystem | Guide |
|---|---|
| Contracts overview | [contracts README](../../contracts/README.md) |
| Development workflow | [development README](../../development/README.md) |
| Architecture overview | [architecture README](../../development/architecture/README.md) |
| Plugin-architecture plan | [PLAN](../../docs/plans/plugin-architecture/PLAN.md) |
| Deterministic port guidance | [deterministic ports](../../python/src/model_deck/adapters/providers/DETERMINISTIC.md) |

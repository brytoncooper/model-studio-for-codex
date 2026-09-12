# Engine and plugin contract specification

Status: proposed v1 specification; freeze through backlog B01 before dependent implementation. [PLAN.md](PLAN.md) owns architecture. Numeric defaults below are initial limits to benchmark and version, not measured performance claims.

## 1. Three distinct surfaces

| Surface | Caller | Contract and responsibility |
|---|---|---|
| `engine.v1` | Swift, CLI, MCP/host adapters, granted plugin clients | Application operations and event subscriptions |
| `plugin.v1` | Kernel supervisor and extension worker | Handshake, activation, registered calls, cancellation and drain |
| Host/vendor adapters | Codex, HTTP providers, Cursor SDK | External protocols translated to application types; not SDK API |

The same schemas serve Python and Swift; the external protocol fixture demonstrates another language without prescribing a future client or platform. Use JSON-RPC 2.0 request/response semantics: numeric protocol error codes, exactly one result or error per request, and notifications have no response. Stream events use the JSON-RPC notification method `engine.v1.event` with params `{subscription_id,event}`; the event is an existing frozen application run event. Do not invent an incompatible `{ok,error}` JSON-RPC envelope. [JSON-RPC specification](https://www.jsonrpc.org/specification).

Engine transports: a LocalTransport port implemented by a private Unix socket on the current platform for shared local use; foreground stdio only for an isolated embedded engine. Transport is negotiated/discovered through a platform-neutral rendezvous descriptor; it is not part of application semantics. Plugin transport: private stdio pipes. Newline-delimited UTF-8 JSON, maximum 1 MiB encoded frame. Attachments larger than a frame use explicit scoped attachment handles and bounded chunk operations; never interpret client strings as unrestricted filesystem paths. Stdout carries protocol only; bounded diagnostics go to stderr. Batch JSON-RPC requests are explicitly unsupported in v1 and return a documented protocol error.

Every connection begins with version/capability negotiation. Select a mutually supported major/minor profile; unknown optional fields in compatible responses are ignored, unknown required capabilities reject negotiation, and mutation request schemas reject unknown fields. Events carry a versioned type; an unknown optional event may be ignored, but unknown state-changing events force an unsupported-protocol result. A new required field or changed meaning requires a major version. Defaults belong in schemas, not scattered UI logic.

## 2. Application-owned vocabulary

| Type | Fields and invariant |
|---|---|
| `Connection` | Opaque connection ID, provider ID, endpoint configuration reference, credential reference, revision; no secret value |
| `RegisteredModel` | Opaque registration ID, provider model ID, connection ID, display name, capability snapshot/provenance; identity is not its display label |
| `Capabilities` | Named feature → supported / unsupported / unknown, constraints and source/time; includes tools, media, compaction, resume, execution mode, reasoning/fast parameters |
| `Session` | Opaque ID, selected registration, host context reference, continuation scope, revision; no implicit provider switch |
| `Run` | Run ID, session ID, client request ID, selected route snapshot, state, timestamps and terminal result |
| `ToolCall` | Stable call ID, tool name/schema reference, arguments, host execution requirement; provider request is not approval |
| `UsageRecord` | Source/account/model/run, units, observed time, optional settled amount/currency and separate estimate; unknown stays null |
| `OperationDescriptor` | Namespaced ID, input/output schema IDs, required grants, effect class, optional job semantics, compatible version |
| `PanelDescriptor` | ID/title/component schema, authorized operation data bindings, metadata-only default subscriptions and required capabilities; plugin note bodies arrive only through its declared authorized read operation |

Provider IDs are stable identifiers, not URL inference. A connection may expose several models. Codex's model string can remain a compatibility alias while engine calls use registration IDs. Capability snapshots are refreshed explicitly and revalidated at invocation; stale catalog data cannot authorize unsupported Fast/tool behavior.

## 3. Public operations

All methods are prefixed `engine.v1.`. Names in this table are the initial public surface; do not add direct repository CRUD around every table.

| Method group | Operations | Mutation semantics |
|---|---|---|
| Discovery | `hello`, `health`, `operations.list`, `capabilities.get` | Read-only; disclose supported API/features |
| Model library | `models.list`, `models.register`, `models.rename`, `models.remove` | Expected revision and idempotency key for writes; removal affects future admission, not a running route snapshot |
| Connections | `connections.list`, `connections.save`, `connections.test` | Save is revisioned; test is explicit remote action, never called implicitly by list |
| Sessions | `sessions.create`, `sessions.get`, `sessions.select_model` | Selection allowed only without an active run; continuation scope reset/retained explicitly |
| Runs | `runs.start`, `runs.get`, `runs.cancel`, `runs.submit_tool_result`, `sessions.compact` | Start durable admission before provider dispatch; tool results bound to call/host; cancel is idempotent |
| Events | `events.subscribe`, `events.ack`, `events.unsubscribe` | Topic/field grants checked; bounded sequence cursor and flow control |
| Usage/evidence | `usage.query`, `catalog.refresh`, `benchmarks.query`, `benchmarks.refresh` | Cached reads; refresh explicit job with provider/network requirements |
| Hosts | `hosts.list`, `hosts.prepare`, `hosts.projection_status` | Prepare validates host compatibility; does not forcibly restart host |
| Extensions | `extensions.inspect`, `extensions.install`, `extensions.enable`, `extensions.disable`, `extensions.update`, `extensions.remove` | Admission, provenance, grant and lifecycle checks; inspect never executes code |
| Features/jobs | `operations.invoke`, `jobs.get`, `jobs.cancel` | Invoke only registered descriptor after schema/grant validation |

`models.list` distinguishes the registered library from cached provider catalog entries using an explicit collection selector (registered by default). Catalog queries may filter by connection, search text and bounded pagination, but never trigger network refresh; `catalog.refresh` remains the explicit refresh job. Unregistered catalog entries have provider model identity and capability provenance, not a fabricated registration ID. This preserves the native catalog browser and registered-model inventory as distinct views of the same application API.

Credential enrollment is a separate privileged client flow through the credential broker, producing a reference. Listing models, diagnostics, manifests, state exports and plugins receive no raw credentials. Provider adapters needing a token receive only their explicitly authorized connection's credential through an ephemeral broker channel, never argv or logs. A host-native subscription route additionally requires its host context and cannot be repurposed as a general API credential.

Example application request:

```json
{"jsonrpc":"2.0","id":"c17","method":"engine.v1.operations.invoke","params":{"operation":"org.example.notebook.notes.create","input":{"title":"Routing ideas","body":"Try a new provider"},"idempotency_key":"note-create-17"}}
```

Example domain error (numeric code mapping is fixed in B01):

```json
{"jsonrpc":"2.0","id":"c17","error":{"code":-32003,"message":"This extension cannot read session content.","data":{"code":"capability_denied","retryable":false,"request_id":"c17"}}}
```

Error vocabulary: `invalid_argument`, `unsupported_capability`, `capability_denied`, `not_found`, `conflict`, `version_mismatch`, `provider_unavailable`, `plugin_unavailable`, `rate_limited`, `deadline_exceeded`, `interrupted`, `resume_unavailable`, `resource_exhausted`, `projection_pending`, `internal`. Do not leak vendor error bodies/keys. Vendor diagnostic codes may be recorded only through a redacted adapter-owned diagnostic field. Retry advice never authorizes automatic billed retry.

## 4. Run state, tools, cancellation and replay

Run states: `accepted → running ↔ waiting_for_tool → completed | failed | cancelled | interrupted`. `cancelling` is an observable intermediate state reachable from any nonterminal state. Terminal transition is atomic; completion/cancel races produce exactly one terminal result. A cancel acknowledgement means request accepted, not provider confirmed stopped. Preserve that distinction in usage/status.

Events: `run.accepted`, `run.started`, `content.delta`, `tool.requested`, `usage.observed`, `run.cancelling`, `run.completed`, `run.failed`, `run.cancelled`, `run.interrupted`. Each includes run/session ID, sequence, event schema version and timestamp; content-bearing events require content grants. Tool requests contain stable call IDs; results are accepted only for outstanding calls from the session's authorized host. Repeating an identical result is idempotent; different result for the same call is conflict. A terminal provider event cannot silently close an unresolved tool call as success.

A headless client without a tool executor advertises tools unsupported. Built-in Codex bridge delegates approval/execution to Codex. Generic plugin operations are not automatically exported as model tools; an explicit MCP/host publication rule may expose approved operations later while preserving that host's approval contract.

Initial flow control: a run subscription has the single topic `run:<uuid>` and the authenticated principal must own that run. The server sends the subscribe response before any `engine.v1.event` notification for that subscription. Client response matching uses request IDs and queues interleaved notifications. Cumulative acknowledgements replenish credit only for newly acknowledged events. Cap each subscriber queue at 256 events and 1 MiB, whichever comes first. Content deltas are lossless until the cap; metadata refreshes may coalesce only where the schema declares that behavior. A slow subscriber is disconnected with a sequence/resume cursor; it must not block all sessions. Live replay buffer is limited to 8 MiB per run and 60 seconds, in memory only by default. If a required consumer remains disconnected beyond the negotiated grace interval, request cancellation; if the provider cannot confirm termination, report interrupted/unconfirmed termination rather than successful cancellation.

Run metadata and terminal result survive engine restart. Stream text does not persist merely to enable replay. `runs.get` reports current/terminal state; an expired replay cursor returns `resume_unavailable`, not regenerated tokens. Provider work is never restarted because a client reconnects. Admitted start requests are deduplicated by principal + operation + idempotency key and request hash for at least 24 hours; changed payload with the same key is conflict. After provider/engine crash, unknown dispatch outcome is interrupted, not a retryable new run, unless that adapter has a separately tested idempotent resume contract.

Provider-private continuations are not shared event payloads. Engine records an opaque reference keyed by connection, model, provider and execution mode. Compaction and resumed tool state follow that scope. Any move to another provider explicitly strips incompatible state or returns unsupported, preserving current safeguards.

## 5. Plugin manifest and activation

Illustrative manifest (B01 defines exact JSON Schema):

```json
{
  "manifest_version": 1,
  "id": "org.example.notebook",
  "version": "1.0.0",
  "plugin_api": {"major": 1, "minimum_minor": 0},
  "entrypoint": {"runtime": "python", "path": "plugin.py"},
  "permissions": ["storage.own", "jobs.own", "sessions.metadata.read"],
  "contributes": {
    "operations": [{"id": "org.example.notebook.notes.create", "input_schema": "schemas/create.json", "output_schema": "schemas/note.json", "effect": "write"}],
    "panels": [{"id": "org.example.notebook.panel", "schema": "panels/notebook.json"}]
  }
}
```

IDs use validated reverse-domain namespaces; a plugin may register only under its own ID. Package paths must remain within the extracted root; reject traversal, symlinks escaping the root, duplicate IDs, invalid schemas, unbounded archives and incompatible runtimes before executing any plugin code. Dependencies declare exact compatible API ranges, resolve deterministically, and reject cycles. Optional dependencies disable only dependent contributions. Dependency installation is explicit; no arbitrary package-manager hooks at activation.

The supervisor sends `plugin.v1.hello` with offered versions and a nonce. The worker returns its identity/version/capabilities matching the inspected manifest. After grants are resolved, `activate` receives an ephemeral activation-scoped token, allowed broker methods, resource limits and immutable configuration revision. No ability to mint tokens or inspect other plugins. Calls use `plugin.v1.invoke`; long work returns a job handle; events are validated against registered schemas. `cancel`, `drain`, `deactivate`, health heartbeat and revocation have explicit acknowledgements and deadlines.

Activation timeout starts at 10 seconds; drain deadline 10 seconds by default; exceeding limits fails that activation. After three crashes in five minutes, disable automatic restart and show a recoverable failed state. Process cleanup is limited to the supervisor's own recorded plugin children; never signal by application name or broad process pattern. Removal and updates stop new admission before draining. Other extensions and runs are unaffected unless they explicitly depend on the unavailable capability.

Grants are checked on every brokered call/subscription, including after revocation. Plugin-to-plugin calls go through registered public operations and are authorized using the caller's scopes; they do not inherit the callee's user authority. Jobs are owned by activation/plugin identity; a crashed worker marks its nonresumable jobs interrupted. Resume requires a declared checkpoint schema and conformance test. A plugin may not arbitrarily register core operation IDs or modify engine routing policy.

## 6. Provider and host ports

Provider execution port accepts normalized `RunRequest`, validated capability/route snapshot and optional opaque continuation. It yields application events and implements cancel. Catalog discovery, credential resolution, compaction and resumable execution are separate optional ports; implementers do not stub methods they cannot support.

Host integration port supplies runtime identity, supported API profile, model projection planning/application, contextual session metadata, tool execution bridge and launch preparation. Window attachment is a separate platform port; headless hosts need not fake windows. Codex subscriptions and opaque host encryption remain host-bound. A fake host proves core independence; a second actual product requires its own integration evidence.

## 7. SDK and schema ownership

Maintain machine-readable JSON Schema and an operation catalog under `contracts/`, with examples and invalid cases. Generate or validate Swift Codable DTOs and Python typed models from those schemas; choose and pin the generator in B01 using an actual encode/decode spike. The source of truth is the schema, not two handwritten type definitions. Unknown field/version behavior has golden fixtures.

Python SDK provides handshake, dispatch, validation, cancellation, broker clients and test harness, without importing the engine. A minimal JavaScript example demonstrates implementing the same wire without the SDK. Proposed author commands: `model-deck plugin init`, `plugin validate`, `plugin dev --state-dir <temp>`, `plugin test`, `plugin pack`. These commands do not exist yet. Developer quickstart must exercise a packaged extension against the installed/staged engine, not silently import the repository.

## 8. Principal establishment and delegated authority

Version negotiation is not authentication. Bootstrap owns trusted-local-client enrollment and the engine instance identity. The current-platform credential/private-file adapter creates an instance-scoped operator credential for the app/CLI; a client proves possession over the private IPC endpoint before any method except minimal hello. Combine platform peer-identity checks where available with that credential. Rendezvous exposes endpoint/version/instance nonce, never credentials. Client code verifies the expected instance challenge before sending application requests. Enrollment/rotation and token storage are implemented by B02's bootstrap/credential adapter, with fixtures; plugins cannot enroll themselves as operators.

The local handshake uses two `engine.v1.hello` requests. The first negotiates the offered version and required capabilities and returns the engine instance ID, instance nonce and unauthenticated status. The client checks those against its private rendezvous descriptor before repeating hello with the instance enrollment credential, instance ID and nonce. The server validates that credential and assigns the principal; a client-supplied role is never accepted. Provider API credentials are not used for client enrollment. Handshake credentials are excluded from logs and responses.

Roles are server-assigned: `local_operator`, `host_session`, or `plugin_activation`. Credentials bind issuer/engine instance, audience, role, principal ID, grants, expiry and revocation generation. Caller-supplied role/principal JSON has no authority. Default unknown-client permissions are none. The local operator may manage settings/extensions; host-session tokens are limited to that host/session's operations; activation tokens are limited to one plugin version/activation and its approved operations/resources. Privileged credential enrollment is unavailable through an ordinary plugin token. Tokens are private and redacted from diagnostics, state exports and request logs.

A plugin call receives a supervisor-authenticated `InvocationContext`: invocation ID, origin principal, actor plugin, operation ID, allowed effects/resource scopes, deadline and revocation generation. The worker cannot widen or forge it. Nested broker requests must carry the opaque invocation handle; the supervisor resolves the context rather than trusting a worker's copy.

For sensitive engine/host/credential capabilities, effective delegated authority is the intersection of the origin's delegated scopes, the called operation's declared effects and the callee's active grants. Calling a privileged plugin does not upgrade the caller. The callee may access its own private feature storage only within the published operation's declared effect (e.g. authorized note creation writes the callee's notes, not the caller's or another plugin's store). A read-only operation cannot become a write merely because the callee generally has storage permission. Persistent job context retains origin/effect scopes and rechecks revocation on every broker call. Scheduled autonomous work needs a separately declared/approved background grant; an arbitrary timer does not acquire operator rights.

Negative fixtures: spoofed role, wrong instance/audience, expired/revoked token, stolen invocation handle from another activation, host-session crossover, and a low-privilege caller using a privileged callee to fetch credentials or session content. Broker checks enforce this API contract; trusted unsandboxed same-user code may bypass OS-private files outside the broker, as PLAN D7 explicitly states.

## 9. Complete supporting method inventory and ownership

These operation families must be included in B01's schema freeze. Exact field definitions are B01 implementation work; their ownership and required outcomes are settled here. Public methods have `engine.v1.` prefix. Worker-to-supervisor broker calls have `plugin.v1.broker.` prefix and run only on the private authenticated activation channel. Brokers forward to the owning capability, not a second storage/authorization implementation.

| Surface / methods | Required inputs, outputs and restrictions | Implementation owner |
|---|---|---|
| Public `extensions.get`, `extensions.list`, `extensions.grants.get`, `extensions.grants.change` | Identity/status/version, requested/approved scopes, expected revision; grant changes operator-only, revocation immediate | B20; auth primitives B02/B17 |
| Public `ui.contributions.list`, `ui.panel.get`, `schemas.get` | Active contribution IDs, descriptors and versioned schema; no private executable paths/credentials | B17 descriptors, B21 rendering/discovery |
| Public `operations.invoke` data reads | Panel reads its own plugin's note body through declared note-read operation with authorized caller/context; not a global session-content event subscription | B19 dispatch/storage, B21 bindings, B22 feature |
| Public `attachments.begin`, `attachments.write_chunk`, `attachments.finish`, `attachments.read_chunk`, `attachments.release` | Scoped owner/run/invocation handle, size/content type, chunk sequence, quotas/TTL; no raw path supplied by plugin; checksum/length verified at finish | B19 attachment store/quotas |
| Public `attachments.export` | Operator-authorized completed handle and platform-issued user-selected destination handle; returns receipt, never accepts an arbitrary plugin path | B21 platform UI broker, B19 data |
| Public `sessions.compact` | Session/revision and capability; starts an explicit run/job result without bypassing route/continuation scope | B15 |
| Privileged `credentials.enroll`, `credentials.replace`, `credentials.remove` | Authenticated operator + connection scope + private bounded input; produces opaque reference/revision, never returns stored token; helper remains platform adapter | B07 connection workflow, B02 broker, B25 UI |
| Broker `storage.get`, `storage.list`, `storage.put`, `storage.delete` | Plugin-owned namespace, key/query limits, expected revision and quotas; descriptor effect/context checked | B19 |
| Broker `jobs.create`, `jobs.progress`, `jobs.complete`, `jobs.fail`, `jobs.check_cancelled` | Operation/invocation context, declared checkpoint schema, status and optional scoped output attachment handle; jobs.get/cancel remain public | B19 |
| Broker `events.publish`, `events.subscribe`, `events.ack`, `events.unsubscribe` | Registered schema/topic, own namespace or granted topic, content scopes and bounded flow control | B19 |
| Broker `engine.call` | Registered public operation plus opaque invocation context; intersects authority and rejects privileged enrollment/lifecycle escalation | B17/B18 |
| Broker `attachments.begin`, `attachments.write_chunk`, `attachments.finish`, `attachments.read_chunk`, `attachments.release` | Same owned attachment use cases; invocation/run ownership and size limits; no export destination authority | B19 |
| Broker `credentials.resolve` | Provider activation only, connection-scoped explicit credential grant; ephemeral private result; never general list/read-all | B18 proxy + existing credential adapter |

Export jobs write an owned attachment and return its handle. The UI/CLI operator explicitly chooses/authorizes the destination through the platform boundary; the plugin does not get unrestricted file export access. Note bodies remain feature-owned data and are exposed only to the authorized note-read operation. All method families have schema-positive/negative fixtures, permission tests and unsupported-capability behavior.

## 10. External provider contribution binding

A manifest may contribute `providers` entries with a stable provider ID under the plugin namespace, `port: provider.execution/v1`, execution mode and declared optional catalog/compaction/resume features. B01 defines these descriptor and wire schemas. Kernel validates/records descriptors; the engine's ProviderExecution proxy adapter binds the installed contribution without a provider-name switch.

Private provider methods on the authenticated activation channel:

- `plugin.v1.provider.start`: normalized RunRequest, route/capability snapshot and opaque continuation reference; returns accepted adapter run handle. Credential access is a separate scoped broker request.
- `plugin.v1.provider.submit_tool_result`: adapter run handle, outstanding tool call ID and result; host authorization was validated by engine before dispatch.
- `plugin.v1.provider.cancel`: owned run handle and deadline; acknowledgement distinguishes request accepted from confirmed provider termination.
- `plugin.v1.provider.resume`: available only when declared and tested; bound to checkpoint schema and original route scope.
- `plugin.v1.provider.event`: notification carrying adapter run handle, sequence and application event. Supervisor validates event schema, route/run ownership, flow-control credits and exactly-one-terminal rules.
- `plugin.v1.provider.ack`: cumulative event acknowledgement/credit from supervisor; worker cannot bypass queue limits by publishing generic feature events.

Optional discovery and compaction ports are separately versioned descriptors; a provider need not implement them to generate text. Provider runs are not generic plugin background jobs: engine owns their lifecycle, tool states, accounting and billed-dispatch semantics. Unexpected provider worker exit marks its affected runs interrupted without automatic billed resubmission.

B18 owns both this proxy binding and an external archive containing a deterministic provider. Install it through the fixture harness, register/select its model through the same model-library flow, exercise text/tools/cancel/errors, and run the same ProviderExecution conformance cases used for built-ins. Fixtures verify foreign connection credentials and another activation's run handles are refused. B20 later proves production archive lifecycle using the same package; B23 documents authoring. No private engine import or per-provider routing branch may be added to pass this proof.

# Host settings service (native typed client)

Purpose: async typed calls for `engine.v1.hosts.settings.read`, `.validate`,
`.preview`, and `.save` against the frozen schemas in
`contracts/engine.v1/methods/hosts.settings.*`, reusing the accepted
`HostSettingsTypes` DTOs with no parallel value/type system.

Ownership: `HostSettingsServing.swift` (protocol),
`EngineHostSettingsService.swift` (service), and
`Tests/ModelDeckClientTests/EngineHostSettingsServiceTests.swift` (tests),
plus the one shared seam in `ModelDeckEngineClient.swift`:
`invokeValidated(method:params:paramsSchemaRef:resultSchemaRef:)`. No other
client files are edited by this slice.

Public signatures:

```swift
// ModelDeckEngineClient.swift (shared seam)
func invokeValidated<Params: Encodable, Result: Decodable>(
    method: String, params: Params,
    paramsSchemaRef: String, resultSchemaRef: String
) throws -> Result

// HostSettingsServing.swift
func read(params: HostSettingsReadParams) async throws -> HostSettingsReadResult
func validate(params: HostSettingsValidateParams) async throws -> HostSettingsValidateResult
func preview(params: HostSettingsPreviewParams) async throws -> HostSettingsPreviewResult
func save(params: HostSettingsSaveParams) async throws -> HostSettingsSaveResult
func cancel()

// EngineHostSettingsService.swift
init(
    rendezvous: EngineRendezvousDescriptor,
    makeTransport: @escaping () throws -> EngineTransport,
    credentialProvider: EngineInstanceCredentialProviding
)
```

Contracts: every call builds a `ModelDeckEngineClient` from the transport
factory, runs the two-step hello authentication over `rendezvous` with the
connect/auth guard, then performs exactly one validated settings call
through the shared client framing path (`invokeValidated`), which refuses any
call until `connectAndAuthenticate` has completed; only the private hello path
is exempt. Request IDs are
client-scoped counters (`md-<n>`), so each transport shows `md-1`, `md-2`
(hellos), `md-3` (settings). Results validate against the frozen result
schema before typed decode.

Invariants: caller-supplied `preview_id`, `candidate_content_hash`,
`base_content_hash`, `context_revision`, and `idempotency_key` propagate
exactly; the service never synthesizes, rewrites, or retries them. One
invocation performs at most one settings send/receive round-trip, so a
failed `save` is never re-executed implicitly. Blocking socket IO runs on an
off-main serial worker queue; concurrent calls serialize on that queue, each
with an isolated transport/client (serialized execution, not simultaneous).
Each call captures the client generation before enqueue; `cancel()` bumps the
generation and cancels registered clients, so a call still blocked in its
transport factory aborts with `requestCancelled` before sending hello, while
later calls on the new generation are unaffected. `connectAndAuthenticate`
never clears a prior cancellation. No filesystem or legacy-config
access, and no secret-bearing values enter logs, diagnostics, or failure
messages.

Extension: new operations require a frozen contract schema first; add the
method name, schema refs, and DTOs following the existing four-call pattern.

Tests: `FakeEngineTransport` proves the two-hello sequence plus actual
settings method names and verbatim param payloads, authentication failure
sending no settings frame, concurrent-call isolation across separate
transports, schema rejection of bad results, malformed-response and engine
`error` surfacing, and single-settings-frame (no-retry) behavior on every
failure path. Remote `error.message` text is never propagated: the service
boundary maps engine `.unavailable` errors to a fixed message naming only the
local method constant. Run `swift test --filter EngineHostSettingsServiceTests` from
`macos/`; never build the app or installer for this slice.

Limitations: connection lifecycle and authentication stay inside the shared
client guard; the service owns no framing. Unknown wire fields are rejected
at schema validation, not in the DTOs.

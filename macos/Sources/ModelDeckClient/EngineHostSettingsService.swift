import Foundation
import ModelDeckContracts

/// Native typed client for the frozen `engine.v1.hosts.settings.*` methods.
///
/// Each call builds a `ModelDeckEngineClient` from the transport factory,
/// runs the two-step hello authentication over `rendezvous`, then performs
/// exactly one validated settings call through the shared client framing
/// path. Blocking socket IO runs on an off-main serial worker queue; concurrent
/// calls serialize on that queue, each with an isolated transport and client. Server-owned IDs, base hashes,
/// context revisions, and tokens pass through untouched: the service never
/// synthesizes `preview_id`, `candidate_content_hash`, `context_revision`,
/// or `idempotency_key` values, never mutates params, and never retries.
/// Engine errors surface as `EngineClientError`.
public final class EngineHostSettingsService: HostSettingsServing {
    /// Frozen method names for `engine.v1.hosts.settings.*`.
    public static let readMethod = "engine.v1.hosts.settings.read"
    /// Frozen method names for `engine.v1.hosts.settings.*`.
    public static let validateMethod = "engine.v1.hosts.settings.validate"
    /// Frozen method names for `engine.v1.hosts.settings.*`.
    public static let previewMethod = "engine.v1.hosts.settings.preview"
    /// Frozen method names for `engine.v1.hosts.settings.*`.
    public static let saveMethod = "engine.v1.hosts.settings.save"

    private let rendezvous: EngineRendezvousDescriptor
    private let makeTransport: () throws -> EngineTransport
    private let credentialProvider: EngineInstanceCredentialProviding
    private let workerQueue = DispatchQueue(label: "modeldeck.engine.hostsettings", qos: .userInitiated)
    private let stateLock = NSLock()
    private var inFlightClients: [ObjectIdentifier: ModelDeckEngineClient] = [:]
    private var generation: UInt64 = 0

    /// Creates a service that authenticates via `rendezvous` and dials through `makeTransport`.
    public init(
        rendezvous: EngineRendezvousDescriptor,
        makeTransport: @escaping () throws -> EngineTransport,
        credentialProvider: EngineInstanceCredentialProviding
    ) {
        self.rendezvous = rendezvous
        self.makeTransport = makeTransport
        self.credentialProvider = credentialProvider
    }

    /// Cancels coordinated in-flight settings calls.
    ///
    /// Bumps the request generation so a call still blocked in its transport
    /// factory aborts before sending hello once the factory returns; calls
    /// started after `cancel` capture the new generation and are unaffected.
    public func cancel() {
        stateLock.lock()
        generation &+= 1
        let clients = Array(inFlightClients.values)
        stateLock.unlock()
        clients.forEach { $0.cancelInFlight() }
    }

    /// Calls `engine.v1.hosts.settings.read`.
    public func read(params: HostSettingsReadParams) async throws -> HostSettingsReadResult {
        try await perform(
            method: Self.readMethod,
            params: params,
            paramsSchemaRef: "contracts/engine.v1/methods/hosts.settings.read.params.schema.json",
            resultSchemaRef: "contracts/engine.v1/methods/hosts.settings.read.result.schema.json"
        )
    }

    /// Calls `engine.v1.hosts.settings.validate`.
    public func validate(params: HostSettingsValidateParams) async throws -> HostSettingsValidateResult {
        try await perform(
            method: Self.validateMethod,
            params: params,
            paramsSchemaRef: "contracts/engine.v1/methods/hosts.settings.validate.params.schema.json",
            resultSchemaRef: "contracts/engine.v1/methods/hosts.settings.validate.result.schema.json"
        )
    }

    /// Calls `engine.v1.hosts.settings.preview`.
    public func preview(params: HostSettingsPreviewParams) async throws -> HostSettingsPreviewResult {
        try await perform(
            method: Self.previewMethod,
            params: params,
            paramsSchemaRef: "contracts/engine.v1/methods/hosts.settings.preview.params.schema.json",
            resultSchemaRef: "contracts/engine.v1/methods/hosts.settings.preview.result.schema.json"
        )
    }

    /// Calls `engine.v1.hosts.settings.save` exactly once; no retry on failure.
    public func save(params: HostSettingsSaveParams) async throws -> HostSettingsSaveResult {
        try await perform(
            method: Self.saveMethod,
            params: params,
            paramsSchemaRef: "contracts/engine.v1/methods/hosts.settings.save.params.schema.json",
            resultSchemaRef: "contracts/engine.v1/methods/hosts.settings.save.result.schema.json"
        )
    }

    private func perform<Params: Encodable & Sendable, Result: Decodable & Sendable>(
        method: String,
        params: Params,
        paramsSchemaRef: String,
        resultSchemaRef: String
    ) async throws -> Result {
        stateLock.lock()
        let requestGeneration = generation
        stateLock.unlock()
        return try await withCheckedThrowingContinuation { continuation in
            workerQueue.async {
                do {
                    let transport = try self.makeTransport()
                    let client = ModelDeckEngineClient(
                        transport: transport,
                        credentialProvider: self.credentialProvider
                    )
                    let key = ObjectIdentifier(client)
                    self.stateLock.lock()
                    self.inFlightClients[key] = client
                    let stale = requestGeneration != self.generation
                    self.stateLock.unlock()
                    guard !stale else {
                        self.stateLock.lock()
                        self.inFlightClients.removeValue(forKey: key)
                        self.stateLock.unlock()
                        transport.close()
                        continuation.resume(throwing: EngineClientError.requestCancelled)
                        return
                    }
                    defer {
                        self.stateLock.lock()
                        self.inFlightClients.removeValue(forKey: key)
                        self.stateLock.unlock()
                        transport.close()
                    }
                    try client.connectAndAuthenticate(descriptor: self.rendezvous)
                    let result: Result = try client.invokeValidated(
                        method: method,
                        params: params,
                        paramsSchemaRef: paramsSchemaRef,
                        resultSchemaRef: resultSchemaRef
                    )
                    continuation.resume(returning: result)
                } catch {
                    continuation.resume(throwing: Self.sanitized(error, method: method))
                }
            }
        }
    }

    /// Replaces server-supplied error text with fixed safe errors.
    ///
    /// The shared JSON-RPC surface surfaces `error.message` verbatim; this
    /// boundary never propagates that text and maps `.unavailable` to a fixed
    /// message naming only the local method constant.
    private static func sanitized(_ error: Error, method: String) -> Error {
        guard let clientError = error as? EngineClientError else { return error }
        if case .unavailable = clientError {
            return EngineClientError.unavailable("engine hosts.settings request failed (\(method))")
        }
        return error
    }
}

import Foundation
import ModelDeckContracts

/// Typed native client for `engine.v1.connections.{list,save}`.
///
/// Wraps the frozen JSON-RPC surface exactly: no schema mutation, no
/// implicit `idempotency_key` synthesis outside the explicit helper, and
/// `expected_revision` passed through verbatim from the caller. Concurrent
/// calls serialize on a single shared `ModelDeckEngineClient`; the two-step
/// hello handshake runs once during ``connect()`` and each call is one
/// validated JSON-RPC round-trip.
///
/// The caller is responsible for:
/// - generating a fresh `idempotency_key` per logical mutation (use
///   ``EngineConnectionService/newIdempotencyKey()`` or supply their own
///   externally-coordinated key);
/// - tracking the current `revision` from ``listConnections()`` and passing
///   it as `expectedRevision` on the next ``saveConnection``;
/// - resolving the schema-validated `conflict` error from the engine by
///   re-listing and retrying — this client never auto-retries.
public final class EngineConnectionService: @unchecked Sendable {
    /// Frozen RPC method for listing connections.
    public static let listMethod = "engine.v1.connections.list"
    /// Frozen RPC method for saving a connection (add or update).
    public static let saveMethod = "engine.v1.connections.save"

    private let rendezvous: EngineRendezvousDescriptor
    private let client: ModelDeckEngineClient
    private let workerQueue = DispatchQueue(label: "modeldeck.engine.connections", qos: .userInitiated)
    private let lock = NSLock()

    /// Creates a service that authenticates via `rendezvous` and dials `transport`.
    public init(
        rendezvous: EngineRendezvousDescriptor,
        transport: EngineTransport,
        credentialProvider: EngineInstanceCredentialProviding
    ) {
        self.rendezvous = rendezvous
        self.client = ModelDeckEngineClient(transport: transport, credentialProvider: credentialProvider)
    }

    /// Runs the two-step hello authentication over `rendezvous`.
    public func connect() throws {
        try lock.withLock { try client.connectAndAuthenticate(descriptor: rendezvous) }
    }

    /// Calls `engine.v1.connections.list` exactly once.
    public func listConnections() async throws -> [ConnectionRef] {
        let result: ConnectionsListResult = try await perform(
            method: Self.listMethod,
            params: EmptyConnectionsParams(),
            paramsSchemaRef: "contracts/engine.v1/methods/connections.list.params.schema.json",
            resultSchemaRef: "contracts/engine.v1/methods/connections.list.result.schema.json"
        )
        return result.connections
    }

    /// Calls `engine.v1.connections.save` exactly once with the supplied
    /// `expectedRevision` and caller-supplied `idempotencyKey`. No retry on
    /// failure; the engine's schema-validated `conflict` error surfaces as
    /// ``EngineClientError/unavailable`` so the caller can re-list and decide.
    public func saveConnection(
        input: ConnectionSaveInput,
        expectedRevision: Int,
        idempotencyKey: String
    ) async throws -> ConnectionRef {
        let result: ConnectionSaveResult = try await perform(
            method: Self.saveMethod,
            params: ConnectionSaveParams(
                connection: input,
                expectedRevision: expectedRevision,
                idempotencyKey: idempotencyKey
            ),
            paramsSchemaRef: "contracts/engine.v1/methods/connections.save.params.schema.json",
            resultSchemaRef: "contracts/engine.v1/methods/connections.save.result.schema.json"
        )
        return result.connection
    }

    /// Generates a fresh lowercase UUIDv4 for use as an `idempotency_key`.
    public static func newIdempotencyKey() -> String {
        UUID().uuidString.lowercased()
    }

    private func perform<P: Encodable & Sendable, R: Decodable & Sendable>(
        method: String,
        params: P,
        paramsSchemaRef: String,
        resultSchemaRef: String
    ) async throws -> R {
        try await withCheckedThrowingContinuation { continuation in
            workerQueue.async {
                self.lock.withLock {
                    do {
                        let result: R = try self.client.invokeValidated(
                            method: method,
                            params: params,
                            paramsSchemaRef: paramsSchemaRef,
                            resultSchemaRef: resultSchemaRef
                        )
                        continuation.resume(returning: result)
                    } catch {
                        continuation.resume(throwing: error)
                    }
                }
            }
        }
    }
}

// MARK: - Wire types

/// Connection reference returned by `engine.v1.connections.{list,save}`.
///
/// Mirrors `vocabulary.schema.json#/definitions/connection_ref` exactly:
/// snake_case wire keys, `revision` is the CAS counter the caller must echo
/// back on the next `saveConnection`.
public struct ConnectionRef: Codable, Equatable, Sendable {
    public let connectionID: String
    public let providerID: String
    public let endpointConfigRef: String?
    public let credentialRef: String?
    public let revision: Int

    public init(
        connectionID: String,
        providerID: String,
        endpointConfigRef: String? = nil,
        credentialRef: String? = nil,
        revision: Int
    ) {
        self.connectionID = connectionID
        self.providerID = providerID
        self.endpointConfigRef = endpointConfigRef
        self.credentialRef = credentialRef
        self.revision = revision
    }

    enum CodingKeys: String, CodingKey {
        case connectionID = "connection_id"
        case providerID = "provider_id"
        case endpointConfigRef = "endpoint_config_ref"
        case credentialRef = "credential_ref"
        case revision
    }
}

/// Input payload for `engine.v1.connections.save`.
///
/// Mirrors `vocabulary.schema.json#/definitions/connection_save_input`.
/// `revision` is intentionally absent: the CAS `expected_revision` lives at
/// the top level of the params, not inside the connection object.
public struct ConnectionSaveInput: Codable, Equatable, Sendable {
    public let connectionID: String
    public let providerID: String
    public let endpointConfigRef: String?
    public let credentialRef: String?

    public init(
        connectionID: String,
        providerID: String,
        endpointConfigRef: String? = nil,
        credentialRef: String? = nil
    ) {
        self.connectionID = connectionID
        self.providerID = providerID
        self.endpointConfigRef = endpointConfigRef
        self.credentialRef = credentialRef
    }

    enum CodingKeys: String, CodingKey {
        case connectionID = "connection_id"
        case providerID = "provider_id"
        case endpointConfigRef = "endpoint_config_ref"
        case credentialRef = "credential_ref"
    }
}

// MARK: - Internal request/response shapes

private struct EmptyConnectionsParams: Encodable, Sendable {}

private struct ConnectionsListResult: Decodable, Sendable {
    let connections: [ConnectionRef]
}

private struct ConnectionSaveResult: Decodable, Sendable {
    let connection: ConnectionRef
}

private struct ConnectionSaveParams: Encodable, Sendable {
    let connection: ConnectionSaveInput
    let expectedRevision: Int
    let idempotencyKey: String

    enum CodingKeys: String, CodingKey {
        case connection
        case expectedRevision = "expected_revision"
        case idempotencyKey = "idempotency_key"
    }
}

private extension NSLock {
    func withLock<T>(_ body: () throws -> T) rethrows -> T {
        lock()
        defer { unlock() }
        return try body()
    }
}

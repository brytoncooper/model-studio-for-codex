import Foundation
import ModelDeckContracts

/// Typed native client for `engine.v1.hosts.projection_status`.
///
/// Wraps the frozen JSON-RPC surface exactly: no schema mutation, no
/// implicit synthesis, no retry. Concurrent calls serialize on a single
/// shared `ModelDeckEngineClient`; the two-step hello handshake runs once
/// during ``connect()`` and each call is one validated JSON-RPC round-trip.
public final class EngineHostProjectionStatusService: @unchecked Sendable {
    /// Frozen RPC method for the host projection status query.
    public static let projectionStatusMethod = "engine.v1.hosts.projection_status"

    private let rendezvous: EngineRendezvousDescriptor
    private let client: ModelDeckEngineClient
    private let workerQueue = DispatchQueue(label: "modeldeck.engine.hostprojection", qos: .userInitiated)
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

    /// Calls `engine.v1.hosts.projection_status` exactly once.
    public func status(hostID: String) async throws -> HostProjectionStatus {
        let result: ProjectionStatusResult = try await perform(
            method: Self.projectionStatusMethod,
            params: ProjectionStatusParams(hostID: hostID),
            paramsSchemaRef: "contracts/engine.v1/methods/hosts.projection_status.params.schema.json",
            resultSchemaRef: "contracts/engine.v1/methods/hosts.projection_status.result.schema.json"
        )
        return result.status
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

/// Projection status reported by the engine.
///
/// Mirrors `contracts/engine.v1/methods/hosts.projection_status.result.schema.json#/properties/status`.
/// `rawValue` matches the schema enum strings verbatim so callers can format
/// the status directly into UI strings.
public enum HostProjectionStatus: String, Codable, Equatable, Sendable {
    case ready
    case pending
    case failed
}

// MARK: - Wire types

private struct ProjectionStatusParams: Encodable, Sendable {
    let hostID: String

    enum CodingKeys: String, CodingKey {
        case hostID = "host_id"
    }
}

private struct ProjectionStatusResult: Decodable, Sendable {
    let status: HostProjectionStatus
}

private extension NSLock {
    func withLock<T>(_ body: () throws -> T) rethrows -> T {
        lock()
        defer { unlock() }
        return try body()
    }
}

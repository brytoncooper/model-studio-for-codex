import Foundation
import ModelDeckContracts

/// Typed native client for the frozen `engine.v1.hosts.{list,prepare}` methods.
///
/// Wraps the JSON-RPC surface exactly: no schema mutation, no implicit
/// synthesis, no retry. Concurrent calls serialize on a single shared
/// `ModelDeckEngineClient`; the two-step hello handshake runs once during
/// ``connect()`` and each call is one validated JSON-RPC round-trip.
///
/// The service is host-agnostic: it never inspects a host's identity to
/// decide whether to call `prepare`, never opens or closes host processes,
/// and never reaches into Codex-specific state. Callers are responsible
/// for matching `host_id` against their known host catalog before invoking
/// ``prepareHost(hostID:)``.
public final class EngineHostService: @unchecked Sendable {
    /// Frozen RPC method for listing registered hosts.
    public static let listMethod = "engine.v1.hosts.list"
    /// Frozen RPC method for preparing a host for the current session.
    public static let prepareMethod = "engine.v1.hosts.prepare"

    private let rendezvous: EngineRendezvousDescriptor
    private let client: ModelDeckEngineClient
    private let workerQueue = DispatchQueue(label: "modeldeck.engine.hosts", qos: .userInitiated)
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

    /// Calls `engine.v1.hosts.list` exactly once with empty params.
    public func listHosts() async throws -> [HostEntry] {
        let result: HostsListResult = try await perform(
            method: Self.listMethod,
            params: EmptyHostsListParams(),
            paramsSchemaRef: "contracts/engine.v1/methods/hosts.list.params.schema.json",
            resultSchemaRef: "contracts/engine.v1/methods/hosts.list.result.schema.json"
        )
        return result.hosts
    }

    /// Calls `engine.v1.hosts.prepare` exactly once with the supplied `hostID`.
    ///
    /// Returns `true` only when the engine reports the host is prepared for
    /// the current session. A `false` response is a normal outcome (e.g. the
    /// engine already considers the host prepared or has nothing to do) and
    /// is not an error; the caller decides how to react.
    public func prepareHost(hostID: String) async throws -> Bool {
        let result: HostsPrepareResult = try await perform(
            method: Self.prepareMethod,
            params: HostsPrepareParams(hostID: hostID),
            paramsSchemaRef: "contracts/engine.v1/methods/hosts.prepare.params.schema.json",
            resultSchemaRef: "contracts/engine.v1/methods/hosts.prepare.result.schema.json"
        )
        return result.prepared
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

/// One entry returned by `engine.v1.hosts.list`.
///
/// Mirrors `contracts/engine.v1/methods/hosts.list.result.schema.json#/properties/hosts/items`:
/// `host_id` matches the shared `reverse_domain_id` pattern and `api_profile`
/// is a free-form short identifier supplied by the engine describing which
/// host profile the entry speaks.
public struct HostEntry: Codable, Equatable, Sendable {
    public let hostID: String
    public let apiProfile: String

    public init(hostID: String, apiProfile: String) {
        self.hostID = hostID
        self.apiProfile = apiProfile
    }

    enum CodingKeys: String, CodingKey {
        case hostID = "host_id"
        case apiProfile = "api_profile"
    }
}

// MARK: - Internal request/response shapes

private struct EmptyHostsListParams: Encodable, Sendable {}

private struct HostsListResult: Decodable, Sendable {
    let hosts: [HostEntry]
}

private struct HostsPrepareParams: Encodable, Sendable {
    let hostID: String

    enum CodingKeys: String, CodingKey {
        case hostID = "host_id"
    }
}

private struct HostsPrepareResult: Decodable, Sendable {
    let prepared: Bool
}

private extension NSLock {
    func withLock<T>(_ body: () throws -> T) rethrows -> T {
        lock()
        defer { unlock() }
        return try body()
    }
}

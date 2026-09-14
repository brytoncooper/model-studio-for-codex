import Foundation

public struct EngineUsageRecord: Codable, Equatable, Sendable {
    public let runID: String
    public let sessionID: String
    public let providerModelID: String?
    public let observedAt: String
    public let units: Double
    public let unitKind: String
    public let settledAmount: Double?
    public let currency: String?
    public let estimateAmount: Double?
    enum CodingKeys: String, CodingKey { case runID = "run_id"; case sessionID = "session_id"; case providerModelID = "provider_model_id"; case observedAt = "observed_at"; case units; case unitKind = "unit_kind"; case settledAmount = "settled_amount"; case currency; case estimateAmount = "estimate_amount" }
}

public final class EngineUsageService: @unchecked Sendable {
    private let rendezvous: EngineRendezvousDescriptor
    private let client: ModelDeckEngineClient
    private let lock = NSLock()
    public init(rendezvous: EngineRendezvousDescriptor, transport: EngineTransport, credentialProvider: EngineInstanceCredentialProviding) {
        self.rendezvous = rendezvous; self.client = ModelDeckEngineClient(transport: transport, credentialProvider: credentialProvider)
    }
    public func connect() throws { try lock.withLock { try client.connectAndAuthenticate(descriptor: rendezvous) } }
    public func query() throws -> [EngineUsageRecord] {
        struct Result: Decodable { let records: [EngineUsageRecord] }
        return try lock.withLock {
            let result: Result = try client.invokeValidated(method: "engine.v1.usage.query", params: EmptyUsageParams(), paramsSchemaRef: "contracts/engine.v1/methods/usage.query.params.schema.json", resultSchemaRef: "contracts/engine.v1/methods/usage.query.result.schema.json")
            return result.records
        }
    }
}
private struct EmptyUsageParams: Encodable {}
private extension NSLock { func withLock<T>(_ body: () throws -> T) rethrows -> T { lock(); defer { unlock() }; return try body() } }

import Foundation
import ModelDeckContracts

public struct ModelsListResult: Equatable, Sendable {
    public let items: [CatalogListItem]
    public let cacheOnly: Bool
}

public final class ModelDeckEngineClient {
    public static let offeredAPI: [String: Int] = ["major": 1, "minor": 0]
    public static let clientName = "model-deck-appkit"

    private let transport: EngineTransport
    private let credentialProvider: EngineInstanceCredentialProviding
    private var requestCounter = 0
    private let transportLock = NSLock()
    private var cancelled = false

    public init(transport: EngineTransport, credentialProvider: EngineInstanceCredentialProviding) {
        self.transport = transport
        self.credentialProvider = credentialProvider
    }

    public func cancelInFlight() {
        transportLock.lock()
        cancelled = true
        transportLock.unlock()
        transport.close()
    }

    public func connectAndAuthenticate(descriptor: EngineRendezvousDescriptor) throws {
        transportLock.lock()
        cancelled = false
        transportLock.unlock()
        try transport.open()
        let firstParams: [String: Any] = [
            "client_name": Self.clientName,
            "offered_api": Self.offeredAPI,
        ]
        try EngineJSONRPC.validateParams(firstParams, schemaRef: "contracts/engine.v1/methods/hello.params.schema.json")
        let firstResult = try call(method: "engine.v1.hello", params: firstParams)
        try validateHelloResult(firstResult)
        guard let firstObject = firstResult as? [String: Any],
              firstObject["authenticated"] as? Bool == false,
              let observedID = firstObject["engine_instance_id"] as? String,
              let observedNonce = firstObject["instance_nonce"] as? String else {
            throw EngineClientError.negotiationFailed("first hello did not return unauthenticated instance metadata")
        }
        guard descriptor.expectation.matches(engineInstanceID: observedID, instanceNonce: observedNonce) else {
            throw EngineClientError.rendezvousMismatch
        }
        try validateHelloAPIProfile(firstObject["api_profile"], expected: descriptor.apiProfile)
        let credential = try credentialProvider.readCredential()
        let secondParams: [String: Any] = [
            "client_name": Self.clientName,
            "offered_api": Self.offeredAPI,
            "authentication": [
                "engine_instance_id": observedID,
                "instance_nonce": observedNonce,
                "credential": credential,
            ],
        ]
        try EngineJSONRPC.validateParams(secondParams, schemaRef: "contracts/engine.v1/methods/hello.params.schema.json")
        let secondResult = try call(method: "engine.v1.hello", params: secondParams)
        try validateHelloResult(secondResult)
        guard let secondObject = secondResult as? [String: Any],
              secondObject["authenticated"] as? Bool == true,
              let authID = secondObject["engine_instance_id"] as? String,
              let authNonce = secondObject["instance_nonce"] as? String else {
            throw EngineClientError.negotiationFailed("second hello did not authenticate")
        }
        guard descriptor.expectation.matches(engineInstanceID: authID, instanceNonce: authNonce) else {
            throw EngineClientError.negotiationFailed("authenticated hello instance identity changed")
        }
        try validateHelloAPIProfile(secondObject["api_profile"], expected: descriptor.apiProfile)
    }

    public func listCatalogModels(connectionID: String, query: String? = nil) throws -> [CatalogListItem] {
        try listCatalogModelsResult(connectionID: connectionID, query: query).items
    }

    public func listCatalogModelsResult(connectionID: String, query: String? = nil) throws -> ModelsListResult {
        var params: [String: Any] = [
            "collection": "catalog",
            "connection_id": connectionID,
        ]
        if let query, !query.isEmpty { params["query"] = query }
        try EngineJSONRPC.validateParams(params, schemaRef: "contracts/engine.v1/methods/models.list.params.schema.json")
        let result = try call(method: "engine.v1.models.list", params: params)
        try EngineJSONRPC.validateResult(result, schemaRef: "contracts/engine.v1/methods/models.list.result.schema.json")
        guard let object = result as? [String: Any],
              let collection = object["collection"] as? String,
              let items = object["items"] as? [[String: Any]] else {
            throw EngineClientError.protocolError("models.list result missing items")
        }
        guard collection == "catalog" else {
            throw EngineClientError.protocolError("models.list returned unexpected collection")
        }
        guard let cacheOnly = object["cache_only"] as? Bool else {
            throw EngineClientError.protocolError("models.list result missing cache_only")
        }
        guard cacheOnly else {
            throw EngineClientError.protocolError("models.list catalog result must have cache_only true")
        }
        let parsed = try items.map { try CatalogListItem.parseEngineCatalogItem($0, expectedConnectionID: connectionID) }
        return ModelsListResult(items: parsed, cacheOnly: cacheOnly)
    }


    private func validateHelloResult(_ result: Any) throws {
        do {
            try EngineJSONRPC.validateResult(
                result,
                schemaRef: "contracts/engine.v1/methods/hello.result.schema.json"
            )
        } catch is SchemaValidationError {
            throw EngineClientError.negotiationFailed("hello result did not match expected schema")
        }
    }

    private func validateHelloAPIProfile(_ value: Any?, expected: EngineAPIProfile) throws {
        guard let value else {
            throw EngineClientError.negotiationFailed("hello result missing api_profile")
        }
        let profile: EngineAPIProfile
        do {
            profile = try EngineAPIProfile.parse(value)
        } catch let error as EngineClientError {
            if case .invalidRendezvous(let message) = error {
                throw EngineClientError.negotiationFailed(message)
            }
            throw error
        }
        guard profile == expected else {
            throw EngineClientError.negotiationFailed("hello api_profile did not match rendezvous")
        }
    }

    private func call(method: String, params: [String: Any]) throws -> Any {
        transportLock.lock()
        if cancelled {
            transportLock.unlock()
            throw EngineClientError.requestCancelled
        }
        requestCounter += 1
        let requestID = "md-\(requestCounter)"
        transportLock.unlock()
        let request = EngineJSONRPCRequest(id: requestID, method: method, params: params)
        let encoded = try request.encoded()
        try transport.send(frame: try EngineFrameCodec.encode(line: encoded))
        while true {
            transportLock.lock()
            if cancelled {
                transportLock.unlock()
                throw EngineClientError.requestCancelled
            }
            transportLock.unlock()
            let frame = try transport.receiveFrame()
            if frame.count > EngineFrameCodec.maxFrameBytes {
                throw EngineTransportError.frameTooLarge(frame.count)
            }
            return try EngineJSONRPC.parseResponse(data: frame, expectedID: requestID)
        }
    }
}

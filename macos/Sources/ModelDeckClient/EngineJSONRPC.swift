import Foundation
import ModelDeckContracts

public struct EngineJSONRPCRequest {
    public let jsonrpc = "2.0"
    public let id: String
    public let method: String
    public let params: [String: Any]

    public init(id: String, method: String, params: [String: Any]) {
        self.id = id
        self.method = method
        self.params = params
    }

    public func encoded() throws -> Data {
        let object: [String: Any] = [
            "jsonrpc": jsonrpc,
            "id": id,
            "method": method,
            "params": params,
        ]
        return try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    }
}

public enum EngineJSONRPC {
    public static func parseResponse(data: Data, expectedID: String) throws -> Any {
        let value = try JSONSerialization.jsonObject(with: data)
        guard let object = value as? [String: Any] else {
            throw EngineClientError.protocolError("response root must be an object")
        }
        if let error = object["error"] as? [String: Any] {
            let message = error["message"] as? String ?? "engine request failed"
            throw EngineClientError.unavailable(message)
        }
        guard object["id"] as? String == expectedID else {
            throw EngineClientError.protocolError("response id mismatch")
        }
        guard let result = object["result"] else {
            throw EngineClientError.protocolError("response missing result")
        }
        return result
    }

    public static func validateParams(_ params: [String: Any], schemaRef: String) throws {
        try SchemaValidator.validate(instance: params, schemaRef: schemaRef)
    }

    public static func validateResult(_ result: Any, schemaRef: String) throws {
        try SchemaValidator.validate(instance: result, schemaRef: schemaRef)
    }
}

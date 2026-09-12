import Foundation

public struct JsonRpcRequest: Codable, Equatable {
    public var jsonrpc: String
    public var id: String
    public var method: String
    public var params: [String: JSONValue]?

    public static func parse(data: Data) throws -> JsonRpcRequest {
        try ContractJSON.decodeValidated(
            JsonRpcRequest.self,
            from: data,
            schemaRef: "contracts/common/jsonrpc.schema.json#/definitions/request"
        )
    }
}

public enum JSONValue: Codable, Equatable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else {
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "unsupported JSON value")
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let value):
            try container.encode(value)
        case .number(let value):
            try container.encode(value)
        case .bool(let value):
            try container.encode(value)
        case .object(let value):
            try container.encode(value)
        case .array(let value):
            try container.encode(value)
        case .null:
            try container.encodeNil()
        }
    }
}

public struct CapabilityFeaturesFixture: Codable, Equatable {
    public var capabilities: CapabilitiesBlock

    public struct CapabilitiesBlock: Codable, Equatable {
        public var features: [String: String]
    }

    public static func parse(data: Data) throws -> CapabilityFeaturesFixture {
        try ContractJSON.decodeValidated(
            CapabilityFeaturesFixture.self,
            from: data,
            schemaRef: "contracts/engine.v1/methods/capabilities.get.result.schema.json"
        )
    }
}

public struct ToolCallFixture: Codable, Equatable {
    public var callId: String
    public var toolName: String
    public var arguments: JSONValue

    enum CodingKeys: String, CodingKey {
        case callId = "call_id"
        case toolName = "tool_name"
        case arguments
    }

    public static func parse(data: Data) throws -> ToolCallFixture {
        try ContractJSON.decodeValidated(
            ToolCallFixture.self,
            from: data,
            schemaRef: "contracts/engine.v1/vocabulary.schema.json#/definitions/tool_call"
        )
    }
}

public struct RunEventCompletedFixture: Codable, Equatable {
    public var kind: String
    public var runId: String
    public var sessionId: String
    public var sequence: Int
    public var eventSchemaVersion: Int
    public var observedAt: String
    public var terminalResult: TerminalResult

    public struct TerminalResult: Codable, Equatable {
        public var outcome: String
    }

    enum CodingKeys: String, CodingKey {
        case kind
        case runId = "run_id"
        case sessionId = "session_id"
        case sequence
        case eventSchemaVersion = "event_schema_version"
        case observedAt = "observed_at"
        case terminalResult = "terminal_result"
    }

    public static func parse(data: Data) throws -> RunEventCompletedFixture {
        try ContractJSON.decodeValidated(
            RunEventCompletedFixture.self,
            from: data,
            schemaRef: "contracts/engine.v1/vocabulary.schema.json#/definitions/run_event_run_completed"
        )
    }
}

import Foundation
import ModelDeckContracts

public struct ExtensionPanelContribution: Codable, Equatable {
    public let panelID: String
    public let title: String?

    public init(panelID: String, title: String?) {
        self.panelID = panelID
        self.title = title
    }

    enum CodingKeys: String, CodingKey {
        case panelID = "panel_id"
        case title
    }
}

public struct ExtensionOperationDescriptor: Codable, Equatable {
    public let operationID: String
    public let inputSchemaID: String
    public let outputSchemaID: String
    public let effect: String
    public let requiredGrants: [String]?

    enum CodingKeys: String, CodingKey {
        case operationID = "operation_id"
        case inputSchemaID = "input_schema_id"
        case outputSchemaID = "output_schema_id"
        case effect
        case requiredGrants = "required_grants"
    }
}

public struct ExtensionOperationResult: Codable, Equatable {
    public let output: JSONValue
    public let jobID: String?

    enum CodingKeys: String, CodingKey {
        case output
        case jobID = "job_id"
    }
}

/// Typed access to the frozen generic extension panel and operation RPCs.
///
/// The caller supplies an explicit rendezvous descriptor, transport, and
/// credential source. This service does not discover paths, start an engine,
/// interpret panel identifiers, or attach plugin-specific meaning to JSON.
public final class EngineExtensionPanelService: @unchecked Sendable {
    private let rendezvous: EngineRendezvousDescriptor
    private let client: ModelDeckEngineClient
    private let requestLock = NSLock()

    public init(
        rendezvous: EngineRendezvousDescriptor,
        transport: EngineTransport,
        credentialProvider: EngineInstanceCredentialProviding
    ) {
        self.rendezvous = rendezvous
        self.client = ModelDeckEngineClient(transport: transport, credentialProvider: credentialProvider)
    }

    public func connect() throws {
        try withSerializedRequest { try client.connectAndAuthenticate(descriptor: rendezvous) }
    }

    public func listPanels(extensionID: String? = nil) throws -> [ExtensionPanelContribution] {
        let result: PanelContributionsResult = try withSerializedRequest { try client.invokeValidated(
            method: "engine.v1.ui.contributions.list",
            params: PanelContributionsParams(extensionID: extensionID),
            paramsSchemaRef: "contracts/engine.v1/methods/ui.contributions.list.params.schema.json",
            resultSchemaRef: "contracts/engine.v1/methods/ui.contributions.list.result.schema.json"
        ) }
        return result.panels
    }

    public func fetchPanel(panelID: String) throws -> JSONValue {
        let result: PanelGetResult = try withSerializedRequest { try client.invokeValidated(
            method: "engine.v1.ui.panel.get",
            params: PanelGetParams(panelID: panelID),
            paramsSchemaRef: "contracts/engine.v1/methods/ui.panel.get.params.schema.json",
            resultSchemaRef: "contracts/engine.v1/methods/ui.panel.get.result.schema.json"
        ) }
        return result.panel
    }

    public func listOperations(pluginID: String? = nil) throws -> [ExtensionOperationDescriptor] {
        let result: OperationsListResult = try withSerializedRequest { try client.invokeValidated(
            method: "engine.v1.operations.list",
            params: OperationsListParams(pluginID: pluginID),
            paramsSchemaRef: "contracts/engine.v1/methods/operations.list.params.schema.json",
            resultSchemaRef: "contracts/engine.v1/methods/operations.list.result.schema.json"
        ) }
        return result.operations
    }

    public func invokeOperation(operationID: String, input: JSONValue) throws -> ExtensionOperationResult {
        try withSerializedRequest { try client.invokeValidated(
            method: "engine.v1.operations.invoke",
            params: OperationInvokeParams(
                operation: operationID,
                input: input,
                idempotencyKey: UUID().uuidString.lowercased()
            ),
            paramsSchemaRef: "contracts/engine.v1/methods/operations.invoke.params.schema.json",
            resultSchemaRef: "contracts/engine.v1/methods/operations.invoke.result.schema.json"
        ) }
    }

    private func withSerializedRequest<Result>(_ request: () throws -> Result) rethrows -> Result {
        requestLock.lock()
        defer { requestLock.unlock() }
        return try request()
    }
}

private struct PanelContributionsParams: Encodable {
    let extensionID: String?
    enum CodingKeys: String, CodingKey { case extensionID = "extension_id" }
}

private struct PanelContributionsResult: Decodable { let panels: [ExtensionPanelContribution] }

private struct PanelGetParams: Encodable {
    let panelID: String
    enum CodingKeys: String, CodingKey { case panelID = "panel_id" }
}

private struct PanelGetResult: Decodable { let panel: JSONValue }

private struct OperationsListParams: Encodable {
    let pluginID: String?
    enum CodingKeys: String, CodingKey { case pluginID = "plugin_id" }
}

private struct OperationsListResult: Decodable { let operations: [ExtensionOperationDescriptor] }

private struct OperationInvokeParams: Encodable {
    let operation: String
    let input: JSONValue
    let idempotencyKey: String
    enum CodingKeys: String, CodingKey {
        case operation, input
        case idempotencyKey = "idempotency_key"
    }
}

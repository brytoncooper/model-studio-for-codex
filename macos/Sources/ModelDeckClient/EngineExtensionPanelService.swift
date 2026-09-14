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
    public let panel: JSONValue?

    enum CodingKeys: String, CodingKey {
        case output
        case jobID = "job_id"
        case panel
    }

    public init(output: JSONValue, jobID: String? = nil, panel: JSONValue? = nil) {
        self.output = output
        self.jobID = jobID
        self.panel = panel
    }
}

public struct InstalledExtension: Codable, Equatable {
    public let extensionID: String
    public let status: String

    enum CodingKeys: String, CodingKey {
        case extensionID = "extension_id"
        case status
    }
}

public struct ExtensionDetail: Codable, Equatable {
    public let extensionID: String
    public let status: String
    public let version: String?
    public let revision: Int

    enum CodingKeys: String, CodingKey {
        case extensionID = "extension_id"
        case status
        case version
        case revision
    }
}



/// Generic surface the V2 observation controller consumes. The production
/// service satisfies this protocol implicitly; tests provide a fake.
public protocol JobObservationService: AnyObject {
    func getJob(jobID: String) throws -> JobSnapshot
    @discardableResult
    func cancelJob(jobID: String, idempotencyKey: String) throws -> Bool
}

extension EngineExtensionPanelService: JobObservationService {}

public enum JobState: String, Codable, Equatable, Sendable {
    case queued
    case running
    case completed
    case failed
    case cancelled
    case interrupted
    /// Engine returned a state string we do not model locally; callers should
    /// treat this as opaque and surface it without parsing plugin output.
    case unknown
}

public struct JobSnapshot: Equatable {
    public let jobID: String
    public let state: JobState
    public let progress: Double?
    /// Persisted JSON envelope when the job is ``completed``. ``nil`` when
    /// the storage column is absent (no terminal output). An explicit JSON
    /// ``null`` is preserved as ``.null``.
    public let output: JSONValue?
    /// True iff the engine result included an ``output`` key, distinguishing
    /// "absent" from "explicit JSON null" without losing either case.
    public let outputPresent: Bool

    public init(
        jobID: String,
        state: JobState,
        progress: Double? = nil,
        output: JSONValue? = nil,
        outputPresent: Bool = false
    ) {
        self.jobID = jobID
        self.state = state
        self.progress = progress
        self.output = output
        self.outputPresent = outputPresent
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

    public func listInstalledExtensions() throws -> [InstalledExtension] {
        let result: ExtensionsListResult = try withSerializedRequest {
            try client.invokeValidated(
                method: "engine.v1.extensions.list",
                params: EmptyParams(),
                paramsSchemaRef: "contracts/engine.v1/methods/extensions.list.params.schema.json",
                resultSchemaRef: "contracts/engine.v1/methods/extensions.list.result.schema.json"
            )
        }
        return result.extensions
    }

    public func extensionDetail(extensionID: String) throws -> ExtensionDetail {
        try withSerializedRequest {
            try client.invokeValidated(
                method: "engine.v1.extensions.get",
                params: ExtensionIDParams(extensionID: extensionID),
                paramsSchemaRef: "contracts/engine.v1/methods/extensions.get.params.schema.json",
                resultSchemaRef: "contracts/engine.v1/methods/extensions.get.result.schema.json"
            )
        }
    }

    @discardableResult
    public func installExtension(archivePath: String) throws -> String {
        let result: InstallResult = try withSerializedRequest {
            try client.invokeValidated(
                method: "engine.v1.extensions.install",
                params: InstallParams(archivePath: archivePath),
                paramsSchemaRef: "contracts/engine.v1/methods/extensions.install.params.schema.json",
                resultSchemaRef: "contracts/engine.v1/methods/extensions.install.result.schema.json"
            )
        }
        return result.extensionID
    }

    public func updateExtension(extensionID: String, archivePath: String, expectedRevision: Int) throws -> String {
        let result: UpdateResult = try withSerializedRequest {
            try client.invokeValidated(
                method: "engine.v1.extensions.update",
                params: UpdateParams(extensionID: extensionID, archivePath: archivePath, expectedRevision: expectedRevision),
                paramsSchemaRef: "contracts/engine.v1/methods/extensions.update.params.schema.json",
                resultSchemaRef: "contracts/engine.v1/methods/extensions.update.result.schema.json"
            )
        }
        return result.version
    }

    public func setExtensionEnabled(extensionID: String, revision: Int, enabled: Bool) throws {
        let method = enabled ? "engine.v1.extensions.enable" : "engine.v1.extensions.disable"
        let operation = enabled ? "enable" : "disable"
        let _: EnabledResult = try withSerializedRequest {
            try client.invokeValidated(
                method: method,
                params: ExtensionStateParams(extensionID: extensionID, expectedRevision: revision),
                paramsSchemaRef: "contracts/engine.v1/methods/extensions.\(operation).params.schema.json",
                resultSchemaRef: "contracts/engine.v1/methods/extensions.\(operation).result.schema.json"
            )
        }
    }


    /// Reads the bounded public job snapshot for ``jobID`` via ``engine.v1.jobs.get``.
    ///
    /// The method is generic: callers receive the JSON-Schema-validated snapshot
    /// (state, optional progress, optional output envelope) without any
    /// plugin-specific parsing. The persisted output is omitted from the
    /// public mapping when storage has no record for the job; explicit JSON
    /// ``null`` is preserved as ``output == .null``.
    public func getJob(jobID: String) throws -> JobSnapshot {
        let result: JobGetResult = try withSerializedRequest {
            try client.invokeValidated(
                method: "engine.v1.jobs.get",
                params: JobGetParams(jobID: jobID),
                paramsSchemaRef: "contracts/engine.v1/methods/jobs.get.params.schema.json",
                resultSchemaRef: "contracts/engine.v1/methods/jobs.get.result.schema.json"
            )
        }
        return JobSnapshot(
            jobID: result.jobID,
            state: JobState(rawValue: result.state) ?? .unknown,
            progress: result.progress,
            output: result.output,
            outputPresent: result.outputPresent
        )
    }

    /// Requests cancellation of ``jobID`` via ``engine.v1.jobs.cancel``.
    ///
    /// The boolean return is the server-side acknowledgement: ``true`` means
    /// the request was accepted (the job was active and may transition to
    /// ``cancelled`` when the worker confirms), ``false`` means the request
    /// was rejected because the job is already terminal. Cancellation UI
    /// must keep showing "requested" until a subsequent ``getJob`` observes
    /// a terminal state; this method never asserts termination.
    @discardableResult
    public func cancelJob(jobID: String, idempotencyKey: String) throws -> Bool {
        let result: JobCancelResult = try withSerializedRequest {
            try client.invokeValidated(
                method: "engine.v1.jobs.cancel",
                params: JobCancelParams(jobID: jobID, idempotencyKey: idempotencyKey),
                paramsSchemaRef: "contracts/engine.v1/methods/jobs.cancel.params.schema.json",
                resultSchemaRef: "contracts/engine.v1/methods/jobs.cancel.result.schema.json"
            )
        }
        return result.accepted
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

private struct EmptyParams: Encodable {}

private struct ExtensionsListResult: Decodable {
    let extensions: [InstalledExtension]
}

private struct ExtensionIDParams: Encodable {
    let extensionID: String

    enum CodingKeys: String, CodingKey {
        case extensionID = "extension_id"
    }
}

private struct InstallParams: Encodable {
    let archivePath: String
    let idempotencyKey = UUID().uuidString.lowercased()
    let expectedRevision = 0

    enum CodingKeys: String, CodingKey {
        case archivePath = "archive_path"
        case idempotencyKey = "idempotency_key"
        case expectedRevision = "expected_revision"
    }
}

private struct InstallResult: Decodable {
    let extensionID: String

    enum CodingKeys: String, CodingKey {
        case extensionID = "extension_id"
    }
}

private struct UpdateParams: Encodable {
    let extensionID: String
    let archivePath: String
    let idempotencyKey = UUID().uuidString.lowercased()
    let expectedRevision: Int
    enum CodingKeys: String, CodingKey {
        case extensionID = "extension_id"
        case archivePath = "archive_path"
        case idempotencyKey = "idempotency_key"
        case expectedRevision = "expected_revision"
    }
}

private struct UpdateResult: Decodable {
    let extensionID: String
    let version: String
    enum CodingKeys: String, CodingKey {
        case extensionID = "extension_id"
        case version
    }
}

private struct ExtensionStateParams: Encodable {
    let extensionID: String
    let expectedRevision: Int
    let idempotencyKey = UUID().uuidString.lowercased()

    enum CodingKeys: String, CodingKey {
        case extensionID = "extension_id"
        case expectedRevision = "expected_revision"
        case idempotencyKey = "idempotency_key"
    }
}

private struct EnabledResult: Decodable {
    let enabled: Bool
}


private struct JobGetParams: Encodable {
    let jobID: String
    enum CodingKeys: String, CodingKey { case jobID = "job_id" }
}

private struct JobGetResult: Decodable {
    let jobID: String
    let state: String
    let progress: Double?
    let output: JSONValue?
    let outputPresent: Bool

    enum CodingKeys: String, CodingKey {
        case jobID = "job_id"
        case state
        case progress
        case output
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        jobID = try container.decode(String.self, forKey: .jobID)
        state = try container.decode(String.self, forKey: .state)
        progress = try container.decodeIfPresent(Double.self, forKey: .progress)
        outputPresent = container.contains(.output)
        output = outputPresent
            ? try container.decode(JSONValue.self, forKey: .output)
            : nil
    }
}

private struct JobCancelParams: Encodable {
    let jobID: String
    let idempotencyKey: String
    enum CodingKeys: String, CodingKey {
        case jobID = "job_id"
        case idempotencyKey = "idempotency_key"
    }
}

private struct JobCancelResult: Decodable {
    let accepted: Bool
}

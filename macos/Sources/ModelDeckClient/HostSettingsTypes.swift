import Foundation
import ModelDeckContracts

/// Typed native contract for `engine.v1.hosts.settings.*`.
///
/// Mirrors `contracts/common/host_settings.schema.json` and the four operation
/// schemas under `contracts/engine.v1/methods/hosts.settings.*` exactly:
/// snake_case wire keys, optional versus omitted fields preserved, and no
/// Codex field allowlist anywhere. Unknown wire fields are rejected by the
/// contract schemas; these types only model known shapes.
///
/// Sensitivity is enforced by construction: ``HostFieldDescriptor`` decodes
/// `sensitivity == "secret"` into ``HostSecretFieldDescriptor``, which has
/// no members capable of carrying `value`, `default`, `effective`, or
/// `entries`, so secret values cannot pass through this type. Raw TOML and
/// diff text stay in their `String` payloads; these types deliberately expose
/// no textual descriptions of themselves.
public enum HostValueState: String, Codable, Equatable, Sendable {
    case unset
    case explicit
    case inherited
    case managed
    case unknown
}

/// Editability of a single settings field.
public enum HostEditability: String, Codable, Equatable, Sendable {
    case editable
    case managed
    case projectionOwned = "projection_owned"
    case unsupported
}

/// When a settings change takes effect.
public enum HostApplicationEffect: String, Codable, Equatable, Sendable {
    case immediate
    case futureSession = "future_session"
    case hostRestart = "host_restart"
    case modelDeckRestart = "model_deck_restart"
    case unknown
}

/// Declared value kind of a settings field.
public enum HostFieldType: String, Codable, Equatable, Sendable {
    case boolean
    case integer
    case number
    case string
    case `enum`
    case stringList = "string_list"
    case entries
}

/// Value visibility class of a settings field.
public enum HostSensitivity: String, Codable, Equatable, Sendable {
    case `public`
    case secret
}

/// Depth of validation the adapter could perform.
public enum HostValidationLevel: String, Codable, Equatable, Sendable {
    case schema
    case tomlOnly = "toml_only"
}

/// Adapter support level for the host version.
public enum HostSupportLevel: String, Codable, Equatable, Sendable {
    case supported
    case tomlOnly = "toml_only"
    case unavailable
}

/// Activation status of one precedence layer.
public enum HostLayerStatus: String, Codable, Equatable, Sendable {
    case active
    case inactive
    case unknown
}

/// Severity of one validation diagnostic.
public enum HostDiagnosticSeverity: String, Codable, Equatable, Sendable {
    case error
    case warning
    case info
}

/// Operation of one structured change: `set` carries a value, `unset` cannot.
public enum HostChangeOperation: String, Codable, Equatable, Sendable {
    case set
    case unset
}

/// SHA-256 over exact document bytes, or `absent` when the document does not exist.
public enum HostContentHash: Codable, Equatable, Sendable {
    case absent
    case present(String)

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        let raw = try container.decode(String.self)
        if raw == "absent" {
            self = .absent
        } else {
            self = .present(raw)
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .absent:
            try container.encode("absent")
        case .present(let digest):
            try container.encode(digest)
        }
    }
}

/// One `{ value, label }` entry of a field constraint choice list.
public struct HostConstraintChoice: Codable, Equatable, Sendable {
    public var value: JSONValue
    public var label: String

    public init(value: JSONValue, label: String) {
        self.value = value
        self.label = label
    }
}

/// Adapter-supplied bounds for a field. No host-owned key allowlist exists.
public struct HostConstraints: Codable, Equatable, Sendable {
    public var minimum: Double?
    public var maximum: Double?
    public var minLength: Int?
    public var maxLength: Int?
    public var maxItems: Int?
    public var choices: [HostConstraintChoice]?

    public init(
        minimum: Double? = nil,
        maximum: Double? = nil,
        minLength: Int? = nil,
        maxLength: Int? = nil,
        maxItems: Int? = nil,
        choices: [HostConstraintChoice]? = nil
    ) {
        self.minimum = minimum
        self.maximum = maximum
        self.minLength = minLength
        self.maxLength = maxLength
        self.maxItems = maxItems
        self.choices = choices
    }

    private enum CodingKeys: String, CodingKey {
        case minimum
        case maximum
        case minLength = "min_length"
        case maxLength = "max_length"
        case maxItems = "max_items"
        case choices
    }
}

/// Public field descriptor. Carries display values; never used for secrets.
public struct HostPublicFieldDescriptor: Codable, Equatable, Sendable {
    public var fieldID: String
    public var label: String
    public var key: String?
    public var fieldDescription: String?
    public var type: HostFieldType
    public var valueState: HostValueState
    public var editability: HostEditability
    public var applicationEffect: HostApplicationEffect
    public var value: JSONValue?
    public var defaultValue: JSONValue?
    public var effective: JSONValue?
    public var constraints: HostConstraints?
    public var entries: [HostEntryGroup]?

    public init(
        fieldID: String,
        label: String,
        key: String? = nil,
        fieldDescription: String? = nil,
        type: HostFieldType,
        valueState: HostValueState,
        editability: HostEditability,
        applicationEffect: HostApplicationEffect,
        value: JSONValue? = nil,
        defaultValue: JSONValue? = nil,
        effective: JSONValue? = nil,
        constraints: HostConstraints? = nil,
        entries: [HostEntryGroup]? = nil
    ) {
        self.fieldID = fieldID
        self.label = label
        self.key = key
        self.fieldDescription = fieldDescription
        self.type = type
        self.valueState = valueState
        self.editability = editability
        self.applicationEffect = applicationEffect
        self.value = value
        self.defaultValue = defaultValue
        self.effective = effective
        self.constraints = constraints
        self.entries = entries
    }

    private enum CodingKeys: String, CodingKey {
        case fieldID = "field_id"
        case label
        case key
        case fieldDescription = "description"
        case type
        case valueState = "value_state"
        case editability
        case applicationEffect = "application_effect"
        case sensitivity
        case value
        case defaultValue = "default"
        case effective
        case constraints
        case entries
        case configured
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        if container.contains(.configured) {
            throw DecodingError.dataCorruptedError(
                forKey: .configured,
                in: container,
                debugDescription: "public descriptor forbids configured"
            )
        }
        fieldID = try container.decode(String.self, forKey: .fieldID)
        label = try container.decode(String.self, forKey: .label)
        key = try container.decodeIfPresent(String.self, forKey: .key)
        fieldDescription = try container.decodeIfPresent(String.self, forKey: .fieldDescription)
        type = try container.decode(HostFieldType.self, forKey: .type)
        valueState = try container.decode(HostValueState.self, forKey: .valueState)
        editability = try container.decode(HostEditability.self, forKey: .editability)
        applicationEffect = try container.decode(HostApplicationEffect.self, forKey: .applicationEffect)
        value = try container.decodeIfPresent(JSONValue.self, forKey: .value)
        defaultValue = try container.decodeIfPresent(JSONValue.self, forKey: .defaultValue)
        effective = try container.decodeIfPresent(JSONValue.self, forKey: .effective)
        constraints = try container.decodeIfPresent(HostConstraints.self, forKey: .constraints)
        entries = try container.decodeIfPresent([HostEntryGroup].self, forKey: .entries)
        if type != .entries, entries != nil {
            throw DecodingError.dataCorruptedError(
                forKey: .entries,
                in: container,
                debugDescription: "entries requires entries type"
            )
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(fieldID, forKey: .fieldID)
        try container.encode(label, forKey: .label)
        try container.encodeIfPresent(key, forKey: .key)
        try container.encodeIfPresent(fieldDescription, forKey: .fieldDescription)
        try container.encode(type, forKey: .type)
        try container.encode(valueState, forKey: .valueState)
        try container.encode(editability, forKey: .editability)
        try container.encode(applicationEffect, forKey: .applicationEffect)
        try container.encode(HostSensitivity.public, forKey: .sensitivity)
        try container.encodeIfPresent(value, forKey: .value)
        try container.encodeIfPresent(defaultValue, forKey: .defaultValue)
        try container.encodeIfPresent(effective, forKey: .effective)
        try container.encodeIfPresent(constraints, forKey: .constraints)
        try container.encodeIfPresent(entries, forKey: .entries)
    }
}

/// Secret field descriptor. Structured values are redacted adapter-side and
/// replaced by `configured`; this type has no members for secret content.
public struct HostSecretFieldDescriptor: Codable, Equatable, Sendable {
    public var fieldID: String
    public var label: String
    public var key: String?
    public var fieldDescription: String?
    public var type: HostFieldType
    public var valueState: HostValueState
    public var editability: HostEditability
    public var applicationEffect: HostApplicationEffect
    public var configured: Bool
    public var constraints: HostConstraints?

    public init(
        fieldID: String,
        label: String,
        key: String? = nil,
        fieldDescription: String? = nil,
        type: HostFieldType,
        valueState: HostValueState,
        editability: HostEditability,
        applicationEffect: HostApplicationEffect,
        configured: Bool,
        constraints: HostConstraints? = nil
    ) {
        self.fieldID = fieldID
        self.label = label
        self.key = key
        self.fieldDescription = fieldDescription
        self.type = type
        self.valueState = valueState
        self.editability = editability
        self.applicationEffect = applicationEffect
        self.configured = configured
        self.constraints = constraints
    }

    private enum CodingKeys: String, CodingKey {
        case fieldID = "field_id"
        case label
        case key
        case fieldDescription = "description"
        case type
        case valueState = "value_state"
        case editability
        case applicationEffect = "application_effect"
        case sensitivity
        case configured
        case constraints
        case value
        case defaultValue = "default"
        case effective
        case entries
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        for forbidden: CodingKeys in [.value, .defaultValue, .effective, .entries] {
            if container.contains(forbidden) {
                throw DecodingError.dataCorruptedError(
                    forKey: forbidden,
                    in: container,
                    debugDescription: "secret descriptor forbids value content"
                )
            }
        }
        fieldID = try container.decode(String.self, forKey: .fieldID)
        label = try container.decode(String.self, forKey: .label)
        key = try container.decodeIfPresent(String.self, forKey: .key)
        fieldDescription = try container.decodeIfPresent(String.self, forKey: .fieldDescription)
        type = try container.decode(HostFieldType.self, forKey: .type)
        valueState = try container.decode(HostValueState.self, forKey: .valueState)
        editability = try container.decode(HostEditability.self, forKey: .editability)
        applicationEffect = try container.decode(HostApplicationEffect.self, forKey: .applicationEffect)
        configured = try container.decode(Bool.self, forKey: .configured)
        constraints = try container.decodeIfPresent(HostConstraints.self, forKey: .constraints)
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(fieldID, forKey: .fieldID)
        try container.encode(label, forKey: .label)
        try container.encodeIfPresent(key, forKey: .key)
        try container.encodeIfPresent(fieldDescription, forKey: .fieldDescription)
        try container.encode(type, forKey: .type)
        try container.encode(valueState, forKey: .valueState)
        try container.encode(editability, forKey: .editability)
        try container.encode(applicationEffect, forKey: .applicationEffect)
        try container.encode(HostSensitivity.secret, forKey: .sensitivity)
        try container.encode(configured, forKey: .configured)
        try container.encodeIfPresent(constraints, forKey: .constraints)
    }
}

/// Field descriptor discriminated by `sensitivity`.
public enum HostFieldDescriptor: Codable, Equatable, Sendable {
    case `public`(HostPublicFieldDescriptor)
    case secret(HostSecretFieldDescriptor)

    private enum SensitivityPeek: String, CodingKey {
        case sensitivity
    }

    public init(from decoder: Decoder) throws {
        let peek = try decoder.container(keyedBy: SensitivityPeek.self)
        switch try peek.decode(HostSensitivity.self, forKey: .sensitivity) {
        case .public:
            self = .public(try HostPublicFieldDescriptor(from: decoder))
        case .secret:
            self = .secret(try HostSecretFieldDescriptor(from: decoder))
        }
    }

    public func encode(to encoder: Encoder) throws {
        switch self {
        case .public(let descriptor):
            try descriptor.encode(to: encoder)
        case .secret(let descriptor):
            try descriptor.encode(to: encoder)
        }
    }
}

/// Named group of fields inside an `entries` field.
public struct HostEntryGroup: Codable, Equatable, Sendable {
    public var entryID: String
    public var label: String
    public var fields: [HostFieldDescriptor]

    public init(entryID: String, label: String, fields: [HostFieldDescriptor]) {
        self.entryID = entryID
        self.label = label
        self.fields = fields
    }

    private enum CodingKeys: String, CodingKey {
        case entryID = "entry_id"
        case label
        case fields
    }
}

/// One structured edit. `unset` carries no value by construction.
public enum HostStructuredChange: Codable, Equatable, Sendable {
    case set(fieldID: String, entryID: String?, value: JSONValue)
    case unset(fieldID: String, entryID: String?)

    private enum CodingKeys: String, CodingKey {
        case fieldID = "field_id"
        case entryID = "entry_id"
        case operation
        case value
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let fieldID = try container.decode(String.self, forKey: .fieldID)
        let entryID = try container.decodeIfPresent(String.self, forKey: .entryID)
        switch try container.decode(HostChangeOperation.self, forKey: .operation) {
        case .set:
            let value = try container.decode(JSONValue.self, forKey: .value)
            self = .set(fieldID: fieldID, entryID: entryID, value: value)
        case .unset:
            if container.contains(.value) {
                throw DecodingError.dataCorruptedError(
                    forKey: .value,
                    in: container,
                    debugDescription: "unset forbids value"
                )
            }
            self = .unset(fieldID: fieldID, entryID: entryID)
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .set(let fieldID, let entryID, let value):
            try container.encode(fieldID, forKey: .fieldID)
            try container.encodeIfPresent(entryID, forKey: .entryID)
            try container.encode(HostChangeOperation.set, forKey: .operation)
            try container.encode(value, forKey: .value)
        case .unset(let fieldID, let entryID):
            try container.encode(fieldID, forKey: .fieldID)
            try container.encodeIfPresent(entryID, forKey: .entryID)
            try container.encode(HostChangeOperation.unset, forKey: .operation)
        }
    }
}

/// Candidate document for validate/preview: exactly one of raw or structured.
public enum HostDraft: Codable, Equatable, Sendable {
    case raw(String)
    case structured([HostStructuredChange])

    private enum CodingKeys: String, CodingKey {
        case kind
        case rawTOML = "raw_toml"
        case changes
    }

    private enum Kind: String, Codable {
        case raw
        case structured
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        switch try container.decode(Kind.self, forKey: .kind) {
        case .raw:
            self = .raw(try container.decode(String.self, forKey: .rawTOML))
        case .structured:
            self = .structured(try container.decode([HostStructuredChange].self, forKey: .changes))
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .raw(let toml):
            try container.encode(Kind.raw, forKey: .kind)
            try container.encode(toml, forKey: .rawTOML)
        case .structured(let changes):
            try container.encode(Kind.structured, forKey: .kind)
            try container.encode(changes, forKey: .changes)
        }
    }
}

/// One validation diagnostic. Locations and field IDs only, never values.
public struct HostDiagnostic: Codable, Equatable, Sendable {
    public var severity: HostDiagnosticSeverity
    public var code: String
    public var message: String
    public var fieldID: String?
    public var entryID: String?
    public var line: Int?
    public var column: Int?

    public init(
        severity: HostDiagnosticSeverity,
        code: String,
        message: String,
        fieldID: String? = nil,
        entryID: String? = nil,
        line: Int? = nil,
        column: Int? = nil
    ) {
        self.severity = severity
        self.code = code
        self.message = message
        self.fieldID = fieldID
        self.entryID = entryID
        self.line = line
        self.column = column
    }

    private enum CodingKeys: String, CodingKey {
        case severity
        case code
        case message
        case fieldID = "field_id"
        case entryID = "entry_id"
        case line
        case column
    }
}

/// One ordered section of a structured document.
public struct HostSettingsSection: Codable, Equatable, Sendable {
    public var sectionID: String
    public var title: String
    public var sectionDescription: String?
    public var fields: [HostFieldDescriptor]

    public init(sectionID: String, title: String, sectionDescription: String? = nil, fields: [HostFieldDescriptor]) {
        self.sectionID = sectionID
        self.title = title
        self.sectionDescription = sectionDescription
        self.fields = fields
    }

    private enum CodingKeys: String, CodingKey {
        case sectionID = "section_id"
        case title
        case sectionDescription = "description"
        case fields
    }
}

/// Structured form of a settings document.
public struct HostStructuredDocument: Codable, Equatable, Sendable {
    public var sections: [HostSettingsSection]
    public var unrepresentedPaths: [String]?

    public init(sections: [HostSettingsSection], unrepresentedPaths: [String]? = nil) {
        self.sections = sections
        self.unrepresentedPaths = unrepresentedPaths
    }

    private enum CodingKeys: String, CodingKey {
        case sections
        case unrepresentedPaths = "unrepresented_paths"
    }
}

/// Write target of a settings document.
public struct HostSettingsTarget: Codable, Equatable, Sendable {
    public var displayName: String
    public var displayPath: String
    public var scope: String
    public var writable: Bool

    public init(displayName: String, displayPath: String, scope: String, writable: Bool) {
        self.displayName = displayName
        self.displayPath = displayPath
        self.scope = scope
        self.writable = writable
    }

    private enum CodingKeys: String, CodingKey {
        case displayName = "display_name"
        case displayPath = "display_path"
        case scope
        case writable
    }
}

/// Adapter schema profile for the host version.
public struct HostSchemaProfile: Codable, Equatable, Sendable {
    public var schemaID: String
    public var schemaRevision: String
    public var hostVersion: String
    public var supportLevel: HostSupportLevel
    public var referenceURL: String?

    public init(schemaID: String, schemaRevision: String, hostVersion: String, supportLevel: HostSupportLevel, referenceURL: String? = nil) {
        self.schemaID = schemaID
        self.schemaRevision = schemaRevision
        self.hostVersion = hostVersion
        self.supportLevel = supportLevel
        self.referenceURL = referenceURL
    }

    private enum CodingKeys: String, CodingKey {
        case schemaID = "schema_id"
        case schemaRevision = "schema_revision"
        case hostVersion = "host_version"
        case supportLevel = "support_level"
        case referenceURL = "reference_url"
    }
}

/// One adapter-provided precedence layer.
public struct HostPrecedenceLayer: Codable, Equatable, Sendable {
    public var layerID: String
    public var label: String
    public var editable: Bool
    public var status: HostLayerStatus

    public init(layerID: String, label: String, editable: Bool, status: HostLayerStatus) {
        self.layerID = layerID
        self.label = label
        self.status = status
        self.editable = editable
    }

    private enum CodingKeys: String, CodingKey {
        case layerID = "layer_id"
        case label
        case editable
        case status
    }
}

/// Read-only snapshot returned by `engine.v1.hosts.settings.read`.
public struct HostSettingsSnapshot: Codable, Equatable, Sendable {
    public var hostID: String
    public var documentID: String
    public var documentRevision: HostContentHash
    public var exists: Bool
    public var target: HostSettingsTarget
    public var schemaProfile: HostSchemaProfile
    public var precedence: [HostPrecedenceLayer]
    public var rawTOML: String
    public var structured: HostStructuredDocument
    public var contextRevision: String

    public init(
        hostID: String,
        documentID: String,
        documentRevision: HostContentHash,
        exists: Bool,
        target: HostSettingsTarget,
        schemaProfile: HostSchemaProfile,
        precedence: [HostPrecedenceLayer],
        rawTOML: String,
        structured: HostStructuredDocument,
        contextRevision: String
    ) {
        self.hostID = hostID
        self.documentID = documentID
        self.documentRevision = documentRevision
        self.exists = exists
        self.target = target
        self.schemaProfile = schemaProfile
        self.precedence = precedence
        self.rawTOML = rawTOML
        self.structured = structured
        self.contextRevision = contextRevision
    }

    private enum CodingKeys: String, CodingKey {
        case hostID = "host_id"
        case documentID = "document_id"
        case documentRevision = "document_revision"
        case exists
        case target
        case schemaProfile = "schema_profile"
        case precedence
        case rawTOML = "raw_toml"
        case structured
        case contextRevision = "context_revision"
    }
}

/// Params for `engine.v1.hosts.settings.read`.
public struct HostSettingsReadParams: Codable, Equatable, Sendable {
    public var hostID: String

    public init(hostID: String) {
        self.hostID = hostID
    }

    private enum CodingKeys: String, CodingKey {
        case hostID = "host_id"
    }
}

/// Result for `engine.v1.hosts.settings.read`.
public struct HostSettingsReadResult: Codable, Equatable, Sendable {
    public var snapshot: HostSettingsSnapshot

    public init(snapshot: HostSettingsSnapshot) {
        self.snapshot = snapshot
    }
}

/// Params for `engine.v1.hosts.settings.validate`.
public struct HostSettingsValidateParams: Codable, Equatable, Sendable {
    public var hostID: String
    public var documentID: String
    public var expectedContentHash: HostContentHash
    public var draft: HostDraft
    public var contextRevision: String

    public init(hostID: String, documentID: String, expectedContentHash: HostContentHash, draft: HostDraft, contextRevision: String) {
        self.hostID = hostID
        self.documentID = documentID
        self.expectedContentHash = expectedContentHash
        self.draft = draft
        self.contextRevision = contextRevision
    }

    private enum CodingKeys: String, CodingKey {
        case hostID = "host_id"
        case documentID = "document_id"
        case expectedContentHash = "expected_content_hash"
        case draft
        case contextRevision = "context_revision"
    }
}

/// Result for `engine.v1.hosts.settings.validate`. Valid results require candidates.
public struct HostSettingsValidateResult: Codable, Equatable, Sendable {
    public var valid: Bool
    public var validationLevel: HostValidationLevel
    public var candidateContentHash: String?
    public var candidateRawTOML: String?
    public var candidateStructured: HostStructuredDocument?
    public var diagnostics: [HostDiagnostic]
    public var contextRevision: String

    public init(
        valid: Bool,
        validationLevel: HostValidationLevel,
        candidateContentHash: String? = nil,
        candidateRawTOML: String? = nil,
        candidateStructured: HostStructuredDocument? = nil,
        diagnostics: [HostDiagnostic],
        contextRevision: String
    ) {
        self.valid = valid
        self.validationLevel = validationLevel
        self.candidateContentHash = candidateContentHash
        self.candidateRawTOML = candidateRawTOML
        self.candidateStructured = candidateStructured
        self.diagnostics = diagnostics
        self.contextRevision = contextRevision
    }

    private enum CodingKeys: String, CodingKey {
        case valid
        case validationLevel = "validation_level"
        case candidateContentHash = "candidate_content_hash"
        case candidateRawTOML = "candidate_raw_toml"
        case candidateStructured = "candidate_structured"
        case diagnostics
        case contextRevision = "context_revision"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        valid = try container.decode(Bool.self, forKey: .valid)
        validationLevel = try container.decode(HostValidationLevel.self, forKey: .validationLevel)
        candidateContentHash = try container.decodeIfPresent(String.self, forKey: .candidateContentHash)
        candidateRawTOML = try container.decodeIfPresent(String.self, forKey: .candidateRawTOML)
        candidateStructured = try container.decodeIfPresent(HostStructuredDocument.self, forKey: .candidateStructured)
        diagnostics = try container.decode([HostDiagnostic].self, forKey: .diagnostics)
        contextRevision = try container.decode(String.self, forKey: .contextRevision)
        if valid, candidateContentHash == nil || candidateRawTOML == nil || candidateStructured == nil {
            throw DecodingError.dataCorruptedError(
                forKey: .candidateContentHash,
                in: container,
                debugDescription: "valid result requires candidates"
            )
        }
    }
}

/// Params for `engine.v1.hosts.settings.preview`.
public struct HostSettingsPreviewParams: Codable, Equatable, Sendable {
    public var hostID: String
    public var documentID: String
    public var expectedContentHash: HostContentHash
    public var draft: HostDraft
    public var contextRevision: String

    public init(hostID: String, documentID: String, expectedContentHash: HostContentHash, draft: HostDraft, contextRevision: String) {
        self.hostID = hostID
        self.documentID = documentID
        self.expectedContentHash = expectedContentHash
        self.draft = draft
        self.contextRevision = contextRevision
    }

    private enum CodingKeys: String, CodingKey {
        case hostID = "host_id"
        case documentID = "document_id"
        case expectedContentHash = "expected_content_hash"
        case draft
        case contextRevision = "context_revision"
    }
}

/// Server-computed diff and single-use token for a valid preview.
public struct HostSettingsPreview: Codable, Equatable, Sendable {
    public var previewID: String
    public var baseContentHash: HostContentHash
    public var candidateContentHash: String
    public var changed: Bool
    public var diff: String
    public var diffTruncated: Bool
    public var changedFieldIDs: [String]
    public var applicationEffects: [HostApplicationEffect]
    public var protectedProjectionChanges: Bool

    public init(
        previewID: String,
        baseContentHash: HostContentHash,
        candidateContentHash: String,
        changed: Bool,
        diff: String,
        diffTruncated: Bool,
        changedFieldIDs: [String],
        applicationEffects: [HostApplicationEffect],
        protectedProjectionChanges: Bool
    ) {
        self.previewID = previewID
        self.baseContentHash = baseContentHash
        self.candidateContentHash = candidateContentHash
        self.changed = changed
        self.diff = diff
        self.diffTruncated = diffTruncated
        self.changedFieldIDs = changedFieldIDs
        self.applicationEffects = applicationEffects
        self.protectedProjectionChanges = protectedProjectionChanges
    }

    private enum CodingKeys: String, CodingKey {
        case previewID = "preview_id"
        case baseContentHash = "base_content_hash"
        case candidateContentHash = "candidate_content_hash"
        case changed
        case diff
        case diffTruncated = "diff_truncated"
        case changedFieldIDs = "changed_field_ids"
        case applicationEffects = "application_effects"
        case protectedProjectionChanges = "protected_projection_changes"
    }
}

/// Result for `engine.v1.hosts.settings.preview`.
public struct HostSettingsPreviewResult: Codable, Equatable, Sendable {
    public var valid: Bool
    public var validationLevel: HostValidationLevel
    public var candidateContentHash: String?
    public var candidateRawTOML: String?
    public var candidateStructured: HostStructuredDocument?
    public var diagnostics: [HostDiagnostic]
    public var contextRevision: String
    public var preview: HostSettingsPreview?

    public init(
        valid: Bool,
        validationLevel: HostValidationLevel,
        candidateContentHash: String? = nil,
        candidateRawTOML: String? = nil,
        candidateStructured: HostStructuredDocument? = nil,
        diagnostics: [HostDiagnostic],
        contextRevision: String,
        preview: HostSettingsPreview? = nil
    ) {
        self.valid = valid
        self.validationLevel = validationLevel
        self.candidateContentHash = candidateContentHash
        self.candidateRawTOML = candidateRawTOML
        self.candidateStructured = candidateStructured
        self.diagnostics = diagnostics
        self.contextRevision = contextRevision
        self.preview = preview
    }

    private enum CodingKeys: String, CodingKey {
        case valid
        case validationLevel = "validation_level"
        case candidateContentHash = "candidate_content_hash"
        case candidateRawTOML = "candidate_raw_toml"
        case candidateStructured = "candidate_structured"
        case diagnostics
        case contextRevision = "context_revision"
        case preview
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        valid = try container.decode(Bool.self, forKey: .valid)
        validationLevel = try container.decode(HostValidationLevel.self, forKey: .validationLevel)
        candidateContentHash = try container.decodeIfPresent(String.self, forKey: .candidateContentHash)
        candidateRawTOML = try container.decodeIfPresent(String.self, forKey: .candidateRawTOML)
        candidateStructured = try container.decodeIfPresent(HostStructuredDocument.self, forKey: .candidateStructured)
        diagnostics = try container.decode([HostDiagnostic].self, forKey: .diagnostics)
        contextRevision = try container.decode(String.self, forKey: .contextRevision)
        guard container.contains(.preview) else {
            throw DecodingError.keyNotFound(
                CodingKeys.preview,
                DecodingError.Context(codingPath: container.codingPath, debugDescription: "preview key is required")
            )
        }
        if try container.decodeNil(forKey: .preview) {
            preview = nil
        } else {
            preview = try container.decode(HostSettingsPreview.self, forKey: .preview)
        }
        if valid {
            guard candidateContentHash != nil, candidateRawTOML != nil, candidateStructured != nil, preview != nil else {
                throw DecodingError.dataCorruptedError(
                    forKey: .preview,
                    in: container,
                    debugDescription: "valid preview result requires candidates and preview"
                )
            }
        } else if preview != nil {
            throw DecodingError.dataCorruptedError(
                forKey: .preview,
                in: container,
                debugDescription: "invalid preview result requires null preview"
            )
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(valid, forKey: .valid)
        try container.encode(validationLevel, forKey: .validationLevel)
        try container.encodeIfPresent(candidateContentHash, forKey: .candidateContentHash)
        try container.encodeIfPresent(candidateRawTOML, forKey: .candidateRawTOML)
        try container.encodeIfPresent(candidateStructured, forKey: .candidateStructured)
        try container.encode(diagnostics, forKey: .diagnostics)
        try container.encode(contextRevision, forKey: .contextRevision)
        if let preview {
            try container.encode(preview, forKey: .preview)
        } else {
            try container.encodeNil(forKey: .preview)
        }
    }
}

/// Params for `engine.v1.hosts.settings.save`.
public struct HostSettingsSaveParams: Codable, Equatable, Sendable {
    public var hostID: String
    public var documentID: String
    public var expectedContentHash: HostContentHash
    public var contextRevision: String
    public var previewID: String
    public var candidateContentHash: String
    public var candidateRawTOML: String
    public var idempotencyKey: String

    public init(
        hostID: String,
        documentID: String,
        expectedContentHash: HostContentHash,
        contextRevision: String,
        previewID: String,
        candidateContentHash: String,
        candidateRawTOML: String,
        idempotencyKey: String
    ) {
        self.hostID = hostID
        self.documentID = documentID
        self.expectedContentHash = expectedContentHash
        self.contextRevision = contextRevision
        self.previewID = previewID
        self.candidateContentHash = candidateContentHash
        self.candidateRawTOML = candidateRawTOML
        self.idempotencyKey = idempotencyKey
    }

    private enum CodingKeys: String, CodingKey {
        case hostID = "host_id"
        case documentID = "document_id"
        case expectedContentHash = "expected_content_hash"
        case contextRevision = "context_revision"
        case previewID = "preview_id"
        case candidateContentHash = "candidate_content_hash"
        case candidateRawTOML = "candidate_raw_toml"
        case idempotencyKey = "idempotency_key"
    }
}

/// Pre-write backup reference in a save result. Null when nothing changed.
public struct HostSettingsSaveBackup: Codable, Equatable, Sendable {
    public var backupID: String
    public var displayPath: String

    public init(backupID: String, displayPath: String) {
        self.backupID = backupID
        self.displayPath = displayPath
    }

    private enum CodingKeys: String, CodingKey {
        case backupID = "backup_id"
        case displayPath = "display_path"
    }
}

/// Result for `engine.v1.hosts.settings.save`.
public struct HostSettingsSaveResult: Codable, Equatable, Sendable {
    public var saved: Bool
    public var changed: Bool
    public var documentID: String
    public var previousContentHash: HostContentHash
    public var documentRevision: String
    public var backup: HostSettingsSaveBackup?
    public var applicationEffects: [HostApplicationEffect]
    public var contextRevision: String

    public init(
        saved: Bool,
        changed: Bool,
        documentID: String,
        previousContentHash: HostContentHash,
        documentRevision: String,
        backup: HostSettingsSaveBackup?,
        applicationEffects: [HostApplicationEffect],
        contextRevision: String
    ) {
        self.saved = saved
        self.changed = changed
        self.documentID = documentID
        self.previousContentHash = previousContentHash
        self.documentRevision = documentRevision
        self.backup = backup
        self.applicationEffects = applicationEffects
        self.contextRevision = contextRevision
    }

    private enum CodingKeys: String, CodingKey {
        case saved
        case changed
        case documentID = "document_id"
        case previousContentHash = "previous_content_hash"
        case documentRevision = "document_revision"
        case backup
        case applicationEffects = "application_effects"
        case contextRevision = "context_revision"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        saved = try container.decode(Bool.self, forKey: .saved)
        changed = try container.decode(Bool.self, forKey: .changed)
        documentID = try container.decode(String.self, forKey: .documentID)
        previousContentHash = try container.decode(HostContentHash.self, forKey: .previousContentHash)
        documentRevision = try container.decode(String.self, forKey: .documentRevision)
        guard container.contains(.backup) else {
            throw DecodingError.keyNotFound(
                CodingKeys.backup,
                DecodingError.Context(codingPath: container.codingPath, debugDescription: "backup key is required")
            )
        }
        if try container.decodeNil(forKey: .backup) {
            backup = nil
        } else {
            backup = try container.decode(HostSettingsSaveBackup.self, forKey: .backup)
        }
        applicationEffects = try container.decode([HostApplicationEffect].self, forKey: .applicationEffects)
        contextRevision = try container.decode(String.self, forKey: .contextRevision)
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(saved, forKey: .saved)
        try container.encode(changed, forKey: .changed)
        try container.encode(documentID, forKey: .documentID)
        try container.encode(previousContentHash, forKey: .previousContentHash)
        try container.encode(documentRevision, forKey: .documentRevision)
        if let backup {
            try container.encode(backup, forKey: .backup)
        } else {
            try container.encodeNil(forKey: .backup)
        }
        try container.encode(applicationEffects, forKey: .applicationEffects)
        try container.encode(contextRevision, forKey: .contextRevision)
    }
}

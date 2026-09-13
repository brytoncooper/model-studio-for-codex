import Foundation
import ModelDeckContracts

// Pure Swift panel document values and semantic validator.
//
// Decodes the versioned `contracts/ui.panel.v1/tree.schema.json` shape and
// enforces the semantic rules the schema leaves to a later pass: max depth,
// max nodes, unique ids, `ready` requires `root`, `field_bindings` targets
// resolve to `text_input` nodes in the same tree, and a key that appears in
// both `params` and `field_bindings` is rejected (the two maps never merge
// and neither overrides the other).
//
// Renderer-agnostic: pure Foundation only, no AppKit, no kernel/provider
// reach-through. Node values carry exactly what the schema requires plus the
// derived `hasStaleRoot` flag the renderer needs to disable button actions
// and `text_input` edits when `state` is non-`ready`. Anything else
// (accessibility wiring, viewdata, button-enablement policy) belongs to the
// future renderer, not here.

/// Stable limits enforced by `PanelDocumentCodec.decode(_:)`.
public enum PanelDocumentLimits {
    /// Hard cap on raw payload size (1 MiB).
    public static let maxRawBytes: Int = 1 << 20
    /// Maximum tree depth (root counts as depth 1).
    public static let maxDepth: Int = 16
    /// Maximum total nodes across the whole tree.
    public static let maxNodes: Int = 256
    /// Maximum children a single `stack` may carry.
    public static let maxChildrenPerStack: Int = 64
    /// Maximum keys in a single `params` map.
    public static let maxParamsKeys: Int = 64
    /// Maximum keys in a single `field_bindings` map.
    public static let maxBindingsKeys: Int = 64
    /// Internal safety ceiling for `params` JSON nesting depth.
    public static let maxJsonValueDepth: Int = 64
    /// Maximum Unicode scalars in a node id (the schema caps at 64).
    static let maxNodeIDScalars: Int = 64

    // Scalar string bounds, mirroring the schema.
    static let titleMinScalars = 1
    static let titleMaxScalars = 256
    static let messageMinScalars = 1
    static let messageMaxScalars = 4096
    static let textValueMinScalars = 1
    static let textValueMaxScalars = 8192
    static let textInputValueMinScalars = 0
    static let textInputValueMaxScalars = 65536
    static let labelMinScalars = 1
    static let labelMaxScalars = 256

    // Shared wire-type bounds (json_value schema).
    static let jsonValueStringMaxScalars = 1 << 20
    static let jsonValueArrayMaxItems = 4096
    static let jsonValueObjectMaxKeys = 1024
}

/// Validated declarative panel tree.
///
/// The renderer reads this snapshot only. The renderer never sees raw JSON,
/// the original payload bytes, or any field that failed validation. A non-
/// `ready` state may carry a stale `root` from a previous ready render; the
/// renderer uses `hasStaleRoot` to disable all `button` actions and
/// `text_input` edits in that case.
public struct PanelDocument: Equatable {
    public let panelID: String
    public let revision: Int
    public let title: String
    public let state: PanelState
    public let message: String?
    public let root: PanelNode?

    init(
        panelID: String,
        revision: Int,
        title: String,
        state: PanelState,
        message: String? = nil,
        root: PanelNode? = nil
    ) {
        self.panelID = panelID
        self.revision = revision
        self.title = title
        self.state = state
        self.message = message
        self.root = root
    }

    /// `true` when `state == .ready`.
    public var isReady: Bool { state == .ready }

    /// `true` when `state` is not `ready` and a `root` is still present
    /// (retained from a previous ready render). The renderer must treat the
    /// `root` as stale and disable every `button` and `text_input` action.
    public var hasStaleRoot: Bool { state != .ready && root != nil }
}

/// Lifecycle state of a panel tree.
public enum PanelState: String, Equatable, Sendable, Codable, CaseIterable {
    case loading
    case ready
    case empty
    case error
    case unavailable
}

/// Discriminator for the four node kinds.
public enum NodeKind: String, Equatable, Sendable, Codable {
    case stack
    case text
    case textInput = "text_input"
    case button
}

/// A node id satisfies `^[a-z][a-z0-9_-]{0,63}$` (1..=64 chars, ASCII,
/// lowercase first character). The codec constructs this value only after
/// validating the wire string.
public struct NodeID: Equatable, Hashable, Sendable, CustomStringConvertible {
    public let rawValue: String

    init(_ rawValue: String) throws {
        guard Self.isValid(rawValue) else {
            throw PanelDocumentError.invalidNodeIDFormat
        }
        self.rawValue = rawValue
    }

    public var description: String { rawValue }

    internal static func isValid(_ s: String) -> Bool {
        let scalars = s.unicodeScalars
        guard !scalars.isEmpty, scalars.count <= PanelDocumentLimits.maxNodeIDScalars else {
            return false
        }
        guard let first = scalars.first, (97...122).contains(first.value) else {
            return false
        }
        for scalar in scalars.dropFirst() {
            let value = scalar.value
            let isLowercaseASCII = (97...122).contains(value)
            let isDigitASCII = (48...57).contains(value)
            guard isLowercaseASCII || isDigitASCII || value == 95 || value == 45 else {
                return false
            }
        }
        return true
    }
}

/// A panel tree node. Pattern-match on the four cases to dispatch.
public indirect enum PanelNode: Equatable {
    case stack(StackNode)
    case text(TextNode)
    case textInput(TextInputNode)
    case button(ButtonNode)

    /// The id of this node. `O(1)`.
    public var id: NodeID {
        switch self {
        case .stack(let n): return n.id
        case .text(let n): return n.id
        case .textInput(let n): return n.id
        case .button(let n): return n.id
        }
    }

    /// The kind discriminator. `O(1)`.
    public var kind: NodeKind {
        switch self {
        case .stack: return .stack
        case .text: return .text
        case .textInput: return .textInput
        case .button: return .button
        }
    }
}

/// A `stack` node: a vertical layout container of one or more child nodes.
public struct StackNode: Equatable {
    public let id: NodeID
    public let children: [PanelNode]

    init(id: NodeID, children: [PanelNode]) {
        self.id = id
        self.children = children
    }
}

/// A `text` node: a static string value rendered as a line of text.
public struct TextNode: Equatable, Sendable {
    public let id: NodeID
    public let value: String

    init(id: NodeID, value: String) {
        self.id = id
        self.value = value
    }
}

/// A `text_input` node: an editable single- or multi-line field.
///
/// `label` is required, non-empty, and exists for visible labeling plus
/// assistive association; the renderer is responsible for applying it to
/// the field's accessibility label.
public struct TextInputNode: Equatable, Sendable {
    public let id: NodeID
    public let value: String
    public let multiline: Bool
    public let label: String

    init(id: NodeID, value: String, multiline: Bool, label: String) {
        self.id = id
        self.value = value
        self.multiline = multiline
        self.label = label
    }
}

/// A `button` node: invokes one `operation_id`.
///
/// `params` carries static JSON values preserved as `JSONValue`. `field_bindings`
/// maps a `params` key to a `text_input` node id in the same tree; at invoke
/// time the host injects the current value of the referenced `text_input` into
/// that key. The two maps never merge; a key present in both is rejected at
/// decode time by the semantic validator.
public struct ButtonNode: Equatable {
    public let id: NodeID
    public let label: String
    public let operationID: String
    public let params: [String: JSONValue]
    public let fieldBindings: [String: NodeID]

    init(
        id: NodeID,
        label: String,
        operationID: String,
        params: [String: JSONValue] = [:],
        fieldBindings: [String: NodeID] = [:]
    ) {
        self.id = id
        self.label = label
        self.operationID = operationID
        self.params = params
        self.fieldBindings = fieldBindings
    }
}

/// Stable typed errors from `PanelDocumentCodec.decode(_:)`.
///
/// `description` is machine-stable: it identifies the error class and any
/// fixed schema field or bound, but never source-controlled keys, kinds, ids,
/// binding names, raw values, or snippets. `Equatable` for tests; `Sendable`.
public enum PanelDocumentError: Error, Equatable, Sendable {
    case payloadTooLarge
    case payloadEmpty
    case invalidUTF8
    case invalidJSON
    case rootNotObject

    case missingField(String)
    case unknownField

    case invalidNodeKind
    case invalidPanelIDFormat
    case invalidOperationIDFormat(String)
    case invalidNodeIDFormat
    case revisionOutOfRange

    case stringLengthOutOfRange(field: String, min: Int, max: Int)
    case collectionTooSmall(field: String, min: Int)
    case collectionTooLarge(field: String, max: Int)

    case paramsTooLarge
    case bindingsTooLarge
    case paramsBindingsCollision

    case depthExceeded
    case nodeCountExceeded
    case duplicateNodeID
    case rootRequiredForReady

    case bindingTargetNotFound
    case bindingTargetNotTextInput

    case jsonValueStringTooLong
    case jsonValueArrayTooLarge
    case jsonValueObjectTooLarge
    case jsonValueDepthExceeded
    case jsonValueUnsupported
}

extension PanelDocumentError: CustomStringConvertible {
    public var description: String {
        switch self {
        case .payloadTooLarge: return "panel payload exceeds max raw bytes"
        case .payloadEmpty: return "panel payload is empty"
        case .invalidUTF8: return "panel payload is not valid UTF-8"
        case .invalidJSON: return "panel payload is not valid JSON"
        case .rootNotObject: return "panel root must be a JSON object"

        case .missingField(let f): return "panel document missing required field: \(f)"
        case .unknownField: return "panel document has an unknown field"

        case .invalidNodeKind: return "panel node has an invalid kind"
        case .invalidPanelIDFormat: return "panel_id does not match reverse_domain_id format"
        case .invalidOperationIDFormat(let f):
            return "operation_id on \(f) does not match reverse_domain_id format"
        case .invalidNodeIDFormat: return "node id does not match required pattern"
        case .revisionOutOfRange:
            return "revision is outside the supported nonnegative Int range"

        case .stringLengthOutOfRange(let f, let mn, let mx):
            return "string field \(f) length is out of range [\(mn), \(mx)]"
        case .collectionTooSmall(let f, let mn):
            return "array/object \(f) has fewer than \(mn) item(s)"
        case .collectionTooLarge(let f, let mx):
            return "array/object \(f) exceeds \(mx) item(s)"

        case .paramsTooLarge: return "button params map exceeds 64 keys"
        case .bindingsTooLarge: return "button field_bindings map exceeds 64 keys"
        case .paramsBindingsCollision:
            return "a key is present in both params and field_bindings"

        case .depthExceeded: return "panel tree exceeds maximum depth of 16"
        case .nodeCountExceeded: return "panel tree exceeds maximum node count of 256"
        case .duplicateNodeID: return "panel tree contains a duplicate node id"
        case .rootRequiredForReady: return "state=ready requires a non-null root"

        case .bindingTargetNotFound:
            return "a field_bindings target node id was not found in the tree"
        case .bindingTargetNotTextInput:
            return "a field_bindings target is not a text_input"

        case .jsonValueStringTooLong: return "params string value exceeds json_value bound"
        case .jsonValueArrayTooLarge: return "params array exceeds json_value bound"
        case .jsonValueObjectTooLarge: return "params object exceeds json_value bound"
        case .jsonValueDepthExceeded: return "params JSON nesting exceeds safe depth"
        case .jsonValueUnsupported: return "params contained an unsupported JSON value"
        }
    }
}

/// Decoder for versioned panel documents.
///
/// Stateless and pure: a single `decode(_:)` call walks the payload once and
/// either returns a fully validated `PanelDocument` or throws the first
/// `PanelDocumentError`. The codec never reads from the kernel/provider, the
/// AppKit view layer, or any external discovery service: it only consumes
/// raw bytes and the public `JSONValue` contract type.
public enum PanelDocumentCodec {
    public static let maxRawBytes: Int = PanelDocumentLimits.maxRawBytes
    public static let maxDepth: Int = PanelDocumentLimits.maxDepth
    public static let maxNodes: Int = PanelDocumentLimits.maxNodes
    public static let maxParamsKeys: Int = PanelDocumentLimits.maxParamsKeys
    public static let maxBindingsKeys: Int = PanelDocumentLimits.maxBindingsKeys
    public static let maxChildrenPerStack: Int = PanelDocumentLimits.maxChildrenPerStack

    /// Decode and validate a raw panel payload.
    ///
    /// On success the returned `PanelDocument` has passed this codec's
    /// structural and local semantic checks and is ready for the renderer to
    /// consume. Operation discovery and authority checks remain host-owned.
    /// On failure a `PanelDocumentError` is thrown; error descriptions identify
    /// the failing category, fixed schema field, or bound but never include
    /// source-controlled content.
    public static func decode(_ data: Data) throws -> PanelDocument {
        if data.isEmpty {
            throw PanelDocumentError.payloadEmpty
        }
        if data.count > maxRawBytes {
            throw PanelDocumentError.payloadTooLarge
        }
        // Cheap pre-check that the payload decodes as UTF-8 so we don't pass
        // arbitrary bytes to JSONSerialization (it tolerates any bytes that
        // form valid JSON, but a non-UTF8 garbage payload would still surface
        // as a generic decoding error rather than a meaningful one).
        if String(data: data, encoding: .utf8) == nil {
            throw PanelDocumentError.invalidUTF8
        }

        let any: Any
        do {
            any = try JSONSerialization.jsonObject(
                with: data,
                options: [.fragmentsAllowed]
            )
        } catch {
            throw PanelDocumentError.invalidJSON
        }
        guard let root = any as? [String: Any] else {
            throw PanelDocumentError.rootNotObject
        }

        var validator = _Validator()
        let document = try validator.decodeDocument(root)

        // Post-walk semantic checks that need the full tree in hand.
        try document.verifySemantics()
        return document
    }
}

// MARK: - Private validator

private struct _Validator {
    var seenIDs: [String: Int] = [:]
    var totalNodes: Int = 0

    mutating func decodeDocument(_ raw: [String: Any]) throws -> PanelDocument {
        // Allowlist panel-level keys (mirrors the schema's `properties` set).
        let allowed: Set<String> = ["panel_id", "revision", "title", "state", "message", "root"]
        try requireFields(raw, required: ["panel_id", "revision", "title", "state"])
        try rejectUnknownFields(raw, allowed: allowed)

        let panelID = try decodeReverseDomainID(raw["panel_id"], field: "panel_id")
        let revision = try decodeRevision(raw["revision"])
        let title = try decodeBoundedString(
            raw["title"], field: "title",
            min: PanelDocumentLimits.titleMinScalars,
            max: PanelDocumentLimits.titleMaxScalars
        )
        let state = try decodeState(raw["state"])
        let message: String?
        if let rawMessage = raw["message"] {
            message = try decodeBoundedString(
                rawMessage, field: "message",
                min: PanelDocumentLimits.messageMinScalars,
                max: PanelDocumentLimits.messageMaxScalars
            )
        } else {
            message = nil
        }

        let rootRaw = raw["root"]
        let rootNode: PanelNode?
        if let rootValue = rootRaw {
            rootNode = try decodeNode(rootValue, depth: 1)
        } else {
            rootNode = nil
        }

        if state == .ready && rootNode == nil {
            throw PanelDocumentError.rootRequiredForReady
        }

        return PanelDocument(
            panelID: panelID,
            revision: revision,
            title: title,
            state: state,
            message: message,
            root: rootNode
        )
    }

    // MARK: Node decode

    mutating func decodeNode(_ value: Any, depth: Int) throws -> PanelNode {
        if depth > PanelDocumentLimits.maxDepth {
            throw PanelDocumentError.depthExceeded
        }
        guard let dict = value as? [String: Any] else {
            throw PanelDocumentError.invalidJSON
        }

        // All node kinds share `id` and `kind`. Reject anything else at the
        // per-kind level so unknown fields surface as `unknownField` rather
        // than silently dropping.
        try requireFields(dict, required: ["id", "kind"])
        let idRaw = try decodeNodeIDRaw(dict["id"])
        let kindRaw = try decodeKindRaw(dict["kind"])

        totalNodes += 1
        if totalNodes > PanelDocumentLimits.maxNodes {
            throw PanelDocumentError.nodeCountExceeded
        }
        if seenIDs[idRaw] != nil {
            throw PanelDocumentError.duplicateNodeID
        }
        seenIDs[idRaw] = (seenIDs[idRaw] ?? 0) + 1

        switch kindRaw {
        case "stack":
            try rejectUnknownFields(dict, allowed: ["id", "kind", "children"])
            guard let childrenValue = dict["children"] else {
                throw PanelDocumentError.missingField("children")
            }
            let children = try decodeChildren(childrenValue, depth: depth)
            let id = try NodeID(idRaw)
            return .stack(StackNode(id: id, children: children))
        case "text":
            try rejectUnknownFields(dict, allowed: ["id", "kind", "value"])
            guard let valueRaw = dict["value"] else {
                throw PanelDocumentError.missingField("value")
            }
            let value = try decodeBoundedString(
                valueRaw, field: "text.value",
                min: PanelDocumentLimits.textValueMinScalars,
                max: PanelDocumentLimits.textValueMaxScalars
            )
            let id = try NodeID(idRaw)
            return .text(TextNode(id: id, value: value))
        case "text_input":
            try rejectUnknownFields(
                dict,
                allowed: ["id", "kind", "value", "multiline", "label"]
            )
            guard let valueRaw = dict["value"] else {
                throw PanelDocumentError.missingField("value")
            }
            guard let multilineRaw = dict["multiline"] else {
                throw PanelDocumentError.missingField("multiline")
            }
            guard let labelRaw = dict["label"] else {
                throw PanelDocumentError.missingField("label")
            }
            let value = try decodeBoundedString(
                valueRaw, field: "text_input.value",
                min: PanelDocumentLimits.textInputValueMinScalars,
                max: PanelDocumentLimits.textInputValueMaxScalars
            )
            let multiline = try decodeBool(multilineRaw)
            let label = try decodeBoundedString(
                labelRaw, field: "text_input.label",
                min: PanelDocumentLimits.labelMinScalars,
                max: PanelDocumentLimits.labelMaxScalars
            )
            let id = try NodeID(idRaw)
            return .textInput(TextInputNode(id: id, value: value, multiline: multiline, label: label))
        case "button":
            try rejectUnknownFields(
                dict,
                allowed: ["id", "kind", "label", "operation_id", "params", "field_bindings"]
            )
            guard let labelRaw = dict["label"] else {
                throw PanelDocumentError.missingField("label")
            }
            guard let opIDRaw = dict["operation_id"] else {
                throw PanelDocumentError.missingField("operation_id")
            }
            let label = try decodeBoundedString(
                labelRaw, field: "button.label",
                min: PanelDocumentLimits.labelMinScalars,
                max: PanelDocumentLimits.labelMaxScalars
            )
            let operationID = try decodeReverseDomainID(opIDRaw, field: "button.operation_id")

            let paramsDict: [String: Any]
            if let paramsRaw = dict["params"] {
                guard let p = paramsRaw as? [String: Any] else {
                    throw PanelDocumentError.invalidJSON
                }
                paramsDict = p
            } else {
                paramsDict = [:]
            }
            if paramsDict.count > PanelDocumentLimits.maxParamsKeys {
                throw PanelDocumentError.paramsTooLarge
            }
            var params: [String: JSONValue] = [:]
            params.reserveCapacity(paramsDict.count)
            for (k, v) in paramsDict {
                params[k] = try _JSONValueBuilder.build(v, depth: 0)
            }

            let bindingsDict: [String: Any]
            if let bindingsRaw = dict["field_bindings"] {
                guard let b = bindingsRaw as? [String: Any] else {
                    throw PanelDocumentError.invalidJSON
                }
                bindingsDict = b
            } else {
                bindingsDict = [:]
            }
            if bindingsDict.count > PanelDocumentLimits.maxBindingsKeys {
                throw PanelDocumentError.bindingsTooLarge
            }
            var fieldBindings: [String: NodeID] = [:]
            fieldBindings.reserveCapacity(bindingsDict.count)
            for (k, v) in bindingsDict {
                let raw = try decodeNodeIDRaw(v)
                guard let nodeID = try? NodeID(raw) else {
                    throw PanelDocumentError.invalidNodeIDFormat
                }
                fieldBindings[k] = nodeID
            }

            // Collision check: a key in both maps is rejected outright; the
            // maps never merge and neither overrides the other.
            for key in params.keys where fieldBindings.keys.contains(key) {
                throw PanelDocumentError.paramsBindingsCollision
            }

            let id = try NodeID(idRaw)
            return .button(ButtonNode(
                id: id,
                label: label,
                operationID: operationID,
                params: params,
                fieldBindings: fieldBindings
            ))
        default:
            throw PanelDocumentError.invalidNodeKind
        }
    }

    mutating func decodeChildren(_ value: Any, depth: Int) throws -> [PanelNode] {
        guard let array = value as? [Any] else {
            throw PanelDocumentError.invalidJSON
        }
        if array.count < 1 {
            throw PanelDocumentError.collectionTooSmall(field: "stack.children", min: 1)
        }
        if array.count > PanelDocumentLimits.maxChildrenPerStack {
            throw PanelDocumentError.collectionTooLarge(
                field: "stack.children",
                max: PanelDocumentLimits.maxChildrenPerStack
            )
        }
        var out: [PanelNode] = []
        out.reserveCapacity(array.count)
        for child in array {
            out.append(try decodeNode(child, depth: depth + 1))
        }
        return out
    }

    // MARK: Field-level helpers

    func requireFields(_ raw: [String: Any], required: [String]) throws {
        for key in required where raw[key] == nil {
            throw PanelDocumentError.missingField(key)
        }
    }

    func rejectUnknownFields(_ raw: [String: Any], allowed: Set<String>) throws {
        if !raw.keys.allSatisfy({ allowed.contains($0) }) {
            throw PanelDocumentError.unknownField
        }
    }

    func decodeBoundedString(_ value: Any, field: String, min: Int, max: Int) throws -> String {
        guard let s = value as? String else {
            throw PanelDocumentError.invalidJSON
        }
        let scalarCount = s.unicodeScalars.count
        if scalarCount < min || scalarCount > max {
            throw PanelDocumentError.stringLengthOutOfRange(field: field, min: min, max: max)
        }
        return s
    }

    func decodeBool(_ value: Any) throws -> Bool {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) == CFBooleanGetTypeID()
        else {
            throw PanelDocumentError.invalidJSON
        }
        return number.boolValue
    }

    func decodeRevision(_ value: Any) throws -> Int {
        guard let n = value as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID() else {
            throw PanelDocumentError.invalidJSON
        }
        if let revision = n as? Int {
            if revision < 0 {
                throw PanelDocumentError.revisionOutOfRange
            }
            return revision
        }

        let numericValue = n.doubleValue
        if numericValue.isFinite,
           numericValue.rounded(.towardZero) != numericValue {
            throw PanelDocumentError.invalidJSON
        }
        if numericValue.isFinite {
            throw PanelDocumentError.revisionOutOfRange
        }
        throw PanelDocumentError.invalidJSON
    }

    func decodeState(_ value: Any) throws -> PanelState {
        guard let raw = value as? String else {
            throw PanelDocumentError.invalidJSON
        }
        guard let state = PanelState(rawValue: raw) else {
            throw PanelDocumentError.invalidJSON
        }
        return state
    }

    func decodeKindRaw(_ value: Any) throws -> String {
        guard let raw = value as? String else {
            throw PanelDocumentError.invalidJSON
        }
        switch raw {
        case "stack", "text", "text_input", "button":
            return raw
        default:
            throw PanelDocumentError.invalidNodeKind
        }
    }

    func decodeNodeIDRaw(_ value: Any) throws -> String {
        guard let raw = value as? String, NodeID.isValid(raw) else {
            throw PanelDocumentError.invalidNodeIDFormat
        }
        return raw
    }

    func decodeReverseDomainID(_ value: Any, field: String) throws -> String {
        guard let raw = value as? String else {
            throw PanelDocumentError.invalidJSON
        }
        guard _ReverseDomainID.isValid(raw) else {
            if field == "panel_id" {
                throw PanelDocumentError.invalidPanelIDFormat
            }
            throw PanelDocumentError.invalidOperationIDFormat(field)
        }
        return raw
    }
}

// MARK: - Reverse-domain-id validator

/// Validates `reverse_domain_id`: `^[a-z][a-z0-9]*(\.[a-z][a-z0-9_-]*)+$`,
/// length 3..=256. Centralised here so the schema's regex matches the
/// decoder exactly without duplicating string handling elsewhere.
private enum _ReverseDomainID {
    private static let pattern: NSRegularExpression = {
        // swiftlint:disable:next force_try
        try! NSRegularExpression(pattern: "^[a-z][a-z0-9]*(\\.[a-z][a-z0-9_-]*)+$")
    }()

    static func isValid(_ s: String) -> Bool {
        let scalarCount = s.unicodeScalars.count
        guard scalarCount >= 3, scalarCount <= 256 else { return false }
        let range = NSRange(location: 0, length: s.utf16.count)
        return pattern.firstMatch(in: s, options: [], range: range) != nil
    }
}

// MARK: - JSONValue builder for button params

/// Converts an `Any` from `JSONSerialization` into a `JSONValue`, enforcing
/// the `json_value` schema's string/array/object bounds at every level so
/// the host never receives a `JSONValue` that the dispatcher would reject.
private enum _JSONValueBuilder {
    static func build(_ value: Any, depth: Int) throws -> JSONValue {
        if depth >= PanelDocumentLimits.maxJsonValueDepth {
            throw PanelDocumentError.jsonValueDepthExceeded
        }
        // NSNull first; JSONSerialization surfaces missing object values as NSNull.
        if value is NSNull {
            return .null
        }
        // Booleans: NSNumber wraps Bool too. Check via CFBooleanGetTypeID
        // because a Swift `as? Bool` cast on a numeric NSNumber would crash.
        if let n = value as? NSNumber {
            if CFGetTypeID(n) == CFBooleanGetTypeID() {
                return .bool(n.boolValue)
            }
            // Numbers: accept both integer-typed and float-typed NSNumber; the
            // dispatcher schema permits both. Round-trip is preserved through
            // `.doubleValue` regardless of underlying integer-ness.
            return .number(n.doubleValue)
        }
        if let s = value as? String {
            if s.unicodeScalars.count > PanelDocumentLimits.jsonValueStringMaxScalars {
                throw PanelDocumentError.jsonValueStringTooLong
            }
            return .string(s)
        }
        if let arr = value as? [Any] {
            if arr.count > PanelDocumentLimits.jsonValueArrayMaxItems {
                throw PanelDocumentError.jsonValueArrayTooLarge
            }
            var out: [JSONValue] = []
            out.reserveCapacity(arr.count)
            for item in arr {
                out.append(try build(item, depth: depth + 1))
            }
            return .array(out)
        }
        if let dict = value as? [String: Any] {
            if dict.count > PanelDocumentLimits.jsonValueObjectMaxKeys {
                throw PanelDocumentError.jsonValueObjectTooLarge
            }
            var out: [String: JSONValue] = [:]
            out.reserveCapacity(dict.count)
            for (k, v) in dict {
                out[k] = try build(v, depth: depth + 1)
            }
            return .object(out)
        }
        throw PanelDocumentError.jsonValueUnsupported
    }
}

// MARK: - Post-decode semantic checks (run after a successful decode)

extension PanelDocument {
    /// Re-checks the post-decode semantic invariants the per-node walker
    /// cannot fully verify because it needs the whole tree in hand:
    /// `field_bindings` targets must exist as `text_input` nodes in this tree.
    ///
    /// `PanelDocumentCodec.decode(_:)` runs this automatically after the
    /// structural decoding pass.
    func verifySemantics() throws {
        guard let root else { return }
        var registry = _SemanticRegistry()
        registry.register(root)
        try registry.verifyBindings()
    }
}

private final class _SemanticRegistry {
    var nodesByID: [String: PanelNode] = [:]
    var bindingTargets: [String] = []

    func register(_ node: PanelNode) {
        let key = node.id.rawValue
        nodesByID[key] = node
        if case let .button(b) = node {
            for target in b.fieldBindings.values {
                bindingTargets.append(target.rawValue)
            }
        }
        if case let .stack(s) = node {
            for child in s.children {
                register(child)
            }
        }
    }

    func verifyBindings() throws {
        for target in bindingTargets {
            guard let node = nodesByID[target] else {
                throw PanelDocumentError.bindingTargetNotFound
            }
            if case .textInput = node { continue }
            throw PanelDocumentError.bindingTargetNotTextInput
        }
    }
}

// MARK: - Tree helpers for the renderer

extension PanelNode {
    /// Depth-first walk of the subtree rooted at this node. `visit` is
    /// called once for this node and once per descendant. Safe to use
    /// from the renderer's main-thread view code; the walker is non-
    /// reentrant (no mutation expected from `visit`).
    public func walk(_ visit: (PanelNode) -> Void) {
        visit(self)
        if case let .stack(s) = self {
            for child in s.children {
                child.walk(visit)
            }
        }
    }

    /// Counts the total nodes under this node, including the receiver.
    public func totalNodeCount() -> Int {
        var count = 0
        walk { _ in count += 1 }
        return count
    }

    /// Counts only `text_input` nodes under this node. `0` if none.
    public func textInputCount() -> Int {
        var count = 0
        walk { node in
            if case .textInput = node { count += 1 }
        }
        return count
    }

    /// Finds the node with the given id in this subtree, or `nil` if absent.
    public func node(withID id: NodeID) -> PanelNode? {
        if self.id == id { return self }
        guard case let .stack(s) = self else { return nil }
        for child in s.children {
            if let found = child.node(withID: id) { return found }
        }
        return nil
    }
}

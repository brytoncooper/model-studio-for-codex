import Foundation

public struct SchemaValidationError: Error, Equatable, CustomStringConvertible {
    public let message: String
    public init(_ message: String) { self.message = message }
    public var description: String { message }
}

public enum SchemaValidator {
    private static let allowedKeywords: Set<String> = [
        "$id", "$ref", "$schema", "additionalProperties", "allOf", "const", "default",
        "definitions", "description", "enum", "format", "items", "maxItems", "maxLength",
        "maxProperties", "maximum", "minItems", "minLength", "minimum", "not", "oneOf",
        "pattern", "properties", "required", "title", "type",
    ]
    private static let allowedFormats: Set<String> = ["uuid", "date-time", "uri"]
    private static let maxDepth = 64
    private static let maxNodes = 200_000
    private static let maxSchemaFrames = 128

    private static var registryLock = NSLock()
    private static var documentCache: [String: [String: Any]] = [:]

    public static func resetCache() {
        registryLock.lock()
        documentCache = [:]
        registryLock.unlock()
    }

    public static func contractsRoot() -> URL {
        #if SWIFT_PACKAGE
        return Bundle.module.resourceURL!
            .appendingPathComponent("contracts", isDirectory: true)
        #else
        return Bundle(for: SchemaValidatorBundleToken.self)
            .resourceURL!
            .appendingPathComponent("contracts", isDirectory: true)
        #endif
    }

    public static func normalizeSchemaURI(_ uri: String) throws -> String {
        var value = uri.replacingOccurrences(of: "\\", with: "/")
        if value.hasPrefix("file://") {
            throw SchemaValidationError("file:// schema URIs are not allowed")
        }
        if value.hasPrefix("http://") || value.hasPrefix("https://") {
            throw SchemaValidationError("remote schema URI not allowed: \(uri)")
        }
        if !value.hasPrefix("contracts/") {
            value = "contracts/" + value.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        }
        return value
    }

    private static func confinedBundledSchemaURL(relativePath: String) throws -> URL {
        let normalizedRelative = relativePath.replacingOccurrences(of: "\\", with: "/")
        for component in normalizedRelative.split(separator: "/") {
            if component == ".." {
                throw SchemaValidationError("schema path outside bundle: \(relativePath)")
            }
        }
        let root = contractsRoot().resolvingSymlinksInPath()
        let candidate = root.appendingPathComponent(String(normalizedRelative)).resolvingSymlinksInPath()
        let rootPath = root.path
        let candidatePath = candidate.path
        if candidatePath != rootPath && !candidatePath.hasPrefix(rootPath + "/") {
            throw SchemaValidationError("schema path outside bundle: \(relativePath)")
        }
        return candidate
    }

    public static func loadSchemaDocument(relative: String) throws -> [String: Any] {
        let normalized = try normalizeSchemaURI(relative)
        registryLock.lock()
        if let cached = documentCache[normalized] {
            registryLock.unlock()
            return cached
        }
        registryLock.unlock()
        let relativePath = String(normalized.dropFirst("contracts/".count))
        let url = try confinedBundledSchemaURL(relativePath: relativePath)
        guard FileManager.default.fileExists(atPath: url.path) else {
            throw SchemaValidationError("schema not found in bundle: \(normalized)")
        }
        let data = try Data(contentsOf: url)
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw SchemaValidationError("schema root must be an object: \(normalized)")
        }
        try assertSubsetSchema(object, at: normalized)
        registryLock.lock()
        documentCache[normalized] = object
        if let schemaID = object["$id"] as? String {
            let idKey = try normalizeSchemaURI(schemaID)
            documentCache[idKey] = object
        }
        documentCache[relativePath.replacingOccurrences(of: "\\", with: "/")] = object
        registryLock.unlock()
        return object
    }

    public static func schemaNode(schemaRef: String) throws -> [String: Any] {
        let (documentPath, fragment) = try splitSchemaRef(schemaRef)
        let document = try loadSchemaDocument(relative: documentPath)
        if fragment.isEmpty {
            return document
        }
        return try navigateFragment(document, fragment: fragment)
    }

    public static func validate(instance: Any, schemaRef: String) throws {
        let counter = Counter()
        try walkInstanceBounds(instance, depth: 0, counter: counter)
        let schema = try schemaNode(schemaRef: schemaRef)
        let (documentPath, _) = try splitSchemaRef(schemaRef)
        let base = try normalizeSchemaURI(documentPath.isEmpty ? (schema["$id"] as? String ?? "contracts/unknown.schema.json") : documentPath)
        try validateInstance(instance, schema: schema, baseDocumentPath: base, frames: 0, nodeCounter: counter)
    }

    public static func validate(instance: Any, schema: [String: Any], baseDocumentPath: String) throws {
        let counter = Counter()
        try walkInstanceBounds(instance, depth: 0, counter: counter)
        let base = try normalizeSchemaURI(baseDocumentPath)
        try validateInstance(instance, schema: schema, baseDocumentPath: base, frames: 0, nodeCounter: counter)
    }

    private static func splitSchemaRef(_ ref: String) throws -> (String, String) {
        guard let hashIndex = ref.firstIndex(of: "#") else {
            return (ref, "")
        }
        let path = String(ref[..<hashIndex])
        let fragment = String(ref[ref.index(after: hashIndex)...])
        return (path, fragment)
    }

    private static func navigateFragment(_ document: [String: Any], fragment: String) throws -> [String: Any] {
        let trimmed = fragment.hasPrefix("/") ? String(fragment.dropFirst()) : fragment
        guard !trimmed.isEmpty else { return document }
        var node: Any = document
        for part in trimmed.split(separator: "/") {
            guard let dict = node as? [String: Any], let next = dict[String(part)] else {
                throw SchemaValidationError("schema fragment not found: \(fragment)")
            }
            node = next
        }
        guard let schema = node as? [String: Any] else {
            throw SchemaValidationError("schema fragment must be an object: \(fragment)")
        }
        return schema
    }

    private static func assertSubsetSchema(_ schema: [String: Any], at path: String) throws {
        for key in schema.keys {
            if !allowedKeywords.contains(key) {
                throw SchemaValidationError("unsupported schema keyword \(key) at \(path)")
            }
            if key == "additionalProperties", let value = schema[key] as? Bool, value {
                throw SchemaValidationError("additionalProperties: true is forbidden in contract schemas")
            }
        }
        if let definitions = schema["definitions"] as? [String: Any] {
            for (name, value) in definitions {
                guard let sub = value as? [String: Any] else { continue }
                try assertSubsetSchema(sub, at: "\(path)#/definitions/\(name)")
            }
        }
        for combinator in ["allOf", "oneOf"] {
            if let items = schema[combinator] as? [Any] {
                for (index, item) in items.enumerated() {
                    guard let sub = item as? [String: Any] else { continue }
                    try assertSubsetSchema(sub, at: "\(path)#/\(combinator)/\(index)")
                }
            }
        }
        if let not = schema["not"] as? [String: Any] {
            try assertSubsetSchema(not, at: "\(path)#/not")
        }
        if let properties = schema["properties"] as? [String: Any] {
            for (name, value) in properties {
                guard let sub = value as? [String: Any] else { continue }
                try assertSubsetSchema(sub, at: "\(path)#/properties/\(name)")
            }
        }
        if let items = schema["items"] as? [String: Any] {
            try assertSubsetSchema(items, at: "\(path)#/items")
        } else if let items = schema["items"] as? [Any] {
            for (index, item) in items.enumerated() {
                guard let sub = item as? [String: Any] else { continue }
                try assertSubsetSchema(sub, at: "\(path)#/items/\(index)")
            }
        }
        if let additional = schema["additionalProperties"] as? [String: Any] {
            try assertSubsetSchema(additional, at: "\(path)#/additionalProperties")
        }
    }

    private final class Counter {
        var count = 0
    }

    private static func walkInstanceBounds(_ value: Any, depth: Int, counter: Counter) throws {
        counter.count += 1
        if counter.count > maxNodes {
            throw SchemaValidationError("instance exceeds maximum node count")
        }
        if depth > maxDepth {
            throw SchemaValidationError("instance exceeds maximum depth")
        }
        if let number = value as? Double, !number.isFinite {
            throw SchemaValidationError("non-finite number")
        }
        if let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() {
            let d = number.doubleValue
            if !d.isFinite {
                throw SchemaValidationError("non-finite number")
            }
        }
        if let dict = value as? [String: Any] {
            for child in dict.values {
                try walkInstanceBounds(child, depth: depth + 1, counter: counter)
            }
        } else if let dict = value as? NSDictionary {
            for child in dict.allValues {
                try walkInstanceBounds(child, depth: depth + 1, counter: counter)
            }
        } else if let array = value as? [Any] {
            for child in array {
                try walkInstanceBounds(child, depth: depth + 1, counter: counter)
            }
        } else if let array = value as? NSArray {
            for child in array {
                try walkInstanceBounds(child, depth: depth + 1, counter: counter)
            }
        }
    }

    private static func validateInstance(
        _ instance: Any,
        schema: [String: Any],
        baseDocumentPath: String,
        frames: Int,
        nodeCounter: Counter
    ) throws {
        if frames > maxSchemaFrames {
            throw SchemaValidationError("schema reference depth exceeded")
        }
        try assertSubsetSchema(schema, at: baseDocumentPath)
        if let ref = schema["$ref"] as? String {
            let resolved = try resolveRef(ref, from: baseDocumentPath)
            try validateInstance(instance, schema: resolved.schema, baseDocumentPath: resolved.path, frames: frames + 1, nodeCounter: nodeCounter)
            return
        }
        if let not = schema["not"] as? [String: Any] {
            if try matches(instance, schema: not, baseDocumentPath: baseDocumentPath, frames: frames, nodeCounter: nodeCounter) {
                throw SchemaValidationError("instance matched forbidden schema")
            }
        }
        if let oneOf = schema["oneOf"] as? [Any] {
            var matchCount = 0
            for item in oneOf {
                guard let sub = item as? [String: Any] else { continue }
                if try matches(instance, schema: sub, baseDocumentPath: baseDocumentPath, frames: frames, nodeCounter: nodeCounter) {
                    matchCount += 1
                }
            }
            if matchCount != 1 {
                throw SchemaValidationError("instance must match exactly one oneOf branch (matched \(matchCount))")
            }
            return
        }
        if let allOf = schema["allOf"] as? [Any] {
            for item in allOf {
                guard let sub = item as? [String: Any] else {
                    throw SchemaValidationError("allOf entries must be objects")
                }
                try validateInstance(instance, schema: sub, baseDocumentPath: baseDocumentPath, frames: frames, nodeCounter: nodeCounter)
            }
            return
        }
        if let const = schema["const"] {
            if !jsonEqual(instance, const) {
                throw SchemaValidationError("instance does not match const")
            }
        }
        if let enumValues = schema["enum"] as? [Any] {
            if !enumValues.contains(where: { jsonEqual(instance, $0) }) {
                throw SchemaValidationError("instance not in enum")
            }
        }
        if let typeValue = schema["type"] {
            try validateTypes(instance, typeValue: typeValue)
        }
        if let format = schema["format"] as? String {
            if !allowedFormats.contains(format) {
                throw SchemaValidationError("unsupported format \(format)")
            }
            try validateFormat(instance, format: format)
        }
        if let minimum = schema["minimum"] as? NSNumber {
            try validateMinimum(instance, minimum: minimum)
        }
        if let maximum = schema["maximum"] as? NSNumber {
            try validateMaximum(instance, maximum: maximum)
        }
        if let minLength = schema["minLength"] as? Int {
            try validateMinLength(instance, minLength: minLength)
        }
        if let maxLength = schema["maxLength"] as? Int {
            try validateMaxLength(instance, maxLength: maxLength)
        }
        if let pattern = schema["pattern"] as? String {
            try validatePattern(instance, pattern: pattern)
        }
        if let minItems = schema["minItems"] as? Int {
            try validateMinItems(instance, minItems: minItems)
        }
        if let maxItems = schema["maxItems"] as? Int {
            try validateMaxItems(instance, maxItems: maxItems)
        }
        if schema["properties"] != nil || schema["required"] != nil || schema["additionalProperties"] != nil {
            let properties = schema["properties"] as? [String: Any] ?? [:]
            try validateObject(instance, properties: properties, schema: schema, baseDocumentPath: baseDocumentPath, nodeCounter: nodeCounter, frames: frames)
        } else if schema["type"] as? String == "object" || (schema["type"] as? [Any])?.contains(where: { ($0 as? String) == "object" }) == true {
            if let maxProperties = schema["maxProperties"] as? Int {
                try validateMaxProperties(instance, maxProperties: maxProperties)
            }
        }
        if let items = schema["items"] {
            try validateItems(instance, items: items, baseDocumentPath: baseDocumentPath, nodeCounter: nodeCounter, frames: frames)
        }
    }

    private static func matches(
        _ instance: Any,
        schema: [String: Any],
        baseDocumentPath: String,
        frames: Int,
        nodeCounter: Counter
    ) throws -> Bool {
        do {
            try validateInstance(instance, schema: schema, baseDocumentPath: baseDocumentPath, frames: frames, nodeCounter: Counter())
            return true
        } catch is SchemaValidationError {
            return false
        }
    }

    private static func resolveRef(_ ref: String, from baseDocumentPath: String) throws -> (path: String, schema: [String: Any]) {
        if ref.hasPrefix("#") {
            let document = try loadSchemaDocument(relative: baseDocumentPath)
            let fragment = ref.hasPrefix("#/") ? String(ref.dropFirst()) : String(ref.dropFirst())
            let node = try navigateFragment(document, fragment: fragment)
            return (baseDocumentPath, node)
        }
        let parts = ref.split(separator: "#", maxSplits: 1, omittingEmptySubsequences: false)
        let filePart = String(parts[0])
        let fragment = parts.count > 1 ? String(parts[1]) : ""
        let resolvedPath = try resolveRelativeSchemaPath(baseDocumentPath: baseDocumentPath, relativeFile: filePart)
        let document = try loadSchemaDocument(relative: resolvedPath)
        if fragment.isEmpty {
            return (resolvedPath, document)
        }
        let node = try navigateFragment(document, fragment: fragment)
        return (resolvedPath, node)
    }

    private static func resolveRelativeSchemaPath(baseDocumentPath: String, relativeFile: String) throws -> String {
        if relativeFile.hasPrefix("contracts/") {
            return try normalizeSchemaURI(relativeFile)
        }
        let baseNormalized = try normalizeSchemaURI(baseDocumentPath)
        let baseRelative = String(baseNormalized.dropFirst("contracts/".count))
        let baseFileURL = contractsRoot().appendingPathComponent(baseRelative)
        let rootURL = contractsRoot().resolvingSymlinksInPath()
        let resolvedURL = baseFileURL.deletingLastPathComponent().appendingPathComponent(relativeFile).resolvingSymlinksInPath()
        let rootPath = rootURL.path
        let resolvedPath = resolvedURL.path
        if resolvedPath != rootPath && !resolvedPath.hasPrefix(rootPath + "/") {
            throw SchemaValidationError("schema path outside bundle: \(relativeFile)")
        }
        let rel = resolvedURL.path.dropFirst(rootURL.path.count).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        return try normalizeSchemaURI("contracts/" + rel)
    }

    private static func validateTypes(_ instance: Any, typeValue: Any) throws {
        let types: [String]
        if let single = typeValue as? String {
            types = [single]
        } else if let many = typeValue as? [Any] {
            types = many.compactMap { $0 as? String }
        } else {
            throw SchemaValidationError("invalid type keyword")
        }
        if types.contains(where: { instanceMatchesType(instance, type: $0) }) {
            return
        }
        throw SchemaValidationError("instance type mismatch")
    }

    private static func instanceMatchesType(_ instance: Any, type: String) -> Bool {
        switch type {
        case "null":
            return instance is NSNull
        case "boolean":
            return isJSONBool(instance)
        case "integer":
            return isJSONInteger(instance)
        case "number":
            return isJSONNumber(instance)
        case "string":
            return instance is String || instance is NSString
        case "array":
            return instance is [Any] || instance is NSArray
        case "object":
            return instance is [String: Any] || instance is NSDictionary
        default:
            return false
        }
    }

    private static func isJSONBool(_ value: Any) -> Bool {
        if let number = value as? NSNumber {
            return CFGetTypeID(number) == CFBooleanGetTypeID()
        }
        return false
    }

    private static func isJSONNumber(_ value: Any) -> Bool {
        if isJSONBool(value) { return false }
        if value is Int || value is Double || value is Float { return true }
        if let number = value as? NSNumber {
            return CFGetTypeID(number) != CFBooleanGetTypeID()
        }
        return false
    }

    private static func isJSONInteger(_ value: Any) -> Bool {
        guard isJSONNumber(value) else { return false }
        let doubleValue = numericValue(value)
        return doubleValue.rounded() == doubleValue
    }

    private static func numericValue(_ value: Any) -> Double {
        if let intValue = value as? Int { return Double(intValue) }
        if let doubleValue = value as? Double { return doubleValue }
        if let number = value as? NSNumber { return number.doubleValue }
        return 0
    }

    private static func validateFormat(_ instance: Any, format: String) throws {
        guard let string = instance as? String else {
            throw SchemaValidationError("format \(format) requires string")
        }
        switch format {
        case "uuid":
            let pattern = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
            if string.range(of: pattern, options: .regularExpression) == nil {
                throw SchemaValidationError("invalid uuid format")
            }
        case "date-time":
            let formatter = ISO8601DateFormatter()
            formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            if formatter.date(from: string) == nil {
                formatter.formatOptions = [.withInternetDateTime]
                if formatter.date(from: string) == nil {
                    throw SchemaValidationError("invalid date-time format")
                }
            }
        case "uri":
            guard let url = URL(string: string), url.scheme != nil, url.host != nil else {
                throw SchemaValidationError("invalid uri format")
            }
        default:
            throw SchemaValidationError("unsupported format \(format)")
        }
    }

    private static func validateMinimum(_ instance: Any, minimum: NSNumber) throws {
        guard isJSONNumber(instance) else {
            throw SchemaValidationError("minimum requires number")
        }
        if numericValue(instance) < minimum.doubleValue {
            throw SchemaValidationError("value below minimum")
        }
    }

    private static func validateMaximum(_ instance: Any, maximum: NSNumber) throws {
        guard isJSONNumber(instance) else {
            throw SchemaValidationError("maximum requires number")
        }
        if numericValue(instance) > maximum.doubleValue {
            throw SchemaValidationError("value above maximum")
        }
    }

    private static func validateMinLength(_ instance: Any, minLength: Int) throws {
        guard let string = instance as? String else {
            throw SchemaValidationError("minLength requires string")
        }
        if string.count < minLength {
            throw SchemaValidationError("string shorter than minLength")
        }
    }

    private static func validateMaxLength(_ instance: Any, maxLength: Int) throws {
        guard let string = instance as? String else {
            throw SchemaValidationError("maxLength requires string")
        }
        if string.count > maxLength {
            throw SchemaValidationError("string longer than maxLength")
        }
    }

    private static func validatePattern(_ instance: Any, pattern: String) throws {
        guard let string = instance as? String else {
            throw SchemaValidationError("pattern requires string")
        }
        let regex = try NSRegularExpression(pattern: pattern)
        let range = NSRange(string.startIndex..<string.endIndex, in: string)
        if regex.firstMatch(in: string, range: range) == nil {
            throw SchemaValidationError("pattern mismatch")
        }
    }

    private static func validateMinItems(_ instance: Any, minItems: Int) throws {
        let count = arrayCount(instance)
        guard count >= 0 else {
            throw SchemaValidationError("minItems requires array")
        }
        if count < minItems {
            throw SchemaValidationError("array shorter than minItems")
        }
    }

    private static func validateMaxItems(_ instance: Any, maxItems: Int) throws {
        let count = arrayCount(instance)
        guard count >= 0 else {
            throw SchemaValidationError("maxItems requires array")
        }
        if count > maxItems {
            throw SchemaValidationError("array longer than maxItems")
        }
    }

    private static func validateMaxProperties(_ instance: Any, maxProperties: Int) throws {
        let count = objectKeyCount(instance)
        guard count >= 0 else {
            throw SchemaValidationError("maxProperties requires object")
        }
        if count > maxProperties {
            throw SchemaValidationError("object has too many properties")
        }
    }

    private static func validateObject(
        _ instance: Any,
        properties: [String: Any],
        schema: [String: Any],
        baseDocumentPath: String,
        nodeCounter: Counter,
        frames: Int
    ) throws {
        let object: [String: Any]
        if let dictionary = instance as? [String: Any] {
            object = dictionary
        } else if let dictionary = instance as? NSDictionary {
            var mapped: [String: Any] = [:]
            for (key, value) in dictionary {
                guard let name = key as? String else { continue }
                mapped[name] = value
            }
            object = mapped
        } else {
            throw SchemaValidationError("object expected")
        }
        if let maxProperties = schema["maxProperties"] as? Int {
            if object.count > maxProperties {
                throw SchemaValidationError("object has too many properties")
            }
        }
        if let required = schema["required"] as? [Any] {
            for item in required {
                guard let key = item as? String else { continue }
                if object[key] == nil {
                    throw SchemaValidationError("missing required property \(key)")
                }
            }
        }
        for (key, subschemaAny) in properties {
            guard let subschema = subschemaAny as? [String: Any] else { continue }
            if let value = object[key] {
                try validateInstance(value, schema: subschema, baseDocumentPath: baseDocumentPath, frames: frames, nodeCounter: nodeCounter)
            }
        }
        let declared = Set(properties.keys)
        for key in object.keys where !declared.contains(key) {
            if let additional = schema["additionalProperties"] as? Bool {
                if !additional {
                    throw SchemaValidationError("additional property not allowed: \(key)")
                }
            } else if let additional = schema["additionalProperties"] as? [String: Any] {
                try validateInstance(object[key]!, schema: additional, baseDocumentPath: baseDocumentPath, frames: frames, nodeCounter: nodeCounter)
            }
        }
    }

    private static func validateItems(
        _ instance: Any,
        items: Any,
        baseDocumentPath: String,
        nodeCounter: Counter,
        frames: Int
    ) throws {
        let array: [Any]
        if let values = instance as? [Any] {
            array = values
        } else if let values = instance as? NSArray {
            array = values.map { $0 }
        } else {
            throw SchemaValidationError("array expected")
        }
        if let subschema = items as? [String: Any] {
            for element in array {
                try validateInstance(element, schema: subschema, baseDocumentPath: baseDocumentPath, frames: frames, nodeCounter: nodeCounter)
            }
        }
    }

    private static func arrayCount(_ value: Any) -> Int {
        if let array = value as? [Any] { return array.count }
        if let array = value as? NSArray { return array.count }
        return -1
    }

    private static func objectKeyCount(_ value: Any) -> Int {
        if let object = value as? [String: Any] { return object.count }
        if let object = value as? NSDictionary { return object.count }
        return -1
    }

    private static func jsonEqual(_ lhs: Any, _ rhs: Any) -> Bool {
        if let left = lhs as? NSObject, let right = rhs as? NSObject {
            return left.isEqual(right)
        }
        return false
    }

    public static func validateDocument(_ instance: Any, schemaRef: String) throws {
        let counter = Counter()
        try walkInstanceBounds(instance, depth: 0, counter: counter)
        try validate(instance: instance, schemaRef: schemaRef)
    }
}

private final class SchemaValidatorBundleToken {}

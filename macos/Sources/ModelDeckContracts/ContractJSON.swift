import Foundation

public enum ContractJSON {
    public static func fixturesRoot() -> URL {
        #if SWIFT_PACKAGE
        return Bundle.module.resourceURL!
            .appendingPathComponent("fixtures", isDirectory: true)
        #else
        return Bundle(for: BundleToken.self)
            .resourceURL!
            .appendingPathComponent("fixtures", isDirectory: true)
        #endif
    }

    public static func loadFixture(named name: String, valid: Bool = true) throws -> Data {
        let folder = valid ? "valid" : "invalid"
        let url = fixturesRoot()
            .appendingPathComponent(folder, isDirectory: true)
            .appendingPathComponent(name)
        return try Data(contentsOf: url)
    }

    public static func loadManifest() throws -> [String: Any] {
        let url = fixturesRoot().appendingPathComponent("manifest.json")
        let data = try Data(contentsOf: url)
        let value = try JSONSerialization.jsonObject(with: data)
        guard let object = value as? [String: Any] else {
            throw SchemaValidationError("manifest root must be an object")
        }
        return object
    }

    public static func loadFixtureJSONObject(named name: String, valid: Bool = true) throws -> Any {
        let data = try loadFixture(named: name, valid: valid)
        return try JSONSerialization.jsonObject(with: data)
    }

    public static func validateFixture(named name: String, schemaRef: String, valid: Bool) throws {
        let instance = try loadFixtureJSONObject(named: name, valid: valid)
        if valid {
            try SchemaValidator.validate(instance: instance, schemaRef: schemaRef)
        } else {
            var rejected = false
            do {
                try SchemaValidator.validate(instance: instance, schemaRef: schemaRef)
            } catch let error as SchemaValidationError {
                rejected = true
                _ = error
            }
            if !rejected {
                throw SchemaValidationError("expected invalid fixture to be rejected: \(name)")
            }
        }
    }

    public static func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        let decoder = JSONDecoder()
        return try decoder.decode(type, from: data)
    }

    public static func encode<T: Encodable>(_ value: T) throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        return try encoder.encode(value)
    }

    public static func decodeValidated<T: Decodable>(
        _ type: T.Type,
        from data: Data,
        schemaRef: String
    ) throws -> T {
        let instance = try JSONSerialization.jsonObject(with: data)
        try SchemaValidator.validate(instance: instance, schemaRef: schemaRef)
        return try decode(type, from: data)
    }
}

private final class BundleToken {}

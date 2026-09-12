
import Foundation

public enum CanonicalJSON {
    public static func parseObject(_ data: Data) throws -> [String: Any] {
        let value = try JSONSerialization.jsonObject(with: data)
        guard let object = value as? [String: Any] else {
            throw NSError(domain: "ModelDeckContracts", code: 1)
        }
        return object
    }

    public static func canonicalData(from object: Any) throws -> Data {
        let normalized = sortValue(object)
        return try JSONSerialization.data(
            withJSONObject: normalized,
            options: [.sortedKeys, .fragmentsAllowed]
        )
    }

    public static func roundTripEqual(_ data: Data) throws -> Bool {
        let object = try JSONSerialization.jsonObject(with: data)
        let again = try canonicalData(from: object)
        let first = try canonicalData(from: object)
        return first == again
    }

    private static func sortValue(_ value: Any) -> Any {
        if let dict = value as? [String: Any] {
            var out: [String: Any] = [:]
            for key in dict.keys.sorted() {
                out[key] = sortValue(dict[key]!)
            }
            return out
        }
        if let array = value as? [Any] {
            return array.map { sortValue($0) }
        }
        return value
    }
}

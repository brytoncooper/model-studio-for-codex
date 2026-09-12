import Foundation

public struct SavedAccount: Codable, Sendable {
    public var id: String
    public var name: String
    public var baseURL: String? = nil
    public var wire: String? = nil
    public var hasKey: Bool? = nil
    public init(id: String, name: String, baseURL: String? = nil, wire: String? = nil, hasKey: Bool? = nil) {
        self.id = id
        self.name = name
        self.baseURL = baseURL
        self.wire = wire
        self.hasKey = hasKey
    }
    public static let openRouterURL = "https://openrouter.ai/api/v1"
    public static let cursorURL = "https://api.cursor.com"
    public var isCursor: Bool { wire == "cursor" }
    public var resolvedBaseURL: String { (baseURL?.isEmpty == false ? baseURL! : Self.openRouterURL).trimmingCharacters(in: CharacterSet(charactersIn: "/")) }
    public var isOpenRouter: Bool {
        guard let host = URL(string: resolvedBaseURL)?.host?.lowercased() else { return false }
        return host == "openrouter.ai" || host.hasSuffix(".openrouter.ai")
    }
    public var keyed: Bool { hasKey ?? true }
    public var resolvedWire: String { ["responses", "chat", "cursor"].contains(wire ?? "") ? wire! : "auto" }
}

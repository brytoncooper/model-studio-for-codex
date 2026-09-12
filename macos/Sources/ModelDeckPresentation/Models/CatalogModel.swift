import Foundation

public struct CatalogModel: Equatable, Sendable {
    public let id: String
    public let name: String
    public let suggested: Bool
    public init(id: String, name: String, suggested: Bool) {
        self.id = id
        self.name = name
        self.suggested = suggested
    }
    public static func matches(_ query: String, text: String) -> Bool {
        let haystack = text.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: .current)
        return query.split(whereSeparator: { $0.isWhitespace }).allSatisfy {
            haystack.contains(String($0).folding(options: [.caseInsensitive, .diacriticInsensitive], locale: .current))
        }
    }
}

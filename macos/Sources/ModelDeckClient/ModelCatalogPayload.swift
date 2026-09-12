import Foundation

public struct ModelCatalogPayload: Sendable, Equatable {
    public let models: [CatalogListItem]
    public let suggested: Bool
    public let statusMessage: String
    public let unavailable: Bool

    public init(
        models: [CatalogListItem],
        suggested: Bool,
        statusMessage: String,
        unavailable: Bool = false
    ) {
        self.models = models
        self.suggested = suggested
        self.statusMessage = statusMessage
        self.unavailable = unavailable
    }
}

public enum ModelCatalogServiceError: Error, Equatable, CustomStringConvertible {
    case cancelled
    case message(String)

    public var description: String {
        switch self {
        case .cancelled: return "catalog request cancelled"
        case .message(let value): return value
        }
    }
}

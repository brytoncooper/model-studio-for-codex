import Foundation

public struct ModelCatalogConnectionRequest: Sendable, Equatable {
    public let accountID: String
    public let accountName: String
    public let baseURL: String
    public let wire: String
    public let hasKey: Bool
    public let isCursor: Bool
    public let executablePath: String

    public init(
        accountID: String,
        accountName: String,
        baseURL: String,
        wire: String,
        hasKey: Bool,
        isCursor: Bool,
        executablePath: String
    ) {
        self.accountID = accountID
        self.accountName = accountName
        self.baseURL = baseURL
        self.wire = wire
        self.hasKey = hasKey
        self.isCursor = isCursor
        self.executablePath = executablePath
    }

    public var routeKey: String { accountID + "|" + baseURL + "|" + wire }
}

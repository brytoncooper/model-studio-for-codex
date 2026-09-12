import AppKit
import Foundation

public struct UsageProvider: Sendable {
    public let id: String
    public let name: String
    public let localKey: String
    public let accountID: String?
    public let saved: Bool
    public init(id: String, name: String, localKey: String, accountID: String?, saved: Bool) {
        self.id = id
        self.name = name
        self.localKey = localKey
        self.accountID = accountID
        self.saved = saved
    }
    public var color: NSColor { UsageValues.color(localKey) }
}

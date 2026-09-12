import Foundation

public struct SavedPreferences: Codable, Sendable {
    public var accounts: [SavedAccount] = []
    public var models: [String] = []
    public var selectedAccount: String = ""
    public var selectedModel: String = "openai/gpt-6-astra"
    public init() {}
}

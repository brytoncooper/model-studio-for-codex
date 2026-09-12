import Foundation

public struct UsageRecord: Sendable {
    public let providerKey: String
    public let providerName: String
    public let date: Date?
    public let model: String
    public let agent: String
    public let tokens: Double?
    public let cost: Double?
    public let status: Int?
    public var isSubscription: Bool { providerKey == "chatgpt" }
    public init(_ entry: [String: Any]) {
        let route = entry["route"] as? String ?? "unknown"
        let endpoint = (entry["endpoint"] as? String).flatMap { $0.isEmpty ? nil : $0 }
        switch route {
        case "openai": providerKey = "chatgpt"; providerName = "ChatGPT"
        case "openrouter": providerKey = "openrouter"; providerName = "OpenRouter"
        case "cursor": providerKey = "cursor"; providerName = "Cursor"
        case "endpoint": providerKey = "endpoint:" + (endpoint ?? "Unnamed endpoint"); providerName = endpoint ?? "Unnamed endpoint"
        default: providerKey = "route:" + route; providerName = endpoint ?? "Unknown provider"
        }
        date = UsageValues.date(entry["timestamp"])
        model = entry["model"] as? String ?? "Unknown model"
        let agentPath = entry["agent_name"] as? String ?? ""
        agent = agentPath == "/root" ? "Lead agent" : agentPath.split(separator: "/").last.map(String.init) ?? "Agent"
        let usage = entry["usage"] as? [String: Any] ?? [:]
        if let total = UsageValues.number(usage["total_tokens"]) {
            tokens = total
        } else if let input = UsageValues.number(usage["input_tokens"]), let output = UsageValues.number(usage["output_tokens"]), (input + output).isFinite {
            tokens = input + output
        } else { tokens = nil }
        cost = route == "openai" ? nil : UsageValues.number(route == "cursor" ? entry["cost"] : usage["cost"])
        if let code = UsageValues.number(entry["status"]), code < Double(Int.max), code.rounded() == code {
            status = Int(code)
        } else { status = nil }
    }
}

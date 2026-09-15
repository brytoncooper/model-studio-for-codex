import Foundation

/// Which kind of money a ``UsageRecord``'s ``UsageRecord/cost`` represents,
/// mirroring `contracts/engine.v1/vocabulary.schema.json#/definitions/cost_kind`.
/// Values other than the three the engine defines decode as `nil` (unknown)
/// rather than being force-cast, so an unrecognized upstream string never
/// silently becomes a wrong-but-plausible kind.
public enum UsageCostKind: String, Sendable {
    case estimated
    case providerSettled = "provider_settled"
    case subscriptionAllowance = "subscription_allowance"

    init?(rawEntry value: Any?) {
        guard let raw = value as? String, let kind = UsageCostKind(rawValue: raw) else { return nil }
        self = kind
    }
}

public struct UsageRecord: Sendable {
    public let providerKey: String
    public let providerName: String
    public let date: Date?
    public let model: String
    public let agent: String
    public let tokens: Double?
    /// `nil` whenever ``costKind`` is `nil` (neither recorded nor projectable)
    /// or ``costKind`` is `.subscriptionAllowance` (consumption against a
    /// prepaid allowance, not a per-request dollar figure). Never guessed from
    /// `route` or `providerKey`.
    public let cost: Double?
    public let status: Int?
    /// Exactly the `cost_kind` the producer wrote on this entry, or `nil` when
    /// it wrote none (or wrote a string this build does not recognize).
    /// Today's producer — the local router's ledger — writes none, so this is
    /// `nil` for every entry the app reads; see ``projectedCostKind(entry:reportedAmount:)``.
    public let recordedCostKind: UsageCostKind?
    /// Which kind of money ``cost`` is: ``recordedCostKind`` when the producer
    /// recorded one, otherwise the kind projected from the entry's own recorded
    /// evidence. `nil` means unknown, never "not a subscription".
    public let costKind: UsageCostKind?
    /// `nil` when ``costKind`` is `nil`: whether a record is subscription
    /// billing is unknown until either a recorded `cost_kind` or a recorded
    /// plan meter says so, never guessed from `providerKey`/`route`.
    public var isSubscription: Bool? { costKind.map { $0 == .subscriptionAllowance } }

    /// Ledger fields carrying a subscription-plan meter. The local router
    /// records the `x-codex-plan-type` and `x-codex-primary-used-percent`
    /// response headers under these keys (`local_router.py`, `Router.record`
    /// call site for the `openai` route); a header value arrives as a string,
    /// so both a string and a number count as present.
    private static let planMeterKeys = ["plan_type", "primary_used_percent"]

    /// A JSON `null` is the same as an absent key: the producer declared nothing.
    private static func declaredValue(_ value: Any?) -> Any? {
        guard let value, !(value is NSNull) else { return nil }
        return value
    }

    /// Label an entry that declares no `cost_kind`, from evidence the entry
    /// itself recorded, in the same order the engine's `project_cost_kind`
    /// uses (`python/src/model_deck/engine/usage/use_cases.py`):
    ///
    /// 1. a provider-reported amount on the record is provider-settled money;
    /// 2. a recorded plan meter is an allowance that claims the record;
    /// 3. otherwise the kind stays unknown.
    ///
    /// This fallback exists because nothing writes `cost_kind` into the ledger
    /// the app reads: the engine labels records on read, and the local router
    /// appends its entries verbatim. Delete it once a producer stamps the key.
    ///
    /// Nothing here reads `route` or ``providerKey``. The removed B16
    /// assumption called every ChatGPT-route record a subscription and nilled
    /// its cost by route name; here a ChatGPT-route entry with no recorded plan
    /// meter stays unknown, and a plan meter on any route is honored.
    static func projectedCostKind(entry: [String: Any], reportedAmount: Double?) -> UsageCostKind? {
        if reportedAmount != nil { return .providerSettled }
        if recordsPlanMeter(entry) { return .subscriptionAllowance }
        return nil
    }

    private static func recordsPlanMeter(_ entry: [String: Any]) -> Bool {
        planMeterKeys.contains { key in
            guard let value = declaredValue(entry[key]) else { return false }
            if let text = value as? String { return !text.isEmpty }
            return UsageValues.number(value) != nil
        }
    }

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
        let declaredKind = UsageRecord.declaredValue(entry["cost_kind"])
        let recorded = UsageCostKind(rawEntry: declaredKind)
        recordedCostKind = recorded
        let rawCost = UsageValues.number(route == "cursor" ? entry["cost"] : usage["cost"])
        // Project a kind only for an entry that declares none. A declared value
        // this build does not recognize is a producer it does not understand,
        // so that record stays unknown instead of being second-guessed.
        let kind = declaredKind == nil
            ? UsageRecord.projectedCostKind(entry: entry, reportedAmount: rawCost)
            : recorded
        costKind = kind
        cost = (kind == nil || kind == .subscriptionAllowance) ? nil : rawCost
        if let code = UsageValues.number(entry["status"]), code < Double(Int.max), code.rounded() == code {
            status = Int(code)
        } else { status = nil }
    }
}

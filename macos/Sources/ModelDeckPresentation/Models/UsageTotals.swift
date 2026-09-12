import Foundation

public struct UsageTotals: Sendable {
    public let count: Int
    public let reportedCost: Double?
    public let unknownCosts: Int
    public let subscriptionCount: Int
    public let reportedTokens: Double?
    public let tokenCoverage: Int
    public let failedCount: Int
    public init(_ records: [UsageRecord]) {
        count = records.count
        let costs = records.compactMap(\.cost)
        let costSum = costs.reduce(0, +)
        reportedCost = costs.isEmpty || !costSum.isFinite ? nil : costSum
        unknownCosts = records.filter { !$0.isSubscription && $0.cost == nil }.count
        subscriptionCount = records.filter(\.isSubscription).count
        let tokens = records.compactMap(\.tokens)
        let tokenSum = tokens.reduce(0, +)
        reportedTokens = tokens.isEmpty || !tokenSum.isFinite ? nil : tokenSum
        tokenCoverage = tokens.count
        failedCount = records.filter { ($0.status ?? 0) >= 400 }.count
    }
}

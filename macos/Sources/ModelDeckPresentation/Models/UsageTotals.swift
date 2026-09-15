import Foundation

public struct UsageTotals: Sendable {
    public let count: Int
    /// The sum of the records that report money — never a figure invented for a
    /// record that reports none.
    public let reportedCost: Double?
    /// Records whose cost kind could not be determined: no `cost_kind` was
    /// recorded, no amount was reported, and no plan meter claimed them.
    public let unknownCosts: Int
    /// Records a recorded `cost_kind` or a recorded plan meter identifies as
    /// allowance consumption. A record whose kind is unknown is never counted
    /// here, and never counted as "not a subscription" either.
    public let subscriptionCount: Int
    public let reportedTokens: Double?
    public let tokenCoverage: Int
    public let failedCount: Int
    public init(_ records: [UsageRecord]) {
        count = records.count
        let costs = records.compactMap(\.cost)
        let costSum = costs.reduce(0, +)
        reportedCost = costs.isEmpty || !costSum.isFinite ? nil : costSum
        // A subscription-allowance record's nil cost is expected, not unknown;
        // only count records that are not a *known* subscription record.
        unknownCosts = records.filter { $0.isSubscription != true && $0.cost == nil }.count
        subscriptionCount = records.filter { $0.isSubscription == true }.count
        let tokens = records.compactMap(\.tokens)
        let tokenSum = tokens.reduce(0, +)
        reportedTokens = tokens.isEmpty || !tokenSum.isFinite ? nil : tokenSum
        tokenCoverage = tokens.count
        failedCount = records.filter { ($0.status ?? 0) >= 400 }.count
    }
}

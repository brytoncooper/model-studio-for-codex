import Foundation

public enum UsageModelSelfTest {
    public static func run() -> Bool {
        guard UsageValues.number(true) == nil, UsageValues.number(-1) == nil,
              UsageValues.number(Double.infinity) == nil, UsageValues.number(Double.nan) == nil,
              UsageValues.number("12") == nil, UsageValues.number(0) == 0,
              UsageValues.dollars(nil) == "Not reported", UsageValues.dollars(0) == "$0.00" else { return false }
        let cursor = UsageRecord(["route": "cursor", "timestamp": "2026-09-11T12:00:00.123-0600", "model": "Test Model", "agent_name": "/root/reviewer", "cost": 0.25, "usage": ["cost": 9, "input_tokens": 10, "output_tokens": 5]])
        let unknown = UsageRecord(["route": "cursor", "usage": ["cost": 10, "total_tokens": true]])
        let openai = UsageRecord(["route": "openai", "timestamp": "2026-09-11T12:00:00-0600", "usage": ["cost": 100]])
        let endpoint = UsageRecord(["route": "endpoint", "endpoint": "Lab", "usage": ["cost": 0, "total_tokens": 30]])
        let router = UsageRecord(["route": "openrouter", "endpoint": "Lab", "usage": ["cost": Double.nan]])
        let totals = UsageTotals([cursor, unknown, openai, endpoint, router])
        let boundaryStatus = UsageRecord(["status": Double(Int.max)])
        let allowance = UsageFiltering.overviewAllowance([["pool_id": "codex", "pool_name": "Codex", "used_percent": 20], ["pool_id": "spark", "pool_name": "Spark", "used_percent": 100]])
        guard cursor.cost == 0.25, cursor.tokens == 15, cursor.date != nil,
              boundaryStatus.status == nil, allowance.remaining == 80,
              unknown.cost == nil, unknown.tokens == nil, openai.cost == nil,
              endpoint.providerKey == "endpoint:Lab", router.providerKey == "openrouter",
              totals.count == 5, totals.reportedCost == 0.25, totals.unknownCosts == 2,
              totals.subscriptionCount == 1, totals.reportedTokens == 45, totals.tokenCoverage == 2,
              UsageTotals([unknown]).reportedCost == nil, UsageTotals([openai]).reportedTokens == nil,
              UsageFiltering.matchesSearch(cursor, query: "test REVIEWER"), !UsageFiltering.matchesSearch(cursor, query: "other") else { return false }
        guard let now = UsageValues.date("2026-09-11T20:00:00Z") else { return false }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        let yesterday = UsageRecord(["timestamp": "2026-09-10T23:59:59Z", "route": "cursor"])
        let future = UsageRecord(["timestamp": "2026-09-12T01:00:00Z", "route": "cursor"])
        let filtered = UsageFiltering.filteredRecords([cursor, openai, yesterday, future, unknown], days: 1, now: now, calendar: calendar)
        guard filtered.count == 2, UsageFiltering.filteredRecords([yesterday], days: 7, now: now, calendar: calendar).count == 1 else { return false }
        return true
    }
}

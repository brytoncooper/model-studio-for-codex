import Foundation

public enum UsageModelSelfTest {
    public static func run() -> Bool {
        guard UsageValues.number(true) == nil, UsageValues.number(-1) == nil,
              UsageValues.number(Double.infinity) == nil, UsageValues.number(Double.nan) == nil,
              UsageValues.number("12") == nil, UsageValues.number(0) == 0,
              UsageValues.dollars(nil) == "Not reported", UsageValues.dollars(0) == "$0.00" else { return false }
        // A recorded cost_kind drives cost/isSubscription, and an entry that
        // records none falls back to its own evidence (a reported amount, a
        // plan meter). Neither ever comes from route or providerKey — that
        // guess is the B16/C8e injected assumption this self-test exercises the
        // removal of, and the legacy block below covers the entries the local
        // router actually writes, which carry no cost_kind at all.
        let cursor = UsageRecord(["route": "cursor", "timestamp": "2026-09-11T12:00:00.123-0600", "model": "Test Model", "agent_name": "/root/reviewer", "cost": 0.25, "cost_kind": "provider_settled", "usage": ["cost": 9, "input_tokens": 10, "output_tokens": 5]])
        let unknown = UsageRecord(["route": "cursor", "usage": ["cost": 10, "total_tokens": true]])
        let openai = UsageRecord(["route": "openai", "timestamp": "2026-09-11T12:00:00-0600", "cost_kind": "subscription_allowance", "usage": ["cost": 100]])
        let endpoint = UsageRecord(["route": "endpoint", "endpoint": "Lab", "cost_kind": "estimated", "usage": ["cost": 0, "total_tokens": 30]])
        let router = UsageRecord(["route": "openrouter", "endpoint": "Lab", "usage": ["cost": Double.nan]])
        let unrecognizedKind = UsageRecord(["route": "cursor", "cost": 5, "cost_kind": "made_up_kind"])
        let totals = UsageTotals([cursor, unknown, openai, endpoint, router])
        let boundaryStatus = UsageRecord(["status": Double(Int.max)])
        let allowance = UsageFiltering.overviewAllowance([["pool_id": "codex", "pool_name": "Codex", "used_percent": 20], ["pool_id": "spark", "pool_name": "Spark", "used_percent": 100]])
        guard cursor.cost == 0.25, cursor.tokens == 15, cursor.date != nil, cursor.isSubscription == false,
              boundaryStatus.status == nil, allowance.remaining == 80,
              unknown.cost == nil, unknown.tokens == nil, unknown.isSubscription == nil,
              openai.cost == nil, openai.isSubscription == true,
              endpoint.cost == 0, endpoint.isSubscription == false, endpoint.providerKey == "endpoint:Lab",
              router.providerKey == "openrouter", router.cost == nil, router.isSubscription == nil,
              unrecognizedKind.costKind == nil, unrecognizedKind.cost == nil, unrecognizedKind.isSubscription == nil,
              totals.count == 5, totals.reportedCost == 0.25, totals.unknownCosts == 2,
              totals.subscriptionCount == 1, totals.reportedTokens == 45, totals.tokenCoverage == 2,
              UsageTotals([unknown]).reportedCost == nil, UsageTotals([openai]).reportedTokens == nil,
              UsageFiltering.matchesSearch(cursor, query: "test REVIEWER"), !UsageFiltering.matchesSearch(cursor, query: "other") else { return false }
        // Entries exactly as local_router's Router.record writes them: no
        // cost_kind key anywhere.
        let ledgerEndpoint = UsageRecord(["timestamp": "2026-09-11T12:00:00-0600", "route": "openrouter", "model": "some/model", "status": 200, "outcome": "completed", "wire": "responses", "usage": ["input_tokens": 1200, "output_tokens": 300, "cost": 0.5], "generation_id": "gen-abc"])
        let ledgerPlan = UsageRecord(["timestamp": "2026-09-11T12:00:01-0600", "route": "openai", "model": "gpt-5.2-codex", "status": 200, "bytes": 8192, "primary_used_percent": "12.5", "plan_type": "pro"])
        let ledgerFailure = UsageRecord(["timestamp": "2026-09-11T12:00:02-0600", "route": "openai", "model": "gpt-5.2-codex", "status": 429])
        let ledgerTotals = UsageTotals([ledgerEndpoint, ledgerPlan, ledgerFailure])
        guard ledgerEndpoint.recordedCostKind == nil, ledgerEndpoint.cost == 0.5, ledgerEndpoint.isSubscription == false,
              ledgerPlan.cost == nil, ledgerPlan.isSubscription == true,
              ledgerFailure.costKind == nil, ledgerFailure.isSubscription == nil,
              ledgerTotals.reportedCost == 0.5, ledgerTotals.subscriptionCount == 1, ledgerTotals.unknownCosts == 1 else { return false }
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

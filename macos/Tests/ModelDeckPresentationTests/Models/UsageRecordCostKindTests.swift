import XCTest
@testable import ModelDeckPresentation

/// B16/C8e: ``UsageRecord``/``UsageTotals`` must derive `cost` and
/// `isSubscription` from evidence the entry recorded, never guess them from
/// `route`/`providerKey`. See `UsageRecord.swift` for the removed assumption
/// (`isSubscription == (providerKey == "chatgpt")`, `cost == nil` when
/// `route == "openai"`).
///
/// The ledger the app actually reads writes no `cost_kind` (the local router
/// appends its entries verbatim; the engine labels records on read), so the
/// entries below are the shapes `Router.record` writes, unmodified, and they
/// exercise the documented fallback in ``UsageRecord/projectedCostKind(entry:reportedAmount:)``.
final class UsageRecordCostKindTests: XCTestCase {
    // MARK: Recorded cost_kind wins

    func testProviderSettledExposesCostAndIsNotSubscription() {
        let record = UsageRecord(["route": "cursor", "cost": 1.5, "cost_kind": "provider_settled"])
        XCTAssertEqual(record.recordedCostKind, .providerSettled)
        XCTAssertEqual(record.costKind, .providerSettled)
        XCTAssertEqual(record.cost, 1.5)
        XCTAssertEqual(record.isSubscription, false)
    }

    func testEstimatedExposesCostAndIsNotSubscription() {
        let record = UsageRecord(["route": "endpoint", "endpoint": "Lab", "cost_kind": "estimated", "usage": ["cost": 0.02]])
        XCTAssertEqual(record.costKind, .estimated)
        XCTAssertEqual(record.cost, 0.02)
        XCTAssertEqual(record.isSubscription, false)
    }

    func testSubscriptionAllowanceIsSubscriptionWithNilCost() {
        // A subscription-allowance record's nil cost is the expected shape
        // (it is consumption against an allowance, not a per-request charge),
        // not an "unknown cost" the way an undeterminable kind is.
        let record = UsageRecord(["route": "openai", "cost_kind": "subscription_allowance", "usage": ["cost": 100]])
        XCTAssertEqual(record.costKind, .subscriptionAllowance)
        XCTAssertNil(record.cost)
        XCTAssertEqual(record.isSubscription, true)
    }

    func testRecordedSubscriptionAllowanceOutranksAReportedAmount() {
        // The fallback must never override a kind the producer recorded, even
        // when the entry also carries an amount the fallback would settle on.
        let record = UsageRecord(["route": "cursor", "cost": 3, "cost_kind": "subscription_allowance"])
        XCTAssertEqual(record.costKind, .subscriptionAllowance)
        XCTAssertNil(record.cost)
        XCTAssertEqual(record.isSubscription, true)
    }

    func testUnrecognizedCostKindStringIsUnknownNotCrashed() {
        // A declared kind this build does not understand is a producer it does
        // not understand: no fallback, no guess, even though a cost is present.
        let record = UsageRecord(["route": "cursor", "cost": 5, "cost_kind": "not_a_real_kind"])
        XCTAssertNil(record.recordedCostKind)
        XCTAssertNil(record.costKind)
        XCTAssertNil(record.cost)
        XCTAssertNil(record.isSubscription)
    }

    // MARK: Real ledger entries, which declare no cost_kind

    /// The entry `Router.record` writes for a registered endpoint / OpenRouter
    /// turn: provider usage including the provider's own cost, no `cost_kind`.
    private var openRouterLedgerEntry: [String: Any] {
        ["timestamp": "2026-09-11T12:00:00-0600", "route": "openrouter", "model": "some/model",
         "status": 200, "outcome": "completed", "wire": "responses",
         "usage": ["input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500, "cost": 0.0123],
         "generation_id": "gen-abc", "thread_id": "t-1", "agent_name": "/root", "turn_id": "turn-1"]
    }

    /// The entry `Router.record` writes for a ChatGPT-route turn: no cost at
    /// all, and the Codex plan meter headers.
    private var chatGPTPlanLedgerEntry: [String: Any] {
        ["timestamp": "2026-09-11T12:00:01-0600", "route": "openai", "model": "gpt-5.2-codex",
         "status": 200, "bytes": 8_192, "primary_used_percent": "12.5", "plan_type": "pro",
         "thread_id": "t-2", "agent_name": "/root/reviewer", "turn_id": "turn-2"]
    }

    func testLedgerEntryWithProviderReportedCostStillReportsThatCost() {
        let record = UsageRecord(openRouterLedgerEntry)
        XCTAssertNil(record.recordedCostKind, "the ledger writes no cost_kind")
        XCTAssertEqual(record.costKind, .providerSettled)
        XCTAssertEqual(record.cost, 0.0123)
        XCTAssertEqual(record.isSubscription, false)
    }

    func testLedgerEntryWithPlanMeterIsSubscriptionWithoutGuessingFromRoute() {
        let record = UsageRecord(chatGPTPlanLedgerEntry)
        XCTAssertEqual(record.costKind, .subscriptionAllowance)
        XCTAssertNil(record.cost)
        XCTAssertEqual(record.isSubscription, true)
    }

    func testChatGPTRouteWithoutAPlanMeterStaysUnknown() {
        // The removed assumption keyed on the route name; the fallback keys on
        // the recorded meter, so the same route with no meter is unknown.
        let record = UsageRecord(["timestamp": "2026-09-11T12:00:02-0600", "route": "openai",
                                  "model": "gpt-5.2-codex", "status": 429])
        XCTAssertEqual(record.providerKey, "chatgpt")
        XCTAssertNil(record.costKind)
        XCTAssertNil(record.cost)
        XCTAssertNil(record.isSubscription)
    }

    func testPlanMeterOnANonChatGPTRouteIsAlsoHonored() {
        let record = UsageRecord(["route": "endpoint", "endpoint": "Lab", "plan_type": "team"])
        XCTAssertEqual(record.costKind, .subscriptionAllowance)
        XCTAssertEqual(record.isSubscription, true)
    }

    func testCursorLedgerEntryReportsItsTopLevelCost() {
        let record = UsageRecord(["timestamp": "2026-09-11T12:00:03-0600", "route": "cursor",
                                  "model": "composer-1", "wire": "cursor", "status": 200,
                                  "endpoint": "Cursor", "usage": ["total_tokens": 42],
                                  "generation_id": "run-1", "billing": "Cursor subscription",
                                  "cost_source": "cursor_sdk", "cost": 0.42])
        XCTAssertEqual(record.costKind, .providerSettled)
        XCTAssertEqual(record.cost, 0.42)
    }

    func testEntryWithNoAmountAndNoMeterStaysUnknown() {
        // A failed turn records neither money nor a meter: unknown, not zero.
        let record = UsageRecord(["route": "openrouter", "model": "some/model", "status": 502,
                                  "outcome": "failed", "wire": "responses"])
        XCTAssertNil(record.costKind)
        XCTAssertNil(record.cost)
        XCTAssertNil(record.isSubscription)
    }

    func testNullValuesCountAsAbsentNotAsEvidence() {
        // JSON nulls reach the record as NSNull: the router writes null turn
        // metadata, and a producer may write a null cost_kind or plan_type.
        let record = UsageRecord(["route": "openai", "model": "gpt-5.2-codex", "status": 200,
                                  "cost_kind": NSNull(), "plan_type": NSNull(),
                                  "primary_used_percent": NSNull(), "agent_name": NSNull()])
        XCTAssertNil(record.recordedCostKind)
        XCTAssertNil(record.costKind)
        XCTAssertNil(record.isSubscription)
    }

    func testNegativeAndNonFiniteAmountsAreNotEvidenceOfSettledMoney() {
        let negative = UsageRecord(["route": "openrouter", "usage": ["cost": -1]])
        let notANumber = UsageRecord(["route": "openrouter", "usage": ["cost": Double.nan]])
        XCTAssertNil(negative.costKind)
        XCTAssertNil(negative.cost)
        XCTAssertNil(notANumber.costKind)
        XCTAssertNil(notANumber.cost)
    }

    // MARK: Totals

    func testTotalsUnknownCostsExcludesKnownSubscriptionRecords() {
        let settled = UsageRecord(["route": "cursor", "cost": 1, "cost_kind": "provider_settled"])
        let allowance = UsageRecord(["route": "openai", "cost_kind": "subscription_allowance"])
        let noEvidence = UsageRecord(["route": "cursor", "status": 502])
        let totals = UsageTotals([settled, allowance, noEvidence])
        XCTAssertEqual(totals.subscriptionCount, 1)
        // allowance's nil cost is expected (excluded); noEvidence's nil cost is
        // genuinely unknown (included).
        XCTAssertEqual(totals.unknownCosts, 1)
        XCTAssertEqual(totals.reportedCost, 1)
    }

    /// The regression this repair round exists for: with no producer stamping
    /// `cost_kind`, the dashboard reported no money and no subscriptions at all.
    func testTotalsOverRealLedgerEntriesStillReportMoneyAndSubscriptions() {
        let totals = UsageTotals([UsageRecord(openRouterLedgerEntry), UsageRecord(chatGPTPlanLedgerEntry)])
        XCTAssertEqual(totals.count, 2)
        XCTAssertEqual(totals.reportedCost, 0.0123)
        XCTAssertEqual(totals.subscriptionCount, 1)
        XCTAssertEqual(totals.unknownCosts, 0)
    }
}

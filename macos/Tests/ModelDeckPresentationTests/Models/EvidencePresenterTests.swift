import XCTest
@testable import ModelDeckClient
@testable import ModelDeckPresentation

/// B16/C8e: the price snapshot row's five-state presentation logic
/// (``EvidencePhase``: loading/ready/empty/failure/unavailable), same
/// convention ``ModelCatalogPhase`` uses elsewhere in V2.
final class EvidencePresenterTests: XCTestCase {
    private func provenance(sourceID: String = "com.modeldeck.openrouter", fetchedAt: String, stale: Bool) -> EngineSourceProvenance {
        EngineSourceProvenance(sourceID: sourceID, sourceURL: nil, citation: nil, asOf: nil, fetchedAt: fetchedAt, stale: stale, lastRefreshError: nil)
    }

    private func priceRecord(fetchedAt: String, stale: Bool) -> EnginePriceRecord {
        EnginePriceRecord(
            providerModelID: "deepseek/deepseek-v4.1-flash",
            registrationID: nil,
            currency: "USD",
            unitPrices: EngineUnitPrices(inputTokens: 0.00000015, outputTokens: 0.0000006, cachedTokens: nil),
            provenance: provenance(fetchedAt: fetchedAt, stale: stale)
        )
    }

    func testReadyWhenRecordsPresentAndNotStale() {
        let now = Date(timeIntervalSince1970: 1_000_000)
        let fetchedAt = ISO8601DateFormatter().string(from: now.addingTimeInterval(-3600))
        let result = EnginePricesQueryResult(
            records: [priceRecord(fetchedAt: fetchedAt, stale: false)],
            snapshot: provenance(fetchedAt: fetchedAt, stale: false),
            cached: true
        )
        let summary = EvidencePresenter.priceSnapshotSummary(from: result, now: now)
        XCTAssertEqual(summary.phase, .ready)
        XCTAssertFalse(summary.stale)
        XCTAssertTrue(summary.statusMessage.contains("last refreshed"), summary.statusMessage)
    }

    func testEmptyWhenNoRecords() {
        let now = Date(timeIntervalSince1970: 1_000_000)
        let fetchedAt = ISO8601DateFormatter().string(from: now)
        let result = EnginePricesQueryResult(records: [], snapshot: provenance(fetchedAt: fetchedAt, stale: false), cached: true)
        let summary = EvidencePresenter.priceSnapshotSummary(from: result, now: now)
        XCTAssertEqual(summary.phase, .empty)
    }

    func testStaleSnapshotIsFlaggedEvenWhenReady() {
        let now = Date(timeIntervalSince1970: 1_000_000)
        let fetchedAt = ISO8601DateFormatter().string(from: now.addingTimeInterval(-90_000))
        let result = EnginePricesQueryResult(
            records: [priceRecord(fetchedAt: fetchedAt, stale: true)],
            snapshot: provenance(fetchedAt: fetchedAt, stale: true),
            cached: true
        )
        let summary = EvidencePresenter.priceSnapshotSummary(from: result, now: now)
        XCTAssertEqual(summary.phase, .ready)
        XCTAssertTrue(summary.stale)
        XCTAssertTrue(summary.statusMessage.contains("stale"), summary.statusMessage)
    }

    func testUnavailableSummaryFlagsCapabilityMissingSeparatelyFromFailure() {
        let unavailable = EvidencePresenter.evidenceFailureSummary(message: "method not implemented: engine.v1.prices.query", capabilityMissing: true)
        XCTAssertEqual(unavailable.phase, .unavailable("method not implemented: engine.v1.prices.query"))
        XCTAssertFalse(unavailable.stale)

        let failure = EvidencePresenter.evidenceFailureSummary(message: "cache locked", capabilityMissing: false)
        XCTAssertEqual(failure.phase, .failure("cache locked"))
    }

    /// A refresh that never started is reported as a refresh failure, not as
    /// a read failure: the cached snapshot behind the row is untouched.
    func testRefreshFailureSummaryNamesTheRefreshRatherThanTheRead() {
        let failure = EvidencePresenter.refreshFailureSummary(message: "price source unreachable", capabilityMissing: false)
        XCTAssertEqual(failure.phase, .failure("price source unreachable"))
        XCTAssertFalse(failure.stale)
        XCTAssertTrue(failure.statusMessage.hasPrefix("Price refresh failed: "), failure.statusMessage)

        let unavailable = EvidencePresenter.refreshFailureSummary(message: "method not implemented: engine.v1.prices.refresh", capabilityMissing: true)
        XCTAssertEqual(unavailable.phase, .unavailable("method not implemented: engine.v1.prices.refresh"))
        XCTAssertTrue(unavailable.statusMessage.hasPrefix("Price refresh unavailable: "), unavailable.statusMessage)
    }

    func testRefreshStartedMessageIncludesJobID() {
        XCTAssertEqual(EvidencePresenter.refreshStartedMessage(jobID: "job-42"), "Refresh started (job job-42).")
    }
}

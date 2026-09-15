import Foundation
import ModelDeckClient

/// Presentation phase for one evidence row (price snapshot, benchmark
/// snapshot, refresh job). Same five outward states as
/// ``ModelCatalogPhase`` (`loading`/`ready`/`empty`/`failure`/`unavailable`)
/// so V2's evidence rows read the same way the model catalog does; `idle`
/// is omitted here because these rows load themselves on attach rather than
/// waiting for a user-driven connection request.
public enum EvidencePhase: Equatable, Sendable {
    case loading
    case ready
    case empty
    case failure(String)
    case unavailable(String)
}

/// Pure-presentation summary of a price cache snapshot: how the row should
/// read, independent of any AppKit view.
public struct PriceSnapshotSummary: Equatable, Sendable {
    public let phase: EvidencePhase
    public let stale: Bool
    public let statusMessage: String

    public init(phase: EvidencePhase, stale: Bool, statusMessage: String) {
        self.phase = phase
        self.stale = stale
        self.statusMessage = statusMessage
    }
}

public enum EvidencePresenter {
    /// Builds the price snapshot row's summary from an
    /// `engine.v1.prices.query` result. `ready` when at least one record
    /// came back, `empty` when the cache is reachable but has nothing for
    /// the current scope yet.
    public static func priceSnapshotSummary(
        from result: EnginePricesQueryResult,
        now: Date = Date()
    ) -> PriceSnapshotSummary {
        let age = relativeAge(fetchedAt: result.snapshot.fetchedAt, now: now)
        let phase: EvidencePhase = result.records.isEmpty ? .empty : .ready
        let ageText = age ?? "an unknown time ago"
        let message = result.snapshot.stale
            ? "Prices stale — last refreshed \(ageText)."
            : "Prices last refreshed \(ageText)."
        return PriceSnapshotSummary(phase: phase, stale: result.snapshot.stale, statusMessage: message)
    }

    /// Summary for an engine error reading prices/benchmarks. `unavailable`
    /// means the capability itself is not there yet (the engine domain error
    /// this client maps to `EngineClientError.unavailable`); any other
    /// failure is a genuine read error.
    public static func evidenceFailureSummary(message: String, capabilityMissing: Bool) -> PriceSnapshotSummary {
        let phase: EvidencePhase = capabilityMissing ? .unavailable(message) : .failure(message)
        let prefix = capabilityMissing ? "Prices unavailable: " : "Price read failed: "
        return PriceSnapshotSummary(phase: phase, stale: false, statusMessage: prefix + message)
    }

    /// Summary for a price refresh that could not even be started. Separate
    /// from ``evidenceFailureSummary(message:capabilityMissing:)`` so the row
    /// says which call failed: a refresh that never started leaves the cached
    /// snapshot exactly as it was, while a failed read means the row has no
    /// snapshot to show at all.
    public static func refreshFailureSummary(message: String, capabilityMissing: Bool) -> PriceSnapshotSummary {
        let phase: EvidencePhase = capabilityMissing ? .unavailable(message) : .failure(message)
        let prefix = capabilityMissing ? "Price refresh unavailable: " : "Price refresh failed: "
        return PriceSnapshotSummary(phase: phase, stale: false, statusMessage: prefix + message)
    }

    /// One line describing a started refresh job, for the row's status text.
    public static func refreshStartedMessage(jobID: String) -> String {
        "Refresh started (job \(jobID))."
    }

    private static func relativeAge(fetchedAt: String, now: Date) -> String? {
        guard let date = ISO8601DateFormatter().date(from: fetchedAt) else { return nil }
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .abbreviated
        return formatter.localizedString(for: date, relativeTo: now)
    }
}

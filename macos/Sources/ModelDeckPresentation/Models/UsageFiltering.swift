import Foundation

public enum UsageFiltering {
    public static func filteredRecords(_ records: [UsageRecord], days: Int, now: Date, calendar: Calendar = .current) -> [UsageRecord] {
        let start = calendar.date(byAdding: .day, value: -(days - 1), to: calendar.startOfDay(for: now)) ?? now
        return records.filter { record in record.date.map { $0 >= start && $0 <= now } ?? false }
    }
    public static func overviewAllowance(_ windows: [[String: Any]]) -> (remaining: Double?, name: String) {
        let first = windows.first { ($0["pool_id"] as? String)?.lowercased() == "codex" } ?? windows.first
        guard let first else { return (nil, "Allowance unavailable") }
        let poolID = first["pool_id"] as? String ?? ""
        let pool = windows.filter { ($0["pool_id"] as? String ?? "") == poolID }
        let window = pool.filter { UsageValues.number($0["used_percent"]) != nil }.max {
            (UsageValues.number($0["used_percent"]) ?? 0) < (UsageValues.number($1["used_percent"]) ?? 0)
        } ?? first
        let name = window["pool_name"] as? String ?? "Subscription"
        let kind = window["window_kind"] as? String ?? "allowance"
        return (UsageValues.number(window["used_percent"]).map { max(0, 100 - $0) }, name + " · " + kind)
    }
    public static func matchesSearch(_ record: UsageRecord, query: String) -> Bool {
        let haystack = (record.model + " " + record.agent + " " + record.providerName).folding(options: [.caseInsensitive, .diacriticInsensitive], locale: .current)
        return query.split(whereSeparator: { $0.isWhitespace }).allSatisfy { haystack.contains(String($0).folding(options: [.caseInsensitive, .diacriticInsensitive], locale: .current)) }
    }
}

import AppKit
import CoreFoundation
import Foundation

public enum UsageValues {
    private static let fractionalDateParser: ISO8601DateFormatter = {
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return parser
    }()
    private static let dateParser = ISO8601DateFormatter()
    public static func number(_ value: Any?) -> Double? {
        guard let value = value as? NSNumber,
              CFGetTypeID(value) != CFBooleanGetTypeID() else { return nil }
        let amount = value.doubleValue
        return amount.isFinite && amount >= 0 ? amount : nil
    }
    public static func count(_ value: Double) -> String {
        if value >= 1_000_000 { return String(format: "%.1fM", value / 1_000_000) }
        if value >= 10_000 { return String(format: "%.1fK", value / 1_000) }
        return NumberFormatter.localizedString(from: NSNumber(value: value), number: .decimal)
    }
    public static func dollars(_ value: Double?) -> String {
        guard let value else { return "Not reported" }
        return String(format: value > 0 && value < 0.01 ? "$%.4f" : "$%.2f", value)
    }
    public static func date(_ value: Any?) -> Date? {
        guard let text = value as? String else { return nil }
        return fractionalDateParser.date(from: text) ?? dateParser.date(from: text)
    }
    public static func reset(_ value: Any?) -> String {
        guard let seconds = number(value) else { return "Reset time unavailable" }
        let formatter = DateFormatter()
        formatter.dateStyle = .short
        formatter.timeStyle = .short
        return "Resets " + formatter.string(from: Date(timeIntervalSince1970: seconds))
    }
    public static func color(_ key: String) -> NSColor {
        switch key {
        case "chatgpt": return .systemTeal
        case "openrouter": return .systemIndigo
        case "cursor": return .systemBlue
        default: return .systemOrange
        }
    }
}

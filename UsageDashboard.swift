import AppKit
import CoreFoundation

import ModelDeckPresentation

private enum UsageViewStyle {
    static func label(_ text: String, size: CGFloat = 12, weight: NSFont.Weight = .regular, color: NSColor = .secondaryLabelColor) -> NSTextField {
        let label = NSTextField(wrappingLabelWithString: text)
        label.font = .systemFont(ofSize: size, weight: weight)
        label.textColor = color
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        label.setContentHuggingPriority(.defaultHigh, for: .vertical)
        return label
    }

    static func column(_ views: [NSView], spacing: CGFloat = 10) -> NSStackView {
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = spacing
        for view in views {
            stack.addArrangedSubview(view)
            view.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        }
        return stack
    }

    static func row(_ views: [NSView], spacing: CGFloat = 10) -> NSStackView {
        let stack = NSStackView(views: views)
        stack.orientation = .horizontal
        stack.alignment = .centerY
        stack.spacing = spacing
        return stack
    }

    static func rule() -> NSView {
        let rule = NSBox()
        rule.boxType = .separator
        return rule
    }

    static func card(_ views: [NSView], accent: NSColor? = nil, padding: CGFloat = 16, spacing: CGFloat = 10) -> NSView {
        let card = UsageSurfaceView()
        card.accent = accent
        let content = column(views, spacing: spacing)
        content.translatesAutoresizingMaskIntoConstraints = false
        card.addSubview(content)
        NSLayoutConstraint.activate([
            content.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: padding),
            content.trailingAnchor.constraint(equalTo: card.trailingAnchor, constant: -padding),
            content.topAnchor.constraint(equalTo: card.topAnchor, constant: padding),
            content.bottomAnchor.constraint(equalTo: card.bottomAnchor, constant: -padding)
        ])
        return card
    }
}

private final class UsageSurfaceView: NSView {
    var accent: NSColor?
    override func draw(_ dirtyRect: NSRect) {
        let shape = NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), xRadius: 14, yRadius: 14)
        NSColor.controlBackgroundColor.withAlphaComponent(0.84).setFill()
        shape.fill()
        if let accent {
            accent.withAlphaComponent(0.035).setFill()
            shape.fill()
        }
        (accent ?? .separatorColor).withAlphaComponent(accent == nil ? 0.28 : 0.23).setStroke()
        shape.lineWidth = 1
        shape.stroke()
    }
    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        needsDisplay = true
    }
}

private final class UsageMetricGrid: NSView {
    private let cards: [NSView]
    private var columnCount = 3
    private let cardHeight: CGFloat = 148
    init(cards: [NSView]) {
        self.cards = cards
        super.init(frame: .zero)
        cards.forEach(addSubview)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override var isFlipped: Bool { true }
    override var intrinsicContentSize: NSSize {
        let rows = Int(ceil(Double(cards.count) / Double(max(1, columnCount))))
        return NSSize(width: NSView.noIntrinsicMetric, height: CGFloat(rows) * cardHeight + CGFloat(max(0, rows - 1)) * 10)
    }
    override func layout() {
        let columns = min(max(1, cards.count), bounds.width >= 440 ? 3 : bounds.width >= 290 ? 2 : 1)
        if columnCount != columns { columnCount = columns; invalidateIntrinsicContentSize() }
        let width = max(0, (bounds.width - CGFloat(columns - 1) * 10) / CGFloat(columns))
        for (index, card) in cards.enumerated() {
            card.frame = NSRect(x: CGFloat(index % columns) * (width + 10), y: CGFloat(index / columns) * (cardHeight + 10), width: width, height: cardHeight)
        }
        super.layout()
    }
}

private final class UsageAllowanceGraphic: NSView {
    private let remaining: Double
    private let tint: NSColor
    init(remaining: Double, tint: NSColor = .systemTeal) {
        self.remaining = min(100, max(0, remaining))
        self.tint = tint
        super.init(frame: .zero)
        heightAnchor.constraint(equalToConstant: 7).isActive = true
        setAccessibilityElement(true)
        setAccessibilityRole(.progressIndicator)
        setAccessibilityLabel("Allowance remaining")
        setAccessibilityValue(String(format: "%.0f percent", self.remaining))
        toolTip = String(format: "%.1f%% remaining", self.remaining)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override func draw(_ dirtyRect: NSRect) {
        tint.withAlphaComponent(0.12).setFill()
        NSBezierPath(roundedRect: bounds, xRadius: 3.5, yRadius: 3.5).fill()
        tint.setFill()
        NSBezierPath(roundedRect: NSRect(x: 0, y: 0, width: bounds.width * remaining / 100, height: bounds.height), xRadius: 3.5, yRadius: 3.5).fill()
    }
    override func viewDidChangeEffectiveAppearance() { super.viewDidChangeEffectiveAppearance(); needsDisplay = true }
}

private final class UsageRequestGraphic: NSView {
    struct Slice { let name: String; let count: Int; let color: NSColor }
    private let slices: [Slice]
    private let total: Int
    init(records: [UsageRecord]) {
        let groups = Dictionary(grouping: records, by: \.providerKey)
        let sorted = groups.sorted { first, second in
            first.value.count == second.value.count ? first.key < second.key : first.value.count > second.value.count
        }
        var slices = sorted.prefix(4).map { Slice(name: $0.value.first?.providerName ?? "Provider", count: $0.value.count, color: UsageValues.color($0.key)) }
        if sorted.count > 4 { slices.append(Slice(name: "Other providers", count: sorted.dropFirst(4).reduce(0) { $0 + $1.value.count }, color: .systemGray)) }
        self.slices = slices
        total = records.count
        super.init(frame: .zero)
        heightAnchor.constraint(equalToConstant: 156).isActive = true
        setAccessibilityElement(true)
        setAccessibilityRole(.image)
        setAccessibilityLabel("Recorded request distribution. " + (slices.isEmpty ? "No records in this period." : slices.map { "\($0.name): \($0.count)" }.joined(separator: ", ")))
        toolTip = slices.map { "\($0.name): \($0.count) recorded requests" }.joined(separator: "\n")
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override func draw(_ dirtyRect: NSRect) {
        let radius: CGFloat = bounds.width < 330 ? 46 : 56
        let center = NSPoint(x: radius + 13, y: bounds.midY)
        let track = NSBezierPath()
        track.appendArc(withCenter: center, radius: radius, startAngle: 0, endAngle: 360)
        track.lineWidth = 13
        NSColor.separatorColor.withAlphaComponent(0.17).setStroke()
        track.stroke()
        var angle: CGFloat = 90
        for slice in slices {
            let sweep = 360 * CGFloat(slice.count) / CGFloat(max(1, total))
            let arc = NSBezierPath()
            arc.appendArc(withCenter: center, radius: radius, startAngle: angle - min(2, sweep / 4), endAngle: angle - sweep + min(2, sweep / 4), clockwise: true)
            arc.lineWidth = 13
            slice.color.setStroke()
            arc.stroke()
            angle -= sweep
        }
        let totalText = total == 0 ? "—" : UsageValues.count(Double(total))
        let totalStyle: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 28, weight: .semibold), .foregroundColor: NSColor.labelColor]
        let textSize = (totalText as NSString).size(withAttributes: totalStyle)
        (totalText as NSString).draw(at: NSPoint(x: center.x - textSize.width / 2, y: center.y - 6), withAttributes: totalStyle)
        let caption = total == 0 ? "no records" : "requests"
        let captionStyle: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 11), .foregroundColor: NSColor.secondaryLabelColor]
        let captionSize = (caption as NSString).size(withAttributes: captionStyle)
        (caption as NSString).draw(at: NSPoint(x: center.x - captionSize.width / 2, y: center.y - 22), withAttributes: captionStyle)
        let legendX = center.x + radius + 28
        if slices.isEmpty {
            let paragraph = NSMutableParagraphStyle()
            paragraph.lineBreakMode = .byWordWrapping
            ("Your next Model Deck launch will appear here." as NSString).draw(in: NSRect(x: legendX, y: 38, width: max(50, bounds.width - legendX), height: 72), withAttributes: [.font: NSFont.systemFont(ofSize: 12), .foregroundColor: NSColor.secondaryLabelColor, .paragraphStyle: paragraph])
        }
        for (index, slice) in slices.enumerated() {
            let rowY = bounds.midY + CGFloat(slices.count - 1) * 12 - CGFloat(index) * 24
            slice.color.setFill()
            NSBezierPath(ovalIn: NSRect(x: legendX, y: rowY + 4, width: 7, height: 7)).fill()
            let paragraph = NSMutableParagraphStyle()
            paragraph.lineBreakMode = .byTruncatingTail
            (slice.name as NSString).draw(in: NSRect(x: legendX + 15, y: rowY, width: max(20, bounds.width - legendX - 52), height: 17), withAttributes: [.font: NSFont.systemFont(ofSize: 12, weight: .medium), .foregroundColor: NSColor.labelColor, .paragraphStyle: paragraph])
            let count = "\(slice.count)"
            let style: [NSAttributedString.Key: Any] = [.font: NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .medium), .foregroundColor: NSColor.secondaryLabelColor]
            let size = (count as NSString).size(withAttributes: style)
            (count as NSString).draw(at: NSPoint(x: bounds.width - size.width, y: rowY), withAttributes: style)
        }
    }
    override func viewDidChangeEffectiveAppearance() { super.viewDidChangeEffectiveAppearance(); needsDisplay = true }
}

private final class UsageDailyTokenGraphic: NSView {
    private let samples: [(String, Double?)]
    init(samples: [(String, Double?)]) {
        self.samples = Array(samples.suffix(14))
        super.init(frame: .zero)
        heightAnchor.constraint(equalToConstant: 114).isActive = true
        let description = self.samples.map { "\($0.0): " + ($0.1.map { NumberFormatter.localizedString(from: NSNumber(value: $0), number: .decimal) } ?? "not reported") }.joined(separator: "; ")
        toolTip = description
        setAccessibilityElement(true)
        setAccessibilityRole(.image)
        setAccessibilityLabel("Daily tokens reported by OpenAI. " + description)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override func draw(_ dirtyRect: NSRect) {
        guard !samples.isEmpty else { return }
        let maximum = max(1, samples.compactMap { $0.1 }.max() ?? 1)
        let slot = bounds.width / CGFloat(samples.count)
        let baseline: CGFloat = 25
        for (index, sample) in samples.enumerated() {
            let origin = CGFloat(index) * slot + 3
            if let value = sample.1 {
                NSColor.systemTeal.withAlphaComponent(index == samples.count - 1 ? 0.95 : 0.48).setFill()
                let height = max(value == 0 ? 1 : 3, CGFloat(value / maximum) * 77)
                NSBezierPath(roundedRect: NSRect(x: origin, y: baseline, width: max(2, slot - 6), height: height), xRadius: 3, yRadius: 3).fill()
            } else {
                ("—" as NSString).draw(at: NSPoint(x: origin, y: baseline + 2), withAttributes: [.font: NSFont.systemFont(ofSize: 11), .foregroundColor: NSColor.tertiaryLabelColor])
            }
            if index == 0 || index == samples.count - 1 || (samples.count <= 7 && slot >= 40) {
                let date = String(sample.0.suffix(5))
                let style: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 10), .foregroundColor: NSColor.secondaryLabelColor]
                let textWidth = (date as NSString).size(withAttributes: style).width
                let labelX = min(max(0, origin), max(0, bounds.width - textWidth))
                (date as NSString).draw(at: NSPoint(x: labelX, y: 5), withAttributes: style)
            }
        }
    }
    override func viewDidChangeEffectiveAppearance() { super.viewDidChangeEffectiveAppearance(); needsDisplay = true }
}

final class UsageDashboardView: NSView, NSSearchFieldDelegate {
    var onRefresh: (() -> Void)?
    var onOpenRouterAccountSelected: ((String) -> Void)?

    private var accounts: [SavedAccount] = []
    private var subscription: [String: Any] = [:]
    private var openRouter: [String: Any] = [:]
    private var openRouterAccountID = ""
    private var records: [UsageRecord] = []
    private var providers: [UsageProvider] = []
    private var selectedProviderID = "chatgpt"
    private var selectedActivityKey = "all"
    private var activityPage = 0
    private let pageSize = 6
    private var isRefreshing = false
    private let tabs = NSSegmentedControl(labels: ["Overview", "Providers", "Activity"], trackingMode: .selectOne, target: nil, action: nil)
    private let period = NSPopUpButton(frame: .zero, pullsDown: false)
    private let providerPicker = NSPopUpButton(frame: .zero, pullsDown: false)
    private let activityPicker = NSPopUpButton(frame: .zero, pullsDown: false)
    private let search = NSSearchField()
    private let refresh = NSButton(title: "Refresh", target: nil, action: nil)
    private let refreshStatus = UsageViewStyle.label("Refresh to load provider usage.", size: 11)
    private let content = NSStackView()
    private let root = NSStackView()
    private let providerControls = NSStackView()
    private let activityControls = NSStackView()
    private var lastAccountChoices: [String] = []
    private var lastActivityChoices: [String] = []

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        buildView()
        rebuildProviders()
        rebuildContent()
    }
    convenience init() { self.init(frame: .zero) }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    private func buildView() {
        let title = UsageViewStyle.label("Usage", size: 28, weight: .bold, color: .labelColor)
        refresh.target = self
        refresh.action = #selector(refreshClicked)
        refresh.bezelStyle = .rounded
        refresh.image = NSImage(systemSymbolName: "arrow.clockwise", accessibilityDescription: "Refresh usage")
        refresh.imagePosition = .imageLeading
        refresh.setContentHuggingPriority(.required, for: .horizontal)
        let heading = UsageViewStyle.row([title, NSView(), refresh])
        tabs.target = self
        tabs.action = #selector(tabChanged)
        tabs.selectedSegment = 0
        tabs.segmentStyle = .rounded
        tabs.segmentDistribution = .fill
        tabs.setAccessibilityLabel("Usage sections")
        tabs.heightAnchor.constraint(equalToConstant: 30).isActive = true
        period.addItems(withTitles: ["Today", "Last 7 days", "Last 30 days"])
        period.selectItem(at: 1)
        period.target = self
        period.action = #selector(periodChanged)
        period.setAccessibilityLabel("Local request period")
        period.setContentHuggingPriority(.required, for: .horizontal)
        let scope = UsageViewStyle.label("RECORDED ACTIVITY", size: 10, weight: .semibold)
        let periodRow = UsageViewStyle.row([scope, NSView(), period])
        providerPicker.target = self
        providerPicker.action = #selector(providerChanged)
        providerPicker.setAccessibilityLabel("Provider connection")
        providerControls.orientation = .vertical
        providerControls.alignment = .leading
        providerControls.spacing = 5
        providerControls.addArrangedSubview(UsageViewStyle.label("Provider connection", size: 11, weight: .medium))
        providerControls.addArrangedSubview(providerPicker)
        providerPicker.widthAnchor.constraint(equalTo: providerControls.widthAnchor).isActive = true
        activityPicker.target = self
        activityPicker.action = #selector(activityFilterChanged)
        activityPicker.setAccessibilityLabel("Filter activity by provider")
        search.placeholderString = "Search models or agents"
        search.delegate = self
        search.setAccessibilityLabel("Search recorded requests")
        activityControls.orientation = .vertical
        activityControls.alignment = .leading
        activityControls.spacing = 8
        for control in [activityPicker, search] as [NSView] {
            activityControls.addArrangedSubview(control)
            control.widthAnchor.constraint(equalTo: activityControls.widthAnchor).isActive = true
        }
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 12
        root.translatesAutoresizingMaskIntoConstraints = false
        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 12
        for view in [heading, UsageViewStyle.label("Your providers, at a glance.", size: 13), refreshStatus, tabs, periodRow, providerControls, activityControls, content] {
            root.addArrangedSubview(view)
            view.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        }
        addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: leadingAnchor), root.trailingAnchor.constraint(equalTo: trailingAnchor),
            root.topAnchor.constraint(equalTo: topAnchor), root.bottomAnchor.constraint(equalTo: bottomAnchor)
        ])
    }

    func update(accounts: [SavedAccount], subscription: [String: Any], openRouter: [String: Any], openRouterAccountID: String, entries: [[String: Any]], updatedAt: Date?, refreshMessage: String?, isRefreshing: Bool) {
        self.accounts = accounts
        self.subscription = subscription
        self.openRouter = openRouter
        self.openRouterAccountID = openRouterAccountID
        self.records = entries.map(UsageRecord.init)
        self.isRefreshing = isRefreshing
        refresh.isEnabled = !isRefreshing
        refresh.title = isRefreshing ? "Refreshing…" : "Refresh"
        let formatter = DateFormatter()
        formatter.timeStyle = .short
        let timestamp = updatedAt.map { "Updated " + formatter.string(from: $0) + " · Auto-refresh on" } ?? "Provider usage has not been refreshed."
        refreshStatus.stringValue = [isRefreshing ? "Refreshing provider usage…" : timestamp, refreshMessage].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: "\n")
        refreshStatus.textColor = refreshMessage == nil ? .secondaryLabelColor : .systemOrange
        rebuildProviders()
        rebuildContent()
    }

    func selectTab(_ index: Int) {
        tabs.selectedSegment = min(2, max(0, index))
        rebuildContent()
    }

    // Accepts a saved account ID, or a provider group such as chatgpt or cursor.
    func selectProvider(_ id: String) {
        guard let provider = providers.first(where: { $0.id == id || $0.accountID == id || $0.localKey == id }) else { return }
        selectedProviderID = provider.id
        providerPicker.selectItem(at: providers.firstIndex(where: { $0.id == provider.id }) ?? 0)
        tabs.selectedSegment = 1
        rebuildContent()
    }

    private func rebuildProviders() {
        providers = [UsageProvider(id: "chatgpt", name: "ChatGPT", localKey: "chatgpt", accountID: nil, saved: false)]
        for account in accounts {
            let key = account.isCursor ? "cursor" : account.isOpenRouter ? "openrouter" : "endpoint:" + account.name
            let family = account.isCursor ? "Cursor" : account.isOpenRouter ? "OpenRouter" : ""
            let name = family.isEmpty || account.name == family ? account.name : family + " · " + account.name
            providers.append(UsageProvider(id: "account:" + account.id, name: name, localKey: key, accountID: account.id, saved: true))
        }
        var represented = Set(providers.map(\.localKey))
        for record in records where represented.insert(record.providerKey).inserted {
            providers.append(UsageProvider(id: "history:" + record.providerKey, name: record.providerName + " · Recorded", localKey: record.providerKey, accountID: nil, saved: false))
        }
        if !providers.contains(where: { $0.id == selectedProviderID }) { selectedProviderID = "chatgpt" }
        let choices = providers.map { $0.id + "|" + $0.name }
        if choices != lastAccountChoices {
            lastAccountChoices = choices
            providerPicker.removeAllItems()
            for provider in providers {
                providerPicker.addItem(withTitle: provider.name)
                providerPicker.lastItem?.representedObject = provider.id
            }
        }
        providerPicker.selectItem(at: providers.firstIndex(where: { $0.id == selectedProviderID }) ?? 0)
        var groupNames: [String: String] = [:]
        for provider in providers {
            if groupNames[provider.localKey] == nil { groupNames[provider.localKey] = Self.groupName(provider.localKey, fallback: provider.name) }
        }
        let groupKeys = groupNames.keys.sorted()
        if !groupKeys.contains(selectedActivityKey) { selectedActivityKey = "all" }
        let activityChoices = groupKeys.map { $0 + "|" + (groupNames[$0] ?? $0) }
        if activityChoices != lastActivityChoices {
            lastActivityChoices = activityChoices
            activityPicker.removeAllItems()
            activityPicker.addItem(withTitle: "All providers")
            activityPicker.lastItem?.representedObject = "all"
            for key in groupKeys {
                activityPicker.addItem(withTitle: groupNames[key] ?? key)
                activityPicker.lastItem?.representedObject = key
            }
        }
        activityPicker.selectItem(at: selectedActivityKey == "all" ? 0 : (groupKeys.firstIndex(of: selectedActivityKey) ?? -1) + 1)
    }

    private static func groupName(_ key: String, fallback: String) -> String {
        switch key {
        case "chatgpt": return "ChatGPT"
        case "openrouter": return "OpenRouter"
        case "cursor": return "Cursor"
        default: return key.hasPrefix("endpoint:") ? String(key.dropFirst(9)) : fallback
        }
    }

    private static func filteredRecords(_ records: [UsageRecord], days: Int, now: Date, calendar: Calendar = .current) -> [UsageRecord] {
        let start = calendar.date(byAdding: .day, value: -(days - 1), to: calendar.startOfDay(for: now)) ?? now
        return records.filter { record in record.date.map { $0 >= start && $0 <= now } ?? false }
    }

    private var periodRecords: [UsageRecord] { UsageFiltering.filteredRecords(records, days: [1, 7, 30][max(0, min(2, period.indexOfSelectedItem))], now: Date()) }
    private var periodName: String { ["Today", "Last 7 days", "Last 30 days"][max(0, min(2, period.indexOfSelectedItem))] }

    private func rebuildContent() {
        providerControls.isHidden = tabs.selectedSegment != 1
        activityControls.isHidden = tabs.selectedSegment != 2
        content.arrangedSubviews.forEach { content.removeArrangedSubview($0); $0.removeFromSuperview() }
        let views: [NSView]
        switch tabs.selectedSegment {
        case 1: views = providerViews()
        case 2: views = activityViews()
        default: views = overviewViews()
        }
        for view in views {
            content.addArrangedSubview(view)
            view.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true
        }
    }

    private func metricCard(name: String, value: String, caption: String, detail: String, key: String, remaining: Double? = nil) -> NSView {
        let title = UsageViewStyle.label(name, size: 12, weight: .semibold, color: UsageValues.color(key))
        title.maximumNumberOfLines = 1
        title.lineBreakMode = .byTruncatingTail
        let amount = UsageViewStyle.label(value, size: value.count > 13 ? 18 : 27, weight: .semibold, color: .labelColor)
        amount.maximumNumberOfLines = 1
        let subtitle = UsageViewStyle.label(caption, size: 11, weight: .medium)
        subtitle.maximumNumberOfLines = 1
        subtitle.lineBreakMode = .byTruncatingTail
        let footnote = UsageViewStyle.label(detail, size: 10)
        footnote.maximumNumberOfLines = 2
        var body: [NSView] = [title, amount, subtitle]
        if let remaining { body.append(UsageAllowanceGraphic(remaining: remaining, tint: UsageValues.color(key))) }
        body.append(footnote)
        let card = UsageViewStyle.card(body, accent: UsageValues.color(key), padding: 13, spacing: 6)
        card.toolTip = [name, value, caption, detail].joined(separator: ". ")
        return card
    }

    private func overviewViews() -> [NSView] {
        let windows = subscription["windows"] as? [[String: Any]] ?? []
        let allowance = UsageFiltering.overviewAllowance(windows)
        let remaining = allowance.remaining
        var cards: [NSView] = [metricCard(name: "ChatGPT", value: remaining.map { String(format: "%.0f%%", $0) } ?? "Unavailable", caption: allowance.name, detail: remaining == nil ? (isRefreshing ? "Loading subscription limits" : "See Providers for details") : "Allowance left · subscription", key: "chatgpt", remaining: remaining)]
        let local = periodRecords
        let hasOpenRouter = providers.contains { $0.localKey == "openrouter" }
        if hasOpenRouter {
            let account = accounts.first { $0.id == openRouterAccountID && $0.isOpenRouter }
            let monthly = account == nil || openRouter["ok"] as? Bool != true ? nil : UsageValues.number(openRouter["usage_monthly"] ?? openRouter["usage_month"])
            cards.append(metricCard(name: "OpenRouter", value: UsageValues.dollars(monthly), caption: "This month · selected key", detail: account.map { $0.name + " · UTC" } ?? "Select a saved key in Providers", key: "openrouter"))
        }
        if providers.contains(where: { $0.localKey == "cursor" }) {
            let totals = UsageTotals(local.filter { $0.providerKey == "cursor" })
            cards.append(metricCard(name: "Cursor", value: UsageValues.dollars(totals.reportedCost), caption: "Recorded charges · USD", detail: "\(totals.count) requests · " + periodName.lowercased(), key: "cursor"))
        }
        if cards.count < 3, let other = providers.first(where: { $0.localKey.hasPrefix("endpoint:") }) {
            let totals = UsageTotals(local.filter { $0.providerKey == other.localKey })
            cards.append(metricCard(name: Self.groupName(other.localKey, fallback: other.name), value: "\(totals.count)", caption: "Recorded requests", detail: UsageValues.dollars(totals.reportedCost) + " · " + periodName.lowercased(), key: other.localKey))
        }
        let totals = UsageTotals(local)
        let cost = metricText(value: UsageValues.dollars(totals.reportedCost), title: "Reported charges · USD", detail: "\(totals.unknownCosts) costs unknown · \(totals.subscriptionCount) subscription")
        let token = metricText(value: totals.reportedTokens.map(UsageValues.count) ?? "Not reported", title: "Reported tokens", detail: "Available for \(totals.tokenCoverage) of \(totals.count) requests")
        let metrics = UsageViewStyle.row([cost, token], spacing: 16)
        cost.widthAnchor.constraint(equalTo: token.widthAnchor).isActive = true
        let header = UsageViewStyle.row([UsageViewStyle.label("Where your requests go", size: 15, weight: .semibold, color: .labelColor), NSView()])
        let chart = UsageViewStyle.card([header, UsageRequestGraphic(records: local), UsageViewStyle.rule(), metrics])
        let connectionCount = accounts.count
        let footer = UsageViewStyle.label("\(connectionCount) saved connection\(connectionCount == 1 ? "" : "s") · Explore each in Providers.\nLocal totals cover recent Model Deck launches and may be incomplete.", size: 11)
        return [UsageMetricGrid(cards: cards), chart, footer]
    }

    private func metricText(value: String, title: String, detail: String) -> NSView {
        UsageViewStyle.column([
            UsageViewStyle.label(title, size: 10, weight: .medium),
            UsageViewStyle.label(value, size: 21, weight: .semibold, color: .labelColor),
            UsageViewStyle.label(detail, size: 10)
        ], spacing: 4)
    }

    private static func overviewAllowance(_ windows: [[String: Any]]) -> (remaining: Double?, name: String) {
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

    private func providerViews() -> [NSView] {
        guard let provider = providers.first(where: { $0.id == selectedProviderID }) else { return [] }
        var views: [NSView] = []
        if provider.localKey == "chatgpt" {
            views.append(contentsOf: subscriptionViews())
        } else if provider.localKey == "openrouter", let accountID = provider.accountID {
            views.append(contentsOf: openRouterViews(accountID: accountID))
        } else if provider.localKey == "cursor" {
            views.append(UsageViewStyle.card([
                UsageViewStyle.label("Cursor", size: 20, weight: .semibold, color: .labelColor),
                UsageViewStyle.label(provider.saved ? "Saved connection · billed to your Cursor account" : "Previously recorded provider"),
                UsageViewStyle.label("Reported charges and token deltas appear below. Your account allowance, IDE and Cloud Agent pools, and unreported charges are available in Cursor.", size: 12),
                linkButton("View Cursor account ↗", url: "https://cursor.com/dashboard")
            ], accent: .systemBlue))
        } else {
            views.append(UsageViewStyle.card([
                UsageViewStyle.label(provider.name, size: 19, weight: .semibold, color: .labelColor),
                UsageViewStyle.label(provider.saved ? "Saved connection · live authentication not checked here" : "Historical activity · no matching saved connection"),
                UsageViewStyle.label("Account-wide allowance and billing totals are not available here. The local records below show only usage reported to Model Deck.")
            ], accent: provider.color))
        }
        let local = periodRecords.filter { $0.providerKey == provider.localKey }
        let totals = UsageTotals(local)
        let cost = metricText(value: provider.localKey == "chatgpt" ? "Subscription" : UsageValues.dollars(totals.reportedCost), title: "Reported charges · USD", detail: provider.localKey == "chatgpt" ? "Covered by your subscription" : "\(totals.unknownCosts) records with unknown cost")
        let tokens = metricText(value: totals.reportedTokens.map(UsageValues.count) ?? "Not reported", title: "Reported tokens", detail: "Coverage: \(totals.tokenCoverage) of \(totals.count) records")
        let metrics = UsageViewStyle.row([cost, tokens], spacing: 16)
        cost.widthAnchor.constraint(equalTo: tokens.widthAnchor).isActive = true
        let scope = provider.localKey == "chatgpt" ? "Recorded requests using your ChatGPT subscription." : provider.localKey.hasPrefix("endpoint:") ? "Matched by endpoint name; individual accounts cannot be distinguished." : "Grouped by provider across local records; not attributed to this individual connection."
        views.append(UsageViewStyle.card([
            UsageViewStyle.label("Recorded locally · " + periodName.lowercased(), size: 15, weight: .semibold, color: .labelColor),
            UsageViewStyle.label("\(totals.count) recorded requests · \(totals.failedCount) failed", size: 12, weight: .medium),
            metrics,
            UsageViewStyle.rule(),
            UsageViewStyle.label(scope + (provider.localKey == "cursor" ? " An SDK run may include multiple recorded requests." : ""), size: 11)
        ]))
        views.append(UsageViewStyle.label("Recent local history may be incomplete. The period picker changes local activity only; provider allowance and billing periods above stay unchanged.", size: 11))
        return views
    }

    private func subscriptionViews() -> [NSView] {
        let windows = subscription["windows"] as? [[String: Any]] ?? []
        var sections: [NSView] = []
        if windows.isEmpty {
            sections.append(UsageViewStyle.card([
                UsageViewStyle.label("ChatGPT subscription", size: 20, weight: .semibold, color: .labelColor),
                UsageViewStyle.label(subscription["error"] as? String ?? (isRefreshing ? "Loading allowance windows…" : "Allowance windows are unavailable. Refresh to try again.")),
                linkButton("View ChatGPT ↗", url: "https://chatgpt.com")
            ], accent: .systemTeal))
        } else {
            var body: [NSView] = [UsageViewStyle.label("ChatGPT allowance", size: 20, weight: .semibold, color: .labelColor)]
            for (index, window) in windows.enumerated() {
                if index > 0 { body.append(UsageViewStyle.rule()) }
                let pool = window["pool_name"] as? String ?? window["pool_id"] as? String ?? "Subscription"
                let kind = window["window_kind"] as? String ?? "Usage window"
                let duration = UsageValues.number(window["window_minutes"]).map { minutes in
                    minutes >= 1440 ? "\(UsageValues.count(minutes / 1440))-day" : "\(UsageValues.count(minutes / 60))-hour"
                }
                let name = pool + " · " + (duration ?? kind)
                let used = UsageValues.number(window["used_percent"])
                body.append(UsageViewStyle.label(name, size: 12, weight: .semibold, color: .labelColor))
                if let used {
                    let remaining = max(0, 100 - used)
                    body.append(UsageViewStyle.label(String(format: "%.0f%% remaining · %.0f%% used", remaining, used), size: 12))
                    body.append(UsageAllowanceGraphic(remaining: remaining))
                } else { body.append(UsageViewStyle.label("Usage percentage unavailable")) }
                body.append(UsageViewStyle.label(UsageValues.reset(window["resets_at"]), size: 11))
            }
            body.append(UsageViewStyle.label("Windows run simultaneously. Each is a separate limit.", size: 11))
            sections.append(UsageViewStyle.card(body, accent: .systemTeal))
        }
        let days = subscription["daily_usage"] as? [[String: Any]] ?? []
        let samples = days.compactMap { day -> (String, Double?)? in
            guard let date = day["date"] as? String else { return nil }
            return (date, UsageValues.number(day["tokens"]))
        }.sorted { $0.0 < $1.0 }
        if !samples.isEmpty {
            var body: [NSView] = [UsageViewStyle.label("Daily token activity", size: 15, weight: .semibold, color: .labelColor), UsageDailyTokenGraphic(samples: samples)]
            let summary = subscription["summary"] as? [String: Any] ?? [:]
            var facts: [String] = []
            if let lifetime = UsageValues.number(subscription["lifetime_tokens"]) { facts.append(UsageValues.count(lifetime) + " lifetime tokens") }
            if let peak = UsageValues.number(summary["peak_daily_tokens"]) { facts.append(UsageValues.count(peak) + " peak daily") }
            if let streak = UsageValues.number(summary["current_streak_days"]) { facts.append(UsageValues.count(streak) + "-day streak") }
            if !facts.isEmpty { body.append(UsageViewStyle.label(facts.joined(separator: " · "), size: 11)) }
            body.append(UsageViewStyle.label("Last \(min(14, samples.count)) reported dates from OpenAI. A dash means unavailable. Hover for exact counts.", size: 11))
            sections.append(UsageViewStyle.card(body))
        }
        if let error = subscription["usage_error"] as? String { sections.append(UsageViewStyle.label(error, color: .systemOrange)) }
        return sections
    }

    private func openRouterViews(accountID: String) -> [NSView] {
        let matches = accountID == openRouterAccountID
        let available = matches && (openRouter["ok"] as? Bool == true)
        var body: [NSView] = [UsageViewStyle.label("OpenRouter spending", size: 20, weight: .semibold, color: .labelColor), UsageViewStyle.label("Provider-reported USD · this saved key · UTC", size: 11)]
        if available {
            let daily = UsageValues.number(openRouter["usage_daily"])
            let weekly = UsageValues.number(openRouter["usage_weekly"] ?? openRouter["usage_week"])
            let monthly = UsageValues.number(openRouter["usage_monthly"] ?? openRouter["usage_month"])
            let total = UsageValues.number(openRouter["usage_total"] ?? openRouter["usage"])
            let cards = [("Today", daily), ("This week", weekly), ("This month", monthly), ("All time", total)].map { title, value in
                metricText(value: UsageValues.dollars(value), title: title, detail: "Selected key")
            }
            let first = UsageViewStyle.row(Array(cards.prefix(2)), spacing: 20)
            cards[0].widthAnchor.constraint(equalTo: cards[1].widthAnchor).isActive = true
            let second = UsageViewStyle.row(Array(cards.suffix(2)), spacing: 20)
            cards[2].widthAnchor.constraint(equalTo: cards[3].widthAnchor).isActive = true
            body.append(contentsOf: [first, second, UsageViewStyle.rule()])
            if let cap = UsageValues.number(openRouter["limit"]) {
                body.append(UsageViewStyle.label("Key cap " + UsageValues.dollars(cap) + " · " + UsageValues.dollars(UsageValues.number(openRouter["limit_remaining"])) + " remaining", size: 12, weight: .medium))
                if let remaining = UsageValues.number(openRouter["limit_remaining"]), cap > 0 { body.append(UsageAllowanceGraphic(remaining: 100 * remaining / cap, tint: .systemIndigo)) }
            } else { body.append(UsageViewStyle.label(openRouter["limit_known"] as? Bool == true ? "No key spending cap" : "Key spending cap unavailable")) }
            if let reset = openRouter["limit_reset"] as? String, !reset.isEmpty { body.append(UsageViewStyle.label("Key cap reset: " + reset, size: 11)) }
            if let byok = UsageValues.number(openRouter["byok_usage"]) {
                body.append(UsageViewStyle.label("BYOK: " + UsageValues.dollars(byok) + " reported separately; not added to these totals.", size: 11))
                if let included = openRouter["include_byok_in_limit"] as? Bool { body.append(UsageViewStyle.label(included ? "BYOK counts toward this key’s cap." : "BYOK does not count toward this key’s cap.", size: 11)) }
            }
            body.append(UsageViewStyle.label("Periods overlap and are not added together. These totals include every app using this key.", size: 11))
        } else {
            let message = matches ? openRouter["error"] as? String : nil
            body.append(UsageViewStyle.label(message ?? (isRefreshing ? "Loading usage for this key…" : "Usage for this key has not been loaded. Refresh to check it.")))
        }
        body.append(linkButton("View OpenRouter activity ↗", url: "https://openrouter.ai/activity"))
        return [UsageViewStyle.card(body, accent: .systemIndigo)]
    }

    private func activityViews() -> [NSView] {
        let query = search.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let filtered = periodRecords.filter { record in
            (selectedActivityKey == "all" || record.providerKey == selectedActivityKey) && UsageFiltering.matchesSearch(record, query: query)
        }.sorted { ($0.date ?? .distantPast) > ($1.date ?? .distantPast) }
        let pageCount = max(1, Int(ceil(Double(filtered.count) / Double(pageSize))))
        activityPage = min(activityPage, pageCount - 1)
        var rows: [NSView] = []
        if filtered.isEmpty {
            rows.append(UsageViewStyle.label(query.isEmpty ? "No recorded requests in this period." : "No requests match your search.", size: 16, weight: .semibold, color: .labelColor))
            rows.append(UsageViewStyle.label("Try another period or provider. Requests launched from Model Deck appear here."))
        } else {
            let first = activityPage * pageSize
            let visible = filtered.dropFirst(first).prefix(pageSize)
            for (index, record) in visible.enumerated() {
                if index > 0 { rows.append(UsageViewStyle.rule()) }
                rows.append(activityRow(record))
            }
        }
        let previous = NSButton(title: "Previous", target: self, action: #selector(previousPage))
        previous.bezelStyle = .rounded
        previous.isEnabled = activityPage > 0
        let next = NSButton(title: "Next", target: self, action: #selector(nextPage))
        next.bezelStyle = .rounded
        next.isEnabled = activityPage < pageCount - 1
        let paging = UsageViewStyle.row([previous, NSView(), UsageViewStyle.label("\(activityPage + 1) / \(pageCount)", size: 11), NSView(), next])
        let undated = records.filter { $0.date == nil }.count
        let scope = "Showing \(filtered.count) matching records from recent local history, which may be incomplete. Local dates; Model Deck launches only." + (undated > 0 ? " \(undated) undated records are excluded from period totals." : "")
        return [UsageViewStyle.label("\(filtered.count) recorded requests · " + periodName.lowercased(), size: 12, weight: .medium), UsageViewStyle.card(rows), paging, UsageViewStyle.label(scope, size: 11)]
    }

    private func activityRow(_ record: UsageRecord) -> NSView {
        let model = UsageViewStyle.label(record.model, size: 12, weight: .semibold, color: .labelColor)
        model.font = .monospacedSystemFont(ofSize: 12, weight: .medium)
        model.maximumNumberOfLines = 1
        model.lineBreakMode = .byTruncatingMiddle
        model.toolTip = record.model
        let formatter = DateFormatter()
        formatter.dateFormat = "MMM d, HH:mm"
        let timestamp = record.date.map(formatter.string(from:)) ?? "Unknown time"
        let outcome = (record.status ?? 0) >= 400 ? "Failed \(record.status ?? 0)" : record.isSubscription ? "Subscription" : UsageValues.dollars(record.cost)
        let charge = UsageViewStyle.label(outcome, size: 11, weight: .medium, color: (record.status ?? 0) >= 400 ? .systemRed : UsageValues.color(record.providerKey))
        let heading = UsageViewStyle.row([model, charge])
        charge.setContentCompressionResistancePriority(.required, for: .horizontal)
        charge.setContentHuggingPriority(.required, for: .horizontal)
        let meta = timestamp + " · " + record.agent + " · " + record.providerName
        let detail = record.tokens.map { UsageValues.count($0) + " tokens reported" } ?? "Tokens not reported"
        let row = UsageViewStyle.column([heading, UsageViewStyle.label(meta, size: 10), UsageViewStyle.label(detail, size: 10)], spacing: 3)
        row.toolTip = meta + "\n" + outcome + (record.providerKey == "cursor" ? " · Cursor SDK-reported charge" : "") + "\n" + detail
        return row
    }

    private func linkButton(_ title: String, url: String) -> NSButton {
        let button = NSButton(title: title, target: self, action: #selector(openProviderURL(_:)))
        button.bezelStyle = .rounded
        button.identifier = NSUserInterfaceItemIdentifier(url)
        return button
    }

    @objc private func openProviderURL(_ sender: NSButton) {
        guard let url = sender.identifier.flatMap({ URL(string: $0.rawValue) }) else { return }
        NSWorkspace.shared.open(url)
    }
    @objc private func refreshClicked() { onRefresh?() }
    @objc private func tabChanged() { rebuildContent() }
    @objc private func periodChanged() { activityPage = 0; rebuildContent() }
    @objc private func providerChanged() {
        selectedProviderID = providerPicker.selectedItem?.representedObject as? String ?? "chatgpt"
        rebuildContent()
        if let provider = providers.first(where: { $0.id == selectedProviderID }), provider.localKey == "openrouter", let accountID = provider.accountID {
            onOpenRouterAccountSelected?(accountID)
        }
    }
    @objc private func activityFilterChanged() {
        selectedActivityKey = activityPicker.selectedItem?.representedObject as? String ?? "all"
        activityPage = 0
        rebuildContent()
    }
    @objc private func previousPage() { activityPage = max(0, activityPage - 1); rebuildContent() }
    @objc private func nextPage() { activityPage += 1; rebuildContent() }
    func controlTextDidChange(_ notification: Notification) { activityPage = 0; rebuildContent() }

    static func selfTest() -> Bool {
        UsageModelSelfTest.run()
    }
}

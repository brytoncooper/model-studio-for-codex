import AppKit
import Darwin
import ApplicationServices
import ModelDeckPresentation
import ModelDeckPlatform
import ModelDeckClient

enum StudioPalette {
    static let indigo = NSColor.systemIndigo
    static let teal = NSColor.systemTeal
    static func color(_ hex: String) -> NSColor {
        let value = UInt64(hex, radix: 16) ?? 0x7357E8
        return NSColor(srgbRed: Double((value >> 16) & 255) / 255,
                       green: Double((value >> 8) & 255) / 255, blue: Double(value & 255) / 255, alpha: 1)
    }
}

final class ModelSelectionTable: NSTableView {
    var toggleSelectedRows: (() -> Void)?
    override func keyDown(with event: NSEvent) {
        if event.charactersIgnoringModifiers == " " && !event.modifierFlags.contains(.command) {
            toggleSelectedRows?()
        } else { super.keyDown(with: event) }
    }
}

/// A native popover replaces long menus with immediate search and keyboard selection.
final class SearchablePicker: NSControl, NSTableViewDataSource, NSTableViewDelegate, NSSearchFieldDelegate {
    private let trigger = NSButton()
    private let search = NSSearchField()
    private let table = NSTableView()
    private let popover = NSPopover()
    private var titles: [String] = []
    private var visibleIndices: [Int] = []
    private var symbols: [String: (String, NSColor)] = [:]
    private(set) var indexOfSelectedItem = -1
    var titleOfSelectedItem: String? { titles.indices.contains(indexOfSelectedItem) ? titles[indexOfSelectedItem] : nil }
    override var isEnabled: Bool { didSet { trigger.isEnabled = isEnabled } }
    override var intrinsicContentSize: NSSize { NSSize(width: 220, height: 36) }
    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        trigger.bezelStyle = .rounded
        trigger.controlSize = .large
        trigger.font = .systemFont(ofSize: 13, weight: .medium)
        trigger.alignment = .left
        trigger.imagePosition = .imageLeading
        trigger.target = self
        trigger.action = #selector(openPicker)
        trigger.translatesAutoresizingMaskIntoConstraints = false
        addSubview(trigger)
        NSLayoutConstraint.activate([
            trigger.leadingAnchor.constraint(equalTo: leadingAnchor), trigger.trailingAnchor.constraint(equalTo: trailingAnchor),
            trigger.topAnchor.constraint(equalTo: topAnchor), trigger.bottomAnchor.constraint(equalTo: bottomAnchor)
        ])
        let controller = NSViewController()
        let content = NSView(frame: NSRect(x: 0, y: 0, width: 320, height: 300))
        controller.view = content
        search.placeholderString = "Find a provider or connection"
        search.sendsSearchStringImmediately = true
        search.target = self
        search.action = #selector(filterChoices)
        search.delegate = self
        search.setAccessibilityLabel("Search provider or connection")
        let tableColumn = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("choice"))
        table.addTableColumn(tableColumn)
        table.headerView = nil
        table.rowHeight = 38
        table.style = .inset
        table.dataSource = self
        table.delegate = self
        table.target = self
        table.action = #selector(chooseRow)
        table.setAccessibilityLabel("Matching providers and connections")
        let scroll = NSScrollView()
        scroll.documentView = table
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        for view in [search, scroll] { view.translatesAutoresizingMaskIntoConstraints = false; content.addSubview(view) }
        NSLayoutConstraint.activate([
            search.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 12),
            search.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -12),
            search.topAnchor.constraint(equalTo: content.topAnchor, constant: 12),
            scroll.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 4),
            scroll.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -4),
            scroll.topAnchor.constraint(equalTo: search.bottomAnchor, constant: 8),
            scroll.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -8)
        ])
        popover.behavior = .transient
        popover.contentViewController = controller
    }
    required init?(coder: NSCoder) { fatalError("Not used") }
    func removeAllItems() { titles = []; indexOfSelectedItem = -1; updateTrigger() }
    func addItem(withTitle title: String) { titles.append(title); if indexOfSelectedItem < 0 { indexOfSelectedItem = 0 }; updateTrigger() }
    func addItems(withTitles newTitles: [String]) { newTitles.forEach { addItem(withTitle: $0) } }
    func selectItem(at index: Int) { guard titles.indices.contains(index) else { return }; indexOfSelectedItem = index; updateTrigger() }
    func setSymbol(_ symbol: String, color: NSColor, for title: String) { symbols[title] = (symbol, color); updateTrigger() }
    private func updateTrigger() {
        trigger.title = (titleOfSelectedItem ?? "Choose a connection") + "  ⌄"
        let style = symbols[titleOfSelectedItem ?? ""] ?? ("network", .secondaryLabelColor)
        trigger.image = NSImage(systemSymbolName: style.0, accessibilityDescription: nil)
        trigger.contentTintColor = style.1
        trigger.setAccessibilityLabel(titleOfSelectedItem ?? "Choose a connection")
    }
    @objc private func openPicker() {
        search.stringValue = ""
        filterChoices()
        popover.contentSize = NSSize(width: max(290, min(380, bounds.width)), height: 300)
        popover.show(relativeTo: bounds, of: self, preferredEdge: .maxY)
        popover.contentViewController?.view.window?.makeFirstResponder(search)
    }
    @objc private func filterChoices() {
        visibleIndices = titles.indices.filter { CatalogModel.matches(search.stringValue, text: titles[$0]) }
        table.reloadData()
        if !visibleIndices.isEmpty { table.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false) }
    }
    @objc private func chooseRow() {
        guard visibleIndices.indices.contains(table.selectedRow) else { return }
        selectItem(at: visibleIndices[table.selectedRow])
        popover.close()
        if let action { _ = sendAction(action, to: target) }
    }
    func numberOfRows(in tableView: NSTableView) -> Int { visibleIndices.count }
    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        let label = titles[visibleIndices[row]]
        let stack = NSStackView()
        stack.spacing = 10
        let style = symbols[label] ?? ("network", .secondaryLabelColor)
        let icon = NSImageView(image: NSImage(systemSymbolName: style.0, accessibilityDescription: nil)!)
        icon.contentTintColor = style.1
        icon.widthAnchor.constraint(equalToConstant: 20).isActive = true
        stack.addArrangedSubview(icon)
        let text = NSTextField(labelWithString: label)
        text.font = .systemFont(ofSize: 13, weight: .medium)
        text.lineBreakMode = .byTruncatingTail
        stack.addArrangedSubview(text)
        return stack
    }
    func control(_ control: NSControl, textView: NSTextView, doCommandBy commandSelector: Selector) -> Bool {
        if commandSelector == #selector(NSResponder.moveDown(_:)) || commandSelector == #selector(NSResponder.moveUp(_:)) {
            let offset = commandSelector == #selector(NSResponder.moveDown(_:)) ? 1 : -1
            let next = max(0, min(visibleIndices.count - 1, table.selectedRow + offset))
            if !visibleIndices.isEmpty { table.selectRowIndexes(IndexSet(integer: next), byExtendingSelection: false); table.scrollRowToVisible(next) }
            return true
        }
        if commandSelector == #selector(NSResponder.insertNewline(_:)) { chooseRow(); return true }
        if commandSelector == #selector(NSResponder.cancelOperation(_:)) { popover.close(); return true }
        return false
    }
}

/// An endpoint: where added models run. OpenRouter by default; any OpenAI-compatible server otherwise.

enum KeychainCredentials {
    static func run(_ arguments: [String], input: Data? = nil, timeout: TimeInterval) throws -> Data {
        guard input == nil || input!.count <= 8192 else {
            throw SettingsError.message("The key is too long.")
        }
        let process = Process()
        let output = Pipe()
        let errors = Pipe()
        let stdin = Pipe()
        let finished = DispatchSemaphore(value: 0)
        process.executableURL = Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers/OpenRouterCredentialHelper")
        process.arguments = arguments
        process.standardOutput = output
        process.standardError = errors
        process.standardInput = stdin
        process.terminationHandler = { _ in finished.signal() }
        try process.run()
        if let input = input { stdin.fileHandleForWriting.write(input) }
        stdin.fileHandleForWriting.closeFile()
        if finished.wait(timeout: .now() + timeout) == .timedOut {
            process.terminate()
            if finished.wait(timeout: .now() + 0.5) == .timedOut {
                Darwin.kill(process.processIdentifier, SIGKILL)
            }
            throw SettingsError.message("Keychain access timed out. Open this app and authorize access to the saved key.")
        }
        let result = output.fileHandleForReading.readDataToEndOfFile()
        guard process.terminationStatus == 0 else {
            throw SettingsError.message("Keychain access failed. Unlock Keychain and authorize access in this app, or save the key again.")
        }
        return result
    }

    static func save(_ key: String, account: String) throws {
        _ = try run(["--save", account], input: Data(key.utf8), timeout: 60)
    }

    static func read(_ account: String) throws -> String {
        let data = try run(["--authorize-token", account], timeout: 60)
        guard let value = String(data: data, encoding: .utf8) else {
            throw SettingsError.message("The saved key could not be decoded.")
        }
        return value.trimmingCharacters(in: .newlines)
    }
}

// Compatibility entry points forward to the stable helper, which owns Keychain access.
if CommandLine.arguments.count == 2 && CommandLine.arguments[1] == "--self-test-keychain" {
    do {
        let result = try KeychainCredentials.run(["--self-test-keychain"], timeout: 15)
        FileHandle.standardOutput.write(result)
        exit(0)
    } catch {
        FileHandle.standardError.write(Data("Keychain self-test failed.\n".utf8))
        exit(1)
    }
}

if CommandLine.arguments.count == 3 && CommandLine.arguments[1] == "--token" {
    do {
        guard UUID(uuidString: CommandLine.arguments[2]) != nil else { exit(1) }
        let result = try KeychainCredentials.run(["--token", CommandLine.arguments[2]], timeout: 4)
        FileHandle.standardOutput.write(result)
        exit(0)
    } catch {
        FileHandle.standardError.write(Data("OpenRouter Keychain access failed. Open Model Deck to check your saved key.\n".utf8))
        exit(1)
    }
}

final class StudioCard: NSView {
    var accent: NSColor? = nil
    override func draw(_ dirtyRect: NSRect) {
        let rect = bounds.insetBy(dx: 0.5, dy: 0.5)
        let shape = NSBezierPath(roundedRect: rect, xRadius: 14, yRadius: 14)
        NSColor.controlBackgroundColor.withAlphaComponent(0.92).setFill()
        shape.fill()
        (accent ?? NSColor.separatorColor).withAlphaComponent(accent == nil ? 0.22 : 0.24).setStroke()
        shape.lineWidth = 1
        shape.stroke()
        if let accent {
            accent.withAlphaComponent(0.7).setFill()
            NSBezierPath(roundedRect: NSRect(x: 0, y: 20, width: 3, height: max(0, bounds.height - 40)), xRadius: 1.5, yRadius: 1.5).fill()
        }
    }
    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        needsDisplay = true
    }
}

final class StudioSymbolBadge: NSView {
    private let tint: NSColor
    init(symbol: String, color: NSColor) {
        tint = color
        super.init(frame: .zero)
        let image = NSImageView(image: NSImage(systemSymbolName: symbol, accessibilityDescription: nil) ?? NSImage())
        image.contentTintColor = color
        image.image = image.image?.withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: 17, weight: .semibold))
        image.translatesAutoresizingMaskIntoConstraints = false
        addSubview(image)
        NSLayoutConstraint.activate([
            widthAnchor.constraint(equalToConstant: 34), heightAnchor.constraint(equalToConstant: 34),
            image.centerXAnchor.constraint(equalTo: centerXAnchor), image.centerYAnchor.constraint(equalTo: centerYAnchor),
            image.widthAnchor.constraint(equalToConstant: 23), image.heightAnchor.constraint(equalToConstant: 23)
        ])
    }
    required init?(coder: NSCoder) { fatalError("Not used") }
    override func draw(_ dirtyRect: NSRect) {
        tint.withAlphaComponent(0.11).setFill()
        NSBezierPath(roundedRect: bounds, xRadius: 9, yRadius: 9).fill()
    }
    override func viewDidChangeEffectiveAppearance() { super.viewDidChangeEffectiveAppearance(); needsDisplay = true }
}

final class StudioPageDocument: NSView {
    override var isFlipped: Bool { true }
}

final class SidebarDetails: NSStackView {
    private let disclosure = NSButton()
    private let details: NSView
    private var compact = false

    init(title: String, content: NSView) {
        details = content
        super.init(frame: .zero)
        orientation = .vertical
        alignment = .leading
        spacing = 8
        disclosure.title = title
        disclosure.setButtonType(.onOff)
        disclosure.bezelStyle = .inline
        disclosure.isBordered = false
        disclosure.imagePosition = .imageLeading
        disclosure.image = NSImage(systemSymbolName: "chevron.right", accessibilityDescription: nil)
        disclosure.setAccessibilityLabel(title)
        disclosure.target = self
        disclosure.action = #selector(toggleDetails)
        disclosure.isHidden = true
        addArrangedSubview(disclosure)
        addArrangedSubview(content)
        content.widthAnchor.constraint(equalTo: widthAnchor).isActive = true
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    func setCompact(_ value: Bool) {
        guard compact != value else { return }
        compact = value
        disclosure.isHidden = !value
        disclosure.state = .off
        disclosure.image = NSImage(systemSymbolName: "chevron.right", accessibilityDescription: nil)
        details.isHidden = value
    }
    @objc private func toggleDetails() {
        details.isHidden = disclosure.state != .on
        disclosure.image = NSImage(systemSymbolName: details.isHidden ? "chevron.right" : "chevron.down", accessibilityDescription: nil)
    }
}

final class StudioNavigationButton: NSButton {
    private var hovering = false
    private var hoverTracking: NSTrackingArea?
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let hoverTracking { removeTrackingArea(hoverTracking) }
        let area = NSTrackingArea(rect: .zero, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect], owner: self)
        addTrackingArea(area)
        hoverTracking = area
    }
    override func mouseEntered(with event: NSEvent) { hovering = true; needsDisplay = true }
    override func mouseExited(with event: NSEvent) { hovering = false; needsDisplay = true }
    override func draw(_ dirtyRect: NSRect) {
        if state == .on || hovering {
            (state == .on ? NSColor.controlAccentColor.withAlphaComponent(0.12) : NSColor.labelColor.withAlphaComponent(0.045)).setFill()
            NSBezierPath(roundedRect: bounds.insetBy(dx: 1, dy: 1), xRadius: 10, yRadius: 10).fill()
        }
        super.draw(dirtyRect)
    }
    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        needsDisplay = true
    }
}

final class UsageOutputBuffer {
    private let lock = NSLock()
    private var bytes = Data()
    private var exceededLimit = false
    private let maximumBytes: Int

    init(maximumBytes: Int = 65536) { self.maximumBytes = maximumBytes }

    func append(_ incoming: Data) {
        lock.lock()
        defer { lock.unlock() }
        if bytes.count + incoming.count <= maximumBytes { bytes.append(incoming) }
        else { exceededLimit = true }
    }

    func snapshot() -> Data? {
        lock.lock()
        defer { lock.unlock() }
        return exceededLimit ? nil : bytes
    }
}

final class AllowanceBar: NSView {
    let remaining: Double
    init(remaining: Double) {
        self.remaining = min(100, max(0, remaining))
        super.init(frame: .zero)
        heightAnchor.constraint(equalToConstant: 9).isActive = true
        setAccessibilityElement(true)
        setAccessibilityRole(.progressIndicator)
        setAccessibilityLabel(String(format: "%.0f percent remaining", self.remaining))
    }
    required init?(coder: NSCoder) { fatalError("Not used") }
    override func draw(_ dirtyRect: NSRect) {
        NSColor.quaternaryLabelColor.setFill()
        NSBezierPath(roundedRect: bounds, xRadius: 4, yRadius: 4).fill()
        (remaining < 20 ? NSColor.systemOrange : NSColor.controlAccentColor).setFill()
        NSBezierPath(roundedRect: NSRect(x: 0, y: 0, width: bounds.width * CGFloat(remaining / 100), height: bounds.height), xRadius: 4, yRadius: 4).fill()
    }
    override func viewDidChangeEffectiveAppearance() { super.viewDidChangeEffectiveAppearance(); needsDisplay = true }
}

final class TokenActivityChart: NSView {
    let samples: [(date: String, tokens: Double?)]
    init(samples: [(date: String, tokens: Double?)]) {
        self.samples = samples
        super.init(frame: .zero)
        heightAnchor.constraint(equalToConstant: 185).isActive = true
        let description = samples.map { sample in
            sample.date + ": " + (sample.tokens.map { String(format: "%.0f tokens", $0) } ?? "unavailable")
        }.joined(separator: "; ")
        toolTip = description
        setAccessibilityElement(true)
        setAccessibilityRole(.image)
        setAccessibilityLabel("Daily token activity. " + description)
    }
    required init?(coder: NSCoder) { fatalError("Not used") }
    override func draw(_ dirtyRect: NSRect) {
        guard !samples.isEmpty else { return }
        let plot = NSRect(x: 62, y: 30, width: max(1, bounds.width - 70), height: 138)
        let maximum = max(1, samples.compactMap { $0.tokens }.max() ?? 1)
        let attributes: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 10), .foregroundColor: NSColor.secondaryLabelColor]
        let axis = NSBezierPath()
        axis.move(to: NSPoint(x: plot.minX, y: plot.maxY))
        axis.line(to: NSPoint(x: plot.minX, y: plot.minY))
        axis.line(to: NSPoint(x: plot.maxX, y: plot.minY))
        NSColor.separatorColor.setStroke(); axis.stroke()
        (String(format: "%.0f", maximum) as NSString).draw(at: NSPoint(x: 0, y: plot.maxY - 6), withAttributes: attributes)
        ("0" as NSString).draw(at: NSPoint(x: 44, y: plot.minY - 5), withAttributes: attributes)
        let step = plot.width / CGFloat(samples.count)
        for (index, sample) in samples.enumerated() {
            let x = plot.minX + CGFloat(index) * step + 4
            if let tokens = sample.tokens {
                if tokens > 0 {
                    NSColor.controlAccentColor.setFill()
                    NSBezierPath(roundedRect: NSRect(x: x, y: plot.minY, width: max(2, step - 8), height: plot.height * CGFloat(tokens / maximum)), xRadius: 3, yRadius: 3).fill()
                } else {
                    ("0" as NSString).draw(at: NSPoint(x: x, y: plot.minY + 3), withAttributes: attributes)
                }
            } else {
                ("—" as NSString).draw(at: NSPoint(x: x, y: plot.minY + 5), withAttributes: attributes)
            }
            // Keep date labels legible when the window is narrow; all values remain in the tooltip.
            let labelStride = max(1, Int(ceil(38 / step)))
            if index % labelStride == 0 {
                (String(sample.date.suffix(5)) as NSString).draw(at: NSPoint(x: x, y: 9), withAttributes: attributes)
            }
        }
    }
    override func viewDidChangeEffectiveAppearance() { super.viewDidChangeEffectiveAppearance(); needsDisplay = true }
}



func modelStudioBrandImage() -> NSImage {
    if let image = NSImage(named: "ModelStudio") { return image }
    if let url = Bundle.main.url(forResource: "ModelStudio", withExtension: "png"), let image = NSImage(contentsOf: url) { return image }
    return NSImage(systemSymbolName: "square.stack.3d.up.fill", accessibilityDescription: "Model Deck")!
}

final class CompanionPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

final class CompanionPanelController: NSObject {
    private let panel: CompanionPanel
    private let shell = NSVisualEffectView()
    private let pageName = NSTextField(labelWithString: "Overview")
    let contentContainer = StudioPageDocument()
    private let expandedSurface = NSView()
    private let permissionSurface = NSView()
    private let permissionMessage = NSTextField(wrappingLabelWithString: "")
    private let attachmentStatus = NSTextField(wrappingLabelWithString: "")
    private var expandedConstraints: [NSLayoutConstraint] = []
    private var permissionConstraints: [NSLayoutConstraint] = []
    private var expanded = true
    private var showingPermission = false
    private var railButtons: [NSButton] = []
    var openPage: ((Int) -> Void)?
    var openSettings: (() -> Void)?
    var hideRequested: (() -> Void)?
    var enableFollowing: (() -> Void)?
    var collapseChanged: (() -> Void)?
    var previewMode = false
    var desiredWidth: CGFloat { expanded ? 600 : 48 }

    override init() {
        panel = CompanionPanel(contentRect: NSRect(x: 40, y: 80, width: 600, height: 640),
                               styleMask: [.nonactivatingPanel, .borderless], backing: .buffered, defer: false)
        super.init()
        let surface = shell
        surface.material = .sidebar
        surface.state = .active
        surface.wantsLayer = true
        surface.layer?.cornerRadius = 20
        surface.layer?.masksToBounds = true
        surface.layer?.borderWidth = 0.5
        surface.layer?.borderColor = NSColor.separatorColor.cgColor
        let canvas = NSView()
        panel.contentView = canvas
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        surface.translatesAutoresizingMaskIntoConstraints = false
        canvas.addSubview(surface)
        NSLayoutConstraint.activate([
            surface.leadingAnchor.constraint(equalTo: canvas.leadingAnchor, constant: 4),
            surface.trailingAnchor.constraint(equalTo: canvas.trailingAnchor, constant: -4),
            surface.topAnchor.constraint(equalTo: canvas.topAnchor, constant: 4),
            surface.bottomAnchor.constraint(equalTo: canvas.bottomAnchor, constant: -4)
        ])
        expandedSurface.translatesAutoresizingMaskIntoConstraints = false
        expandedConstraints = [
            expandedSurface.leadingAnchor.constraint(equalTo: surface.leadingAnchor, constant: 40),
            expandedSurface.trailingAnchor.constraint(equalTo: surface.trailingAnchor),
            expandedSurface.topAnchor.constraint(equalTo: surface.topAnchor),
            expandedSurface.bottomAnchor.constraint(equalTo: surface.bottomAnchor)
        ]
        panel.title = "Model Deck attached sidebar"
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.hidesOnDeactivate = false
        panel.isMovable = false
        panel.isReleasedWhenClosed = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        contentContainer.translatesAutoresizingMaskIntoConstraints = false
        attachmentStatus.translatesAutoresizingMaskIntoConstraints = false
        attachmentStatus.font = .systemFont(ofSize: 11)
        attachmentStatus.textColor = .secondaryLabelColor
        let brand = NSTextField(labelWithString: "Model Deck")
        brand.font = .systemFont(ofSize: 15, weight: .semibold)
        pageName.font = .systemFont(ofSize: 12)
        pageName.textColor = .secondaryLabelColor
        let header = NSStackView(views: [brand, pageName, NSView(), iconButton("sidebar.right", label: "Collapse sidebar", action: #selector(toggleExpanded))])
        header.spacing = 10
        header.translatesAutoresizingMaskIntoConstraints = false
        expandedSurface.addSubview(header)
        let bodyScroll = NSScrollView()
        bodyScroll.translatesAutoresizingMaskIntoConstraints = false
        bodyScroll.drawsBackground = false
        bodyScroll.hasVerticalScroller = true
        bodyScroll.autohidesScrollers = true
        bodyScroll.documentView = contentContainer
        expandedSurface.addSubview(attachmentStatus)
        expandedSurface.addSubview(bodyScroll)
        let preferredHeight = contentContainer.heightAnchor.constraint(equalTo: bodyScroll.contentView.heightAnchor)
        preferredHeight.priority = .defaultHigh
        NSLayoutConstraint.activate([
            attachmentStatus.leadingAnchor.constraint(equalTo: expandedSurface.leadingAnchor, constant: 16),
            attachmentStatus.trailingAnchor.constraint(equalTo: expandedSurface.trailingAnchor, constant: -16),
            header.leadingAnchor.constraint(equalTo: expandedSurface.leadingAnchor, constant: 16),
            header.trailingAnchor.constraint(equalTo: expandedSurface.trailingAnchor, constant: -12),
            header.topAnchor.constraint(equalTo: expandedSurface.topAnchor, constant: 10),
            attachmentStatus.topAnchor.constraint(equalTo: header.bottomAnchor, constant: 4),
            bodyScroll.leadingAnchor.constraint(equalTo: expandedSurface.leadingAnchor),
            bodyScroll.trailingAnchor.constraint(equalTo: expandedSurface.trailingAnchor),
            bodyScroll.topAnchor.constraint(equalTo: attachmentStatus.bottomAnchor, constant: 6),
            bodyScroll.bottomAnchor.constraint(equalTo: expandedSurface.bottomAnchor),
            contentContainer.widthAnchor.constraint(equalTo: bodyScroll.contentView.widthAnchor),
            contentContainer.heightAnchor.constraint(greaterThanOrEqualToConstant: 360), preferredHeight
        ])
        let rail = NSStackView()
        rail.orientation = .vertical
        rail.spacing = 10
        rail.alignment = .centerX
        rail.translatesAutoresizingMaskIntoConstraints = false
        let railDocument = StudioPageDocument()
        railDocument.translatesAutoresizingMaskIntoConstraints = false
        railDocument.addSubview(rail)
        let railScroll = NSScrollView()
        railScroll.drawsBackground = false
        railScroll.hasVerticalScroller = false
        railScroll.translatesAutoresizingMaskIntoConstraints = false
        railScroll.documentView = railDocument
        surface.addSubview(railScroll)
        NSLayoutConstraint.activate([
            railScroll.leadingAnchor.constraint(equalTo: surface.leadingAnchor),
            railScroll.widthAnchor.constraint(equalToConstant: 40),
            railScroll.topAnchor.constraint(equalTo: surface.topAnchor),
            railScroll.bottomAnchor.constraint(equalTo: surface.bottomAnchor),
            railDocument.widthAnchor.constraint(equalTo: railScroll.contentView.widthAnchor),
            rail.leadingAnchor.constraint(equalTo: railDocument.leadingAnchor, constant: 3),
            rail.trailingAnchor.constraint(equalTo: railDocument.trailingAnchor, constant: -3),
            rail.topAnchor.constraint(equalTo: railDocument.topAnchor, constant: 12),
            rail.bottomAnchor.constraint(equalTo: railDocument.bottomAnchor, constant: -12)
        ])
        let mark = NSImageView(image: modelStudioBrandImage())
        mark.widthAnchor.constraint(equalToConstant: 30).isActive = true
        mark.heightAnchor.constraint(equalToConstant: 30).isActive = true
        rail.addArrangedSubview(mark)
        let toggle = iconButton("sidebar.right", label: "Collapse or expand sidebar", action: #selector(toggleExpanded))
        rail.addArrangedSubview(toggle)
        for (index, entry) in [("square.grid.2x2", "Overview"), ("square.stack.3d.up", "Models"),
                               ("network", "Endpoints"), ("chart.bar", "Usage"), ("slider.horizontal.3", "Advanced")].enumerated() {
            let button = iconButton(entry.0, label: entry.1, action: #selector(selectPage(_:)))
            button.tag = index
            rail.addArrangedSubview(button)
            railButtons.append(button)
        }
        let railSpacer = NSView()
        railSpacer.heightAnchor.constraint(greaterThanOrEqualToConstant: 24).isActive = true
        rail.addArrangedSubview(railSpacer)
        let railHeight = railDocument.heightAnchor.constraint(equalTo: railScroll.contentView.heightAnchor)
        railHeight.priority = .defaultLow
        railHeight.isActive = true
        rail.addArrangedSubview(iconButton("macwindow", label: "Full settings", action: #selector(showSettings)))
        rail.addArrangedSubview(iconButton("xmark", label: "Hide sidebar", action: #selector(hideSidebar)))
        permissionMessage.font = .systemFont(ofSize: 14)
        permissionMessage.translatesAutoresizingMaskIntoConstraints = false
        let enable = NSButton(title: "Enable window following", target: self, action: #selector(requestFollowing))
        enable.bezelStyle = .rounded
        enable.translatesAutoresizingMaskIntoConstraints = false
        permissionSurface.addSubview(permissionMessage)
        permissionSurface.addSubview(enable)
        NSLayoutConstraint.activate([
            permissionMessage.leadingAnchor.constraint(equalTo: permissionSurface.leadingAnchor, constant: 28),
            permissionMessage.trailingAnchor.constraint(equalTo: permissionSurface.trailingAnchor, constant: -28),
            permissionMessage.topAnchor.constraint(equalTo: permissionSurface.topAnchor, constant: 32),
            enable.leadingAnchor.constraint(equalTo: permissionMessage.leadingAnchor),
            enable.topAnchor.constraint(equalTo: permissionMessage.bottomAnchor, constant: 20)
        ])
        mountExpandedSurface()
    }

    private func iconButton(_ symbol: String, label: String, action: Selector) -> NSButton {
        let button = StudioNavigationButton(image: NSImage(systemSymbolName: symbol, accessibilityDescription: label)!, target: self, action: action)
        button.bezelStyle = .rounded
        button.isBordered = false
        button.toolTip = label
        button.contentTintColor = .secondaryLabelColor
        button.setAccessibilityLabel(label)
        button.widthAnchor.constraint(equalToConstant: 34).isActive = true
        button.heightAnchor.constraint(equalToConstant: 32).isActive = true
        return button
    }

    private func mountExpandedSurface() {
        let surface = shell
        NSLayoutConstraint.deactivate(expandedConstraints + permissionConstraints)
        expandedSurface.removeFromSuperview()
        permissionSurface.removeFromSuperview()
        if showingPermission {
            permissionSurface.translatesAutoresizingMaskIntoConstraints = false
            surface.addSubview(permissionSurface)
            permissionConstraints = [
                permissionSurface.leadingAnchor.constraint(equalTo: surface.leadingAnchor, constant: 40),
                permissionSurface.trailingAnchor.constraint(equalTo: surface.trailingAnchor),
                permissionSurface.topAnchor.constraint(equalTo: surface.topAnchor),
                permissionSurface.bottomAnchor.constraint(equalTo: surface.bottomAnchor)
            ]
            NSLayoutConstraint.activate(permissionConstraints)
        } else if expanded {
            surface.addSubview(expandedSurface)
            NSLayoutConstraint.activate(expandedConstraints)
        }
        surface.layoutSubtreeIfNeeded()
    }

    func showPermission(_ message: String) {
        if showingPermission && permissionMessage.stringValue == message { panel.orderFrontRegardless(); return }
        showingPermission = true
        expanded = true
        permissionMessage.stringValue = message
        mountExpandedSurface()
        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1060, height: 820)
        panel.setFrame(NSRect(x: screen.midX - 300, y: screen.midY - 180, width: 600, height: 360), display: true)
        panel.makeKeyAndOrderFront(nil)
    }

    func attach(host: NSRect, screen: NSRect, restartRequired: Bool, reservedWidth: CGFloat) {
        if showingPermission { showingPermission = false; mountExpandedSurface() }
        attachmentStatus.stringValue = (previewMode ? "FAKE-WINDOW PREVIEW · " : "") +
            "Attached to your workspace" +
            (restartRequired ? " Models changed; manual relaunch required." : "")
        panel.setFrame(NSRect(x: host.maxX, y: host.minY, width: reservedWidth, height: host.height), display: true)
        panel.orderFrontRegardless()
    }

    func setSelectedPage(_ index: Int) {
        pageName.stringValue = ["Overview", "Models", "Endpoints", "Usage", "Advanced"][max(0, min(4, index))]
        for (position, button) in railButtons.enumerated() {
            button.state = position == index ? .on : .off
            button.contentTintColor = position == index ? .controlAccentColor : .secondaryLabelColor
            button.needsDisplay = true
        }
    }
    func hide() { panel.orderOut(nil) }
    func renderPreview(to path: String, collapsed: Bool) {
        guard previewMode else { return }
        if collapsed && expanded { toggleExpanded() }
        DispatchQueue.main.async {
            guard let content = self.panel.contentView else { exit(1) }
            content.layoutSubtreeIfNeeded()
            guard let bitmap = content.bitmapImageRepForCachingDisplay(in: content.bounds) else { exit(1) }
            content.cacheDisplay(in: content.bounds, to: bitmap)
            guard let png = bitmap.representation(using: .png, properties: [:]) else { exit(1) }
            do { try png.write(to: URL(fileURLWithPath: path)); exit(0) }
            catch { exit(1) }
        }
    }
    @objc private func toggleExpanded() {
        guard !showingPermission else { return }
        expanded.toggle()
        mountExpandedSurface()
        collapseChanged?()
    }
    @objc private func selectPage(_ sender: NSButton) {
        if !expanded && !showingPermission { expanded = true; mountExpandedSurface(); collapseChanged?() }
        openPage?(sender.tag)
    }
    @objc private func showSettings() { openSettings?() }
    @objc private func hideSidebar() { hideRequested?() }
    @objc private func requestFollowing() { enableFollowing?() }
}

@MainActor
final class OpenRouterSettingsApp: NSObject, NSApplicationDelegate, NSWindowDelegate, NSTableViewDataSource, NSTableViewDelegate {
    private var window: NSWindow!
    private let accounts = SearchablePicker()
    private let catalogAccounts = SearchablePicker()
    private let accountName = NSTextField()
    private let apiKey = NSSecureTextField()
    private let endpointURL = NSTextField()
    private let endpointFormat = NSPopUpButton()
    private let endpointKind = SearchablePicker()
    private let providerPresets = ProviderPreset.bundledFromMainBundle()
    private let endpointHint = NSTextField(wrappingLabelWithString: "")
    private let cursorSDKStatus = NSTextField(wrappingLabelWithString: "Check the SDK to see whether Cursor is ready on this Mac.")
    private var cursorSetupCard: NSView!
    private var endpointFormatRow: NSView!
    private var libraryEmptyState: NSView!
    private var selectedPage = 0
    private var compactLayout = false
    private var modelListHeight: NSLayoutConstraint?
    private let endpointSummary = NSTextField(wrappingLabelWithString: "Add an endpoint below.")
    private let model = NSTextField()
    private let catalogSearch = NSSearchField()
    private let catalogTable = ModelSelectionTable()
    private let catalogStatus = NSTextField(wrappingLabelWithString: "Choose a connection to see its models.")
    private let catalogSelectionSummary = NSTextField(labelWithString: "No models selected")
    private let catalogProgress = NSProgressIndicator()
    private var catalogAddButton: NSButton!
    private var catalogRetryButton: NSButton!
    private var catalogAuthorizeButton: NSButton!
    private var catalogEmptyState: NSView!
    private var catalogListHeight: NSLayoutConstraint?
    private var catalogState = ModelBrowserState()
    private let modelCatalogPresenter = ModelCatalogPresenter()
    private lazy var modelCatalogService: ModelCatalogServing = {
        ModelCatalogBootstrap.makeService(
            legacyRequestHandler: { [weak self] request, completion in
                guard let self else {
                    completion(["ok": false, "error": "Settings unavailable."])
                    return
                }
                self.configure(request, blocksUI: false, completion: completion)
            },
            engineTransportFactory: { descriptor in
                UnixSocketEngineTransport(socketPath: descriptor.socketPath)
            }
        )
    }()
    private var catalogLoading = false
    private var registrationInProgress = false
    private var catalogFailures: [String: String] = [:]
    private var catalogAdded = Set<String>()
    private let favorites = NSPopUpButton()
    private let effort = NSPopUpButton()
    private let status = NSTextField(wrappingLabelWithString: "Loading Codex settings…")
    private let feedback = NSTextField(wrappingLabelWithString: "Ready.")
    private let activeKeySummary = NSTextField(wrappingLabelWithString: "No endpoint selected")
    private let usageDashboard = UsageDashboardView()
    private var usageSubscription: [String: Any] = [:]
    private var usageOpenRouter: [String: Any] = [:]
    private var usageOpenRouterAccountID = ""
    private var usageSelectedOpenRouterAccountID = ""
    private var usageEntries: [[String: Any]] = []
    private var usageRefreshedAt: Date?
    private var usageRefreshMessage: String?
    private let modelLibraryTable = NSTableView()
    private let modelSearch = NSSearchField()
    private let modelProviderFilter = NSPopUpButton()
    private let modelDetailTitle = NSTextField(wrappingLabelWithString: "Select a model to preview its connection")
    private let modelDetailDescription = NSTextField(wrappingLabelWithString: "Selection here previews a model. Choose it inside Codex to start using it.")
    private var modelCopyButton: NSButton!
    private var modelRemoveButton = NSButton()
    private let displayNameField = NSTextField()
    private var displayNameRow: NSStackView!
    private var addModelForm: NSView!
    private var modelLibraryBody: NSView!
    private var addModelsActionRow: NSView!
    private let registeredModelsSummary = NSTextField(wrappingLabelWithString: "Model inventory has not been loaded.")
    private var registeredModelRecords: [[String: Any]] = []
    private var visibleModelRecords: [[String: Any]] = []
    private var modelsInventoryLoaded = false
    private var reasoningDetails: NSView!
    private var studioPages: [NSView] = []
    private var sidebarPresentation = false
    private var presentationHeadings: [NSTextField] = []
    private var presentationCards: [(NSStackView, [NSLayoutConstraint])] = []
    private var sidebarDetails: [SidebarDetails] = []
    private var sidebarStackedRows: [(NSStackView, [NSLayoutConstraint])] = []
    private var standaloneOnlyViews: [NSView] = []
    private var overviewHero: NSTextField?
    private var footerMinimumHeight: NSLayoutConstraint?
    private var navigationButtons: [NSButton] = []
    private var actionButtons: [NSButton] = []
    private var preferences = SavedPreferences()
    private var busy = false
    private var usagePollTimer: Timer?
    private var usageRefreshInFlight = false
    private let usagePollInterval: TimeInterval = 10
    private var restoreAvailable = false
    private var restoreButton: NSButton!
    private var companion: CompanionPanelController?
    private var companionObservers: [NSObjectProtocol] = []
    private var companionLaunchRequested = false
    private let sharedBody = NSView()
    private let standaloneBodyHost = NSView()
    private var sharedBodyConstraints: [NSLayoutConstraint] = []
    private var standaloneSettingsVisible = false
    private let attachedPreview = CommandLine.arguments.contains("--preview-attached-companion") || CommandLine.arguments.contains("--render-attached-preview")
    private let renderingPreview = CommandLine.arguments.contains("--render-preview")
    private var trackingTimer: Timer?
    private let trackingQueue = DispatchQueue(label: "com.cooper.model-studio.window-geometry")
    private var hostTracker: HostWindowTracker?
    private var trackingInFlight = false
    private var trackedHostPID: pid_t?
    private let trackingGeneration = TrackingGeneration()
    private var pendingExplicitReservation = false
    private var attachedTrackingEnabled: Bool { UserDefaults.standard.bool(forKey: "attachedTrackingEnabled") }
    private var companionEnabled: Bool { UserDefaults.standard.bool(forKey: "companionEnabled") }
    private var companionRestartRequired: Bool {
        get { UserDefaults.standard.bool(forKey: "companionRestartRequired") }
        set { UserDefaults.standard.set(newValue, forKey: "companionRestartRequired") }
    }
    /// Model Deck's state folder. The app was first named "Codex OpenRouter"; that folder is renamed once.
    private static let stateDirectory: URL = {
        let support = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support", isDirectory: true)
        let current = support.appendingPathComponent("Model Deck", isDirectory: true)
        let legacy = support.appendingPathComponent("Codex OpenRouter", isDirectory: true)
        if !FileManager.default.fileExists(atPath: current.path), FileManager.default.fileExists(atPath: legacy.path) {
            try? FileManager.default.moveItem(at: legacy, to: current)
        }
        return current
    }()
    private var stateDirectory: URL { Self.stateDirectory }
    private var preferencesURL: URL { stateDirectory.appendingPathComponent("preferences.json") }

    /// Settings saved under the app's previous bundle identifier carry over once.
    private func migrateLegacyDefaults() {
        let current = UserDefaults.standard
        guard current.object(forKey: "companionEnabled") == nil,
              let legacy = UserDefaults(suiteName: "com.cooper.codex-openrouter") else { return }
        for key in ["companionEnabled", "attachedTrackingEnabled", "companionRestartRequired", "NSWindow Frame ModelStudioWindow"] {
            if let value = legacy.object(forKey: key) { current.set(value, forKey: key) }
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        if !attachedPreview && !renderingPreview { migrateLegacyDefaults() }
        if CommandLine.arguments.contains("--render-attached-preview") {
            guard CommandLine.arguments.count >= 6, let page = Int(CommandLine.arguments[3]), (0...4).contains(page),
                  ["light", "dark"].contains(CommandLine.arguments[4]), ["expanded", "collapsed"].contains(CommandLine.arguments[5]) else { exit(2) }
            NSApp.appearance = NSAppearance(named: CommandLine.arguments[4] == "dark" ? .darkAqua : .aqua)
        }
        if renderingPreview && CommandLine.arguments.count >= 5 {
            // Apply before any view exists so dynamic colors resolve for the requested appearance.
            NSApp.appearance = NSAppearance(named: CommandLine.arguments[4] == "dark" ? .darkAqua : .aqua)
        }
        if let iconURL = Bundle.main.url(forResource: "ModelStudio", withExtension: "icns"), let icon = NSImage(contentsOf: iconURL) {
            NSApp.applicationIconImage = icon
        }
        do {
            if !attachedPreview && !renderingPreview && FileManager.default.fileExists(atPath: preferencesURL.path) {
                preferences = try JSONDecoder().decode(SavedPreferences.self, from: Data(contentsOf: preferencesURL))
            }
        } catch { feedback.stringValue = "Saved preferences could not be loaded. Your Keychain keys and Codex settings have not been changed." }
        buildMenu()
        buildWindow()
        reloadAccounts()
        reloadFavorites()
        model.stringValue = preferences.selectedModel
        if renderingPreview || attachedPreview {
            installBrowserPreview()
            if CommandLine.arguments.count > 3 && CommandLine.arguments[3] == "3" { installUsagePreview() }
        }
        if attachedPreview {
            setBusy(true)
            status.stringValue = "Preview only — no settings or provider requests"
            feedback.stringValue = "FAKE-WINDOW PREVIEW. Navigation and collapse work; settings actions are disabled. Hide sidebar exits."
            createCompanionIfNeeded()
            refreshPreviewCompanion()
            if CommandLine.arguments.contains("--render-attached-preview") {
                showStudioPage(Int(CommandLine.arguments[3])!)
                companion?.renderPreview(to: CommandLine.arguments[2], collapsed: CommandLine.arguments[5] == "collapsed")
            }
            return
        }
        if CommandLine.arguments.count >= 3 && CommandLine.arguments[1] == "--render-preview" {
            // Development-only: --render-preview <png> [page 0-4] [light|dark]
            if CommandLine.arguments.contains("--compact") { window.setContentSize(NSSize(width: 720, height: 620)); applyResponsiveLayout(true) }
            if CommandLine.arguments.count >= 4, let page = Int(CommandLine.arguments[3]), (0...4).contains(page) {
                showStudioPage(page)
            }
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            // Development-only snapshot of our own NSView tree, without reading the desktop.
            DispatchQueue.main.async {
                let content = self.window.contentView!
                content.layoutSubtreeIfNeeded()
                guard let bitmap = content.bitmapImageRepForCachingDisplay(in: content.bounds) else { exit(1) }
                content.effectiveAppearance.performAsCurrentDrawingAppearance {
                    // The window paints its own background; the snapshot must add it or dark text vanishes.
                    if let context = NSGraphicsContext(bitmapImageRep: bitmap) {
                        NSGraphicsContext.saveGraphicsState()
                        NSGraphicsContext.current = context
                        NSColor.windowBackgroundColor.setFill()
                        content.bounds.fill()
                        NSGraphicsContext.restoreGraphicsState()
                    }
                    content.cacheDisplay(in: content.bounds, to: bitmap)
                }
                guard let png = bitmap.representation(using: .png, properties: [:]) else { exit(1) }
                do {
                    try png.write(to: URL(fileURLWithPath: CommandLine.arguments[2]))
                    print("Rendered app content at \(Int(content.bounds.width)) × \(Int(content.bounds.height)).")
                    NSApp.terminate(nil)
                } catch { exit(1) }
            }
            return
        }
        installCompanionObservers()
        if companionEnabled {
            createCompanionIfNeeded()
            refreshCompanionVisibility()
        } else { openFullSettings() }
        refreshStatus()
        // Pre-load usage so the page is already filled in when it is opened.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in self?.performUsageRefresh(quiet: true) }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { attachedPreview ? false : !companionEnabled }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openFullSettings()
        return true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !attachedPreview, let tracker = hostTracker else { return .terminateNow }
        trackingTimer?.invalidate()
        let generation = trackingGeneration.advance()
        let primaryTop = NSScreen.screens.first?.frame.maxY ?? 0
        var replied = false
        let reply = {
            guard !replied else { return }
            replied = true
            sender.reply(toApplicationShouldTerminate: true)
        }
        trackingQueue.async {
            tracker.release(primaryTop: primaryTop, allowed: { self.trackingGeneration.matches(generation) })
            DispatchQueue.main.async { reply() }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
            _ = self.trackingGeneration.advance()
            reply()
        }
        return .terminateLater
    }

    func applicationWillTerminate(_ notification: Notification) {
        trackingTimer?.invalidate()
        for token in companionObservers {
            NSWorkspace.shared.notificationCenter.removeObserver(token)
            NotificationCenter.default.removeObserver(token)
        }
    }

    func windowWillClose(_ notification: Notification) {
        guard notification.object as? NSWindow === window else { return }
        standaloneSettingsVisible = false
        if attachedPreview { refreshPreviewCompanion() }
        else { refreshCompanionVisibility() }
    }

    private func installCompanionObservers() {
        for name in [NSWorkspace.didActivateApplicationNotification, NSWorkspace.didLaunchApplicationNotification, NSWorkspace.didTerminateApplicationNotification] {
            companionObservers.append(NSWorkspace.shared.notificationCenter.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                self?.refreshCompanionVisibility()
            })
        }
        companionObservers.append(NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main) { [weak self] _ in
            self?.refreshCompanionVisibility()
        })
    }

    private func createCompanionIfNeeded() {
        guard companion == nil else { return }
        let controller = CompanionPanelController()
        controller.previewMode = attachedPreview
        controller.openPage = { [weak self] page in self?.showStudioPage(page) }
        controller.openSettings = { [weak self] in self?.openFullSettings() }
        controller.enableFollowing = { [weak self] in self?.enableWindowFollowing() }
        controller.collapseChanged = { [weak self] in
            guard let self = self else { return }
            if self.attachedPreview { self.refreshPreviewCompanion() }
            else {
                self.pendingExplicitReservation = true
                _ = self.trackingGeneration.advance()
                self.refreshCompanionVisibility()
            }
        }
        controller.hideRequested = { [weak self] in
            guard let self = self else { return }
            if self.attachedPreview { NSApp.terminate(nil); return }
            UserDefaults.standard.set(false, forKey: "companionEnabled")
            UserDefaults.standard.set(false, forKey: "attachedTrackingEnabled")
            self.trackingTimer?.invalidate()
            self.trackingTimer = nil
            self.releaseReservedSpace()
            self.companion?.hide()
        }
        companion = controller
    }

    private func releaseReservedSpace() {
        let generation = trackingGeneration.advance()
        guard let tracker = hostTracker else { return }
        let primaryTop = NSScreen.screens.first?.frame.maxY ?? 0
        trackingQueue.async {
            tracker.release(primaryTop: primaryTop, allowed: { self.trackingGeneration.matches(generation) })
        }
        trackedHostPID = nil
    }

    @objc private func enableWindowFollowing() {
        guard !attachedPreview else { return }
        // This is the only path that opts in or asks macOS to display its permission prompt.
        UserDefaults.standard.set(true, forKey: "attachedTrackingEnabled")
        UserDefaults.standard.set(true, forKey: "companionEnabled")
        standaloneSettingsVisible = false
        pendingExplicitReservation = true
        _ = trackingGeneration.advance()
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(options)
        refreshCompanionVisibility()
    }

    private func refreshCompanionVisibility() {
        guard !attachedPreview else { refreshPreviewCompanion(); return }
        guard companionEnabled, !standaloneSettingsVisible else { companion?.hide(); return }
        createCompanionIfNeeded()
        guard attachedTrackingEnabled else {
            companion?.showPermission("Enable window management to attach the full Model Deck sidebar.\n\nmacOS Accessibility permission is broad. This app reads only window focus, role, position, size, and minimized/hidden state. It changes only window position and size to reserve sidebar space, expand it, and return space when collapsed or disabled. It never reads chat text, window titles, children, or screenshots.\n\nYou grant permission yourself. App updates may require authorization again.")
            return
        }
        if trackingTimer == nil {
            trackingTimer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in self?.refreshCompanionVisibility() }
        }
        let active = NSWorkspace.shared.frontmostApplication
        let ownBundle = Bundle.main.bundleIdentifier ?? "com.cooper.model-deck"
        guard active?.bundleIdentifier == "com.openai.codex" || active?.bundleIdentifier == ownBundle else {
            if trackedHostPID != nil { _ = trackingGeneration.advance(); trackedHostPID = nil }
            companion?.hide()
            return
        }
        guard AXIsProcessTrusted() else {
            companion?.showPermission("Window management needs your Accessibility approval in System Settings.\n\nThe macOS grant is broad; this app uses only window geometry/focus/state and position/size changes. No chat text, titles, children, or screenshots are read. Click Enable window following to request permission, then return to ChatGPT/Codex.")
            return
        }
        let hostPID: pid_t
        if active?.bundleIdentifier == "com.openai.codex", let pid = active?.processIdentifier { hostPID = pid }
        else if let pid = trackedHostPID { hostPID = pid }
        else {
            companion?.showPermission("Permission is available. Focus a normal ChatGPT/Codex window to attach the sidebar and reserve space. No window will be resized until it is selected this way.")
            return
        }
        guard let host = NSRunningApplication(processIdentifier: hostPID), !host.isTerminated, !host.isHidden else {
            _ = trackingGeneration.advance()
            trackedHostPID = nil
            companion?.hide()
            return
        }
        if trackedHostPID != hostPID {
            _ = trackingGeneration.advance()
            trackedHostPID = hostPID
        }
        guard !trackingInFlight else { return }
        if hostTracker == nil { hostTracker = HostWindowTracker() }
        guard let tracker = hostTracker else { return }
        trackingInFlight = true
        let generation = trackingGeneration.advance()
        let primaryTop = NSScreen.screens.first?.frame.maxY ?? 0
        let width = companion?.desiredWidth ?? 600
        let explicit = pendingExplicitReservation
        pendingExplicitReservation = false
        let refreshFocus = active?.bundleIdentifier == "com.openai.codex"
        trackingQueue.async {
            let update = tracker.update(pid: hostPID, refreshFocus: refreshFocus, primaryTop: primaryTop,
                desiredWidth: width, explicitResize: explicit, allowed: { self.trackingGeneration.matches(generation) })
            DispatchQueue.main.async {
                self.trackingInFlight = false
                guard self.trackingGeneration.matches(generation), self.companionEnabled,
                      self.attachedTrackingEnabled, !self.standaloneSettingsVisible else { return }
                if let error = update.error {
                    self.companion?.showPermission(error + "\n\nRetry is explicit; polling will not repeatedly resize this window.")
                    return
                }
                guard let frame = update.frame else { self.companion?.hide(); return }
                self.mountSharedBody(in: self.companion!.contentContainer)
                self.window.orderOut(nil)
                let screen = NSScreen.screens.first { $0.frame.intersects(frame) }?.visibleFrame ?? frame
                self.companion?.attach(host: frame, screen: screen, restartRequired: self.companionRestartRequired, reservedWidth: update.reservedWidth)
            }
        }
    }

    private func refreshPreviewCompanion() {
        guard attachedPreview, !standaloneSettingsVisible else { companion?.hide(); return }
        createCompanionIfNeeded()
        mountSharedBody(in: companion!.contentContainer)
        window.orderOut(nil)
        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let outer = NSRect(x: screen.minX + 20, y: screen.minY + 30, width: min(1280, screen.width - 40), height: min(740, screen.height - 60))
        let width = companion!.desiredWidth
        let fakeHost = NSRect(x: outer.minX, y: outer.minY, width: max(1, outer.width - width), height: outer.height)
        companion?.attach(host: fakeHost, screen: screen, restartRequired: false, reservedWidth: width)
    }

    private func mountSharedBody(in destination: NSView) {
        setSidebarPresentation(destination !== standaloneBodyHost)
        guard sharedBody.superview !== destination else { return }
        NSLayoutConstraint.deactivate(sharedBodyConstraints)
        sharedBody.removeFromSuperview()
        destination.addSubview(sharedBody)
        sharedBody.translatesAutoresizingMaskIntoConstraints = false
        sharedBodyConstraints = [
            sharedBody.leadingAnchor.constraint(equalTo: destination.leadingAnchor),
            sharedBody.trailingAnchor.constraint(equalTo: destination.trailingAnchor),
            sharedBody.topAnchor.constraint(equalTo: destination.topAnchor),
            sharedBody.bottomAnchor.constraint(equalTo: destination.bottomAnchor)
        ]
        NSLayoutConstraint.activate(sharedBodyConstraints)
    }

    private func setSidebarPresentation(_ compact: Bool) {
        sidebarPresentation = compact
        applyResponsiveLayout(compact || (window?.contentView?.bounds.width ?? 1000) < 900)
    }

    func windowDidResize(_ notification: Notification) {
        guard !sidebarPresentation else { return }
        applyResponsiveLayout((window.contentView?.bounds.width ?? 1000) < 900)
    }

    private func applyResponsiveLayout(_ compact: Bool) {
        compactLayout = compact
        for heading in presentationHeadings { heading.font = .systemFont(ofSize: compact ? 24 : 28, weight: .bold) }
        for (content, padding) in presentationCards {
            content.spacing = compact ? 12 : 16
            for constraint in padding {
                let horizontal = constraint.firstAttribute == .leading || constraint.firstAttribute == .trailing
                constraint.constant = (constraint.constant < 0 ? -1 : 1) * (compact ? 16 : (horizontal ? 24 : 22))
            }
        }
        for (stack, widths) in sidebarStackedRows {
            NSLayoutConstraint.deactivate(widths)
            stack.orientation = compact ? .vertical : .horizontal
            stack.alignment = compact ? .leading : .centerY
            if compact { NSLayoutConstraint.activate(widths) }
        }
        for view in standaloneOnlyViews { view.isHidden = sidebarPresentation }
        for details in sidebarDetails { details.setCompact(compact) }
        footerMinimumHeight?.constant = compact ? 26 : 30
        modelListHeight?.constant = compact ? 244 : 292
        catalogListHeight?.constant = compact ? 244 : 300
    }

    private func adaptiveRow(_ views: [NSView]) -> NSStackView {
        let stack = row(views)
        sidebarStackedRows.append((stack, views.map { $0.widthAnchor.constraint(equalTo: stack.widthAnchor) }))
        return stack
    }

    private func sidebarDisclosure(_ label: String, content: NSView) -> NSView {
        let details = SidebarDetails(title: label, content: content)
        sidebarDetails.append(details)
        return details
    }

    @objc private func showCompanion() {
        if renderingPreview { return }
        standaloneSettingsVisible = false
        if attachedPreview { refreshPreviewCompanion(); return }
        UserDefaults.standard.set(true, forKey: "companionEnabled")
        pendingExplicitReservation = true
        createCompanionIfNeeded()
        refreshCompanionVisibility()
    }

    @objc private func openFullSettings() {
        standaloneSettingsVisible = true
        if !attachedPreview && hostTracker != nil { releaseReservedSpace() }
        companion?.hide()
        mountSharedBody(in: standaloneBodyHost)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func buildMenu() {
        let menu = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Quit Model Deck", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        menu.addItem(appItem)
        let editItem = NSMenuItem()
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        menu.addItem(editItem)
        let viewItem = NSMenuItem()
        viewItem.title = "View"
        let viewMenu = NSMenu(title: "View")
        let showCompanionItem = NSMenuItem(title: "Show companion", action: #selector(showCompanion), keyEquivalent: "")
        showCompanionItem.target = self
        viewMenu.addItem(showCompanionItem)
        let fullSettingsItem = NSMenuItem(title: "Full settings", action: #selector(openFullSettings), keyEquivalent: ",")
        fullSettingsItem.target = self
        viewMenu.addItem(fullSettingsItem)
        viewItem.submenu = viewMenu
        menu.addItem(viewItem)
        NSApp.mainMenu = menu
    }

    private func title(_ text: String, size: CGFloat = 15) -> NSTextField {
        let label = NSTextField(wrappingLabelWithString: text)
        label.font = .systemFont(ofSize: size, weight: .semibold)
        return label
    }

    private func note(_ text: String) -> NSTextField {
        let label = NSTextField(wrappingLabelWithString: text)
        label.font = .systemFont(ofSize: 13)
        label.textColor = .secondaryLabelColor
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return label
    }

    /// A one-line explanation with a bold lead-in: "GPT models  your ChatGPT subscription".
    private func lead(_ lead: String, _ text: String) -> NSTextField {
        let label = NSTextField(wrappingLabelWithString: "")
        let line = NSMutableAttributedString(string: lead + "  ", attributes: [
            .font: NSFont.systemFont(ofSize: 13, weight: .semibold), .foregroundColor: NSColor.labelColor])
        line.append(NSAttributedString(string: text, attributes: [
            .font: NSFont.systemFont(ofSize: 13), .foregroundColor: NSColor.secondaryLabelColor]))
        label.attributedStringValue = line
        return label
    }

    private func button(_ text: String, _ action: Selector) -> NSButton {
        let button = NSButton(title: text, target: self, action: action)
        button.bezelStyle = .rounded
        button.font = .systemFont(ofSize: 13, weight: .medium)
        button.controlSize = .large
        actionButtons.append(button)
        return button
    }

    private func row(_ views: [NSView]) -> NSStackView {
        let stack = NSStackView(views: views)
        stack.orientation = .horizontal
        stack.spacing = 10
        stack.alignment = .centerY
        return stack
    }

    private func column(_ views: [NSView], spacing: CGFloat = 14) -> NSStackView {
        let stack = NSStackView(views: views)
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = spacing
        for view in views {
            view.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        }
        return stack
    }

    private func card(_ views: [NSView]) -> NSView {
        let background = StudioCard()
        let content = column(views)
        content.translatesAutoresizingMaskIntoConstraints = false
        background.addSubview(content)
        let padding = [
            content.leadingAnchor.constraint(equalTo: background.leadingAnchor, constant: 18),
            content.trailingAnchor.constraint(equalTo: background.trailingAnchor, constant: -18),
            content.topAnchor.constraint(equalTo: background.topAnchor, constant: 16),
            content.bottomAnchor.constraint(equalTo: background.bottomAnchor, constant: -16)
        ]
        NSLayoutConstraint.activate(padding)
        presentationCards.append((content, padding))
        return background
    }

    private func pageHeading(_ heading: String, subtitle: String) -> NSView {
        let headingLabel = title(heading, size: 28)
        headingLabel.font = .systemFont(ofSize: 28, weight: .bold)
        presentationHeadings.append(headingLabel)
        return column([headingLabel, note(subtitle)], spacing: 9)
    }

    private func primaryButton(_ text: String, action: Selector, symbol: String) -> NSButton {
        let control = button(text, action)
        control.controlSize = .large
        control.bezelColor = StudioPalette.indigo
        control.image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)
        control.imagePosition = .imageLeading
        return control
    }

    private func providerLine(_ name: String, subtitle: String, symbol: String, color: NSColor) -> NSView {
        let icon = StudioSymbolBadge(symbol: symbol, color: color)
        return row([icon, column([title(name, size: 14), note(subtitle)], spacing: 4)])
    }

    private func buildOverviewPage() -> NSView {
        let launch = primaryButton("Launch Codex", action: #selector(launchIntegratedCodex), symbol: "arrow.up.right")
        status.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
        status.textColor = .secondaryLabelColor
        let hero = title("Your models.\nOne workspace.", size: 31)
        hero.font = .systemFont(ofSize: 31, weight: .bold)
        overviewHero = hero
        let launchCard = card([
            hero,
            note("Bring your subscription, provider keys, and local models into Codex. Every agent uses the model you choose."),
            adaptiveRow([launch, button("Open companion", #selector(showCompanion))]),
            note("Already in ChatGPT? Quit it when you’re ready, then launch here to connect Model Deck.")
        ])
        (launchCard as? StudioCard)?.accent = .controlAccentColor
        standaloneOnlyViews.append(launchCard)
        return column([
            pageHeading("Your workspace", subtitle: "A little more choice. Right beside Codex."),
            launchCard,
            card([
                title("Connected your way", size: 16),
                providerLine("ChatGPT", subtitle: "Native GPT models use your subscription.", symbol: "sparkles", color: .systemTeal),
                providerLine("OpenRouter", subtitle: "Models you add use your OpenRouter credits.", symbol: "arrow.triangle.branch", color: .systemIndigo),
                providerLine("Cursor", subtitle: "Composer and Auto use your Cursor account through the SDK. Plan charges may apply.", symbol: "cursorarrow", color: .labelColor),
                providerLine("More providers", subtitle: "Kimi, Z.AI, MiniMax, Gemini, DeepSeek, and local models.", symbol: "square.stack.3d.up", color: .systemTeal),
                row([button("Manage connections", #selector(showKeysPage)), NSView()])
            ]),
            sidebarDisclosure("Saved Codex defaults", content: card([
                title("Saved Codex defaults", size: 15), status,
                note("Read from your configuration. A running task may use a different model.")
            ]))
        ], spacing: 20)
    }

    private func buildModelsPage() -> NSView {
        model.placeholderString = "Exact model ID"
        model.setContentHuggingPriority(.defaultLow, for: .horizontal)
        effort.addItems(withTitles: ["Default (low)", "low", "medium", "high", "xhigh"])
        activeKeySummary.font = .systemFont(ofSize: 13)
        activeKeySummary.textColor = .secondaryLabelColor
        registeredModelsSummary.font = .systemFont(ofSize: 12)
        registeredModelsSummary.textColor = .secondaryLabelColor
        modelSearch.placeholderString = "Search models"
        modelSearch.target = self
        modelSearch.action = #selector(filterModelLibrary)
        modelSearch.sendsSearchStringImmediately = true
        modelSearch.setContentHuggingPriority(.defaultLow, for: .horizontal)
        modelProviderFilter.addItems(withTitles: ["All models", "ChatGPT", "Endpoints", "Cursor"])
        modelProviderFilter.target = self
        modelProviderFilter.action = #selector(filterModelLibrary)
        let modelColumn = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("model"))
        modelColumn.resizingMask = .autoresizingMask
        modelLibraryTable.addTableColumn(modelColumn)
        modelLibraryTable.headerView = nil
        modelLibraryTable.rowHeight = 78
        modelLibraryTable.style = .inset
        modelLibraryTable.backgroundColor = .clear
        modelLibraryTable.intercellSpacing = NSSize(width: 0, height: 2)
        modelLibraryTable.dataSource = self
        modelLibraryTable.delegate = self
        modelLibraryTable.allowsMultipleSelection = false
        modelLibraryTable.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
        modelLibraryTable.setAccessibilityLabel("Available model library")
        let list = NSScrollView()
        list.documentView = modelLibraryTable
        list.hasVerticalScroller = true
        list.autohidesScrollers = true
        list.borderType = .noBorder
        list.drawsBackground = false
        list.wantsLayer = true
        list.layer?.cornerRadius = 12
        modelListHeight = list.heightAnchor.constraint(equalToConstant: 292)
        modelListHeight?.isActive = true
        let library = StudioCard()
        list.translatesAutoresizingMaskIntoConstraints = false
        library.addSubview(list)
        libraryEmptyState = column([
            title("Make room for your next idea", size: 17),
            note("Refresh to load your available models, or add a model from a connected endpoint.")
        ], spacing: 8)
        libraryEmptyState.translatesAutoresizingMaskIntoConstraints = false
        library.addSubview(libraryEmptyState)
        NSLayoutConstraint.activate([
            list.leadingAnchor.constraint(equalTo: library.leadingAnchor, constant: 6),
            list.trailingAnchor.constraint(equalTo: library.trailingAnchor, constant: -6),
            list.topAnchor.constraint(equalTo: library.topAnchor, constant: 6),
            list.bottomAnchor.constraint(equalTo: library.bottomAnchor, constant: -6),
            libraryEmptyState.leadingAnchor.constraint(equalTo: library.leadingAnchor, constant: 28),
            libraryEmptyState.trailingAnchor.constraint(equalTo: library.trailingAnchor, constant: -28),
            libraryEmptyState.centerYAnchor.constraint(equalTo: library.centerYAnchor)
        ])
        modelDetailTitle.font = .systemFont(ofSize: 17, weight: .semibold)
        modelDetailDescription.font = .systemFont(ofSize: 13)
        modelDetailDescription.textColor = .secondaryLabelColor
        modelCopyButton = button("Copy spawn example", #selector(copyDelegationPrompt))
        modelCopyButton.isHidden = true
        modelRemoveButton = button("Remove from Codex", #selector(removeAddedModel))
        modelRemoveButton.isHidden = true
        displayNameField.placeholderString = "Name shown in Codex's picker"
        displayNameField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        displayNameRow = adaptiveRow([title("Picker name", size: 13), displayNameField, button("Save name", #selector(saveDisplayName))])
        displayNameRow.isHidden = true
        let disclosure = NSButton(title: "", target: self, action: #selector(toggleReasoning(_:)))
        disclosure.setButtonType(.onOff)
        disclosure.bezelStyle = .disclosure
        disclosure.setAccessibilityLabel("Show reasoning effort")
        reasoningDetails = column([
            row([title("Default reasoning effort", size: 13), effort]),
            note("Codex can still pick a different effort per task.")
        ])
        reasoningDetails.isHidden = true
        addModelForm = buildModelBrowser(reasoningDisclosure: disclosure)
        addModelForm.isHidden = true
        modelLibraryBody = column([
            adaptiveRow([modelSearch, modelProviderFilter, button("Refresh", #selector(refreshModelInventory))]),
            registeredModelsSummary,
            library,
            card([modelDetailTitle, modelDetailDescription, displayNameRow, adaptiveRow([modelCopyButton, modelRemoveButton])])
        ], spacing: 14)
        addModelsActionRow = row([primaryButton("Add models", action: #selector(toggleAddModelForm), symbol: "plus"), NSView()])
        return column([
            pageHeading("Your models", subtitle: "Ready for your next task. Choose any added model directly in Codex."),
            addModelsActionRow, addModelForm, modelLibraryBody
        ], spacing: 14)
    }

    private func buildModelBrowser(reasoningDisclosure: NSButton) -> NSView {
        catalogAccounts.target = self
        catalogAccounts.action = #selector(catalogAccountChanged)
        catalogAccounts.setAccessibilityLabel("Model catalog connection")
        catalogSearch.placeholderString = "Search names, providers, or model IDs"
        catalogSearch.sendsSearchStringImmediately = true
        catalogSearch.target = self
        catalogSearch.action = #selector(filterCatalog)
        catalogSearch.setAccessibilityLabel("Search models to add")
        catalogSearch.controlSize = .large
        catalogStatus.font = .systemFont(ofSize: 12)
        catalogStatus.textColor = .secondaryLabelColor
        catalogStatus.setAccessibilityLabel("Model catalog status")
        catalogSelectionSummary.font = .systemFont(ofSize: 12, weight: .medium)
        catalogSelectionSummary.textColor = .secondaryLabelColor
        catalogSelectionSummary.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        catalogProgress.style = .spinning
        catalogProgress.controlSize = .small
        catalogProgress.isDisplayedWhenStopped = false
        catalogProgress.setAccessibilityLabel("Loading models")
        catalogRetryButton = button("Retry", #selector(loadCatalog))
        catalogAuthorizeButton = button("Authorize connection", #selector(authorizeCatalogConnection))
        catalogRetryButton.isHidden = true
        catalogAuthorizeButton.isHidden = true
        let modelColumn = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("catalog"))
        modelColumn.resizingMask = .autoresizingMask
        catalogTable.addTableColumn(modelColumn)
        catalogTable.headerView = nil
        catalogTable.rowHeight = 66
        catalogTable.style = .inset
        catalogTable.backgroundColor = .clear
        catalogTable.dataSource = self
        catalogTable.delegate = self
        catalogTable.allowsMultipleSelection = true
        catalogTable.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
        catalogTable.setAccessibilityLabel("Models to add. Use Space to select highlighted models.")
        catalogTable.toggleSelectedRows = { [weak self] in
            guard let self, !self.registrationInProgress, !self.busy else { return }
            let entries = self.catalogState.visible
            for index in self.catalogTable.selectedRowIndexes where entries.indices.contains(index) {
                if !self.catalogAdded.contains(entries[index].id), self.reservedModelReason(entries[index].id) == nil { self.catalogState.toggle(entries[index].id) }
            }
            self.reloadCatalogRows()
        }
        let list = NSScrollView()
        list.documentView = catalogTable
        list.hasVerticalScroller = true
        list.autohidesScrollers = true
        list.drawsBackground = false
        list.borderType = .noBorder
        catalogListHeight = list.heightAnchor.constraint(equalToConstant: 300)
        catalogListHeight?.isActive = true
        let listContainer = StudioCard()
        list.translatesAutoresizingMaskIntoConstraints = false
        listContainer.addSubview(list)
        catalogEmptyState = column([title("Find your next model", size: 16), note("Choose a saved connection above. Its models will load automatically.")], spacing: 8)
        catalogEmptyState.translatesAutoresizingMaskIntoConstraints = false
        listContainer.addSubview(catalogEmptyState)
        NSLayoutConstraint.activate([
            list.leadingAnchor.constraint(equalTo: listContainer.leadingAnchor, constant: 4),
            list.trailingAnchor.constraint(equalTo: listContainer.trailingAnchor, constant: -4),
            list.topAnchor.constraint(equalTo: listContainer.topAnchor, constant: 4),
            list.bottomAnchor.constraint(equalTo: listContainer.bottomAnchor, constant: -4),
            catalogEmptyState.leadingAnchor.constraint(equalTo: listContainer.leadingAnchor, constant: 24),
            catalogEmptyState.trailingAnchor.constraint(equalTo: listContainer.trailingAnchor, constant: -24),
            catalogEmptyState.centerYAnchor.constraint(equalTo: listContainer.centerYAnchor)
        ])
        catalogAddButton = primaryButton("Add models", action: #selector(registerAgent), symbol: "plus")
        catalogAddButton.isEnabled = false
        let exactModel = SidebarDetails(title: "Use an exact model ID", content: column([
            note("For a model missing from the list. Availability is checked by the provider when you use it."),
            adaptiveRow([model, button("Select ID", #selector(selectExactModel))])
        ], spacing: 8))
        exactModel.setCompact(true)
        let heading = row([title("Discover models", size: 18), NSView(), button("Done", #selector(toggleAddModelForm))])
        let browser = column([
            heading, note("Choose a connection, then select all the models you want."),
            catalogAccounts, activeKeySummary,
            catalogSearch,
            row([catalogProgress, catalogStatus]),
            adaptiveRow([catalogRetryButton, catalogAuthorizeButton]),
            listContainer,
            adaptiveRow([catalogSelectionSummary, button("Select shown", #selector(selectFilteredCatalog)), button("Clear", #selector(clearCatalogSelection))]),
            exactModel,
            row([reasoningDisclosure, note("Reasoning options"), NSView()]), reasoningDetails,
            catalogAddButton,
            note("Models already on another connection stay there. New models appear in Codex on its next turn.")
        ], spacing: 12)
        return browser
    }

    @objc private func filterCatalog() {
        catalogState.query = catalogSearch.stringValue
        reloadCatalogRows()
    }

    private func reloadCatalogRows() {
        let origin = catalogTable.enclosingScrollView?.contentView.bounds.origin ?? .zero
        catalogTable.reloadData()
        if let scroll = catalogTable.enclosingScrollView {
            scroll.contentView.scroll(to: NSPoint(x: origin.x, y: min(origin.y, max(0, catalogTable.bounds.height - scroll.contentView.bounds.height))))
            scroll.reflectScrolledClipView(scroll.contentView)
        }
        let count = catalogState.selected.count
        catalogSelectionSummary.stringValue = "\(count) selected · \(catalogState.visible.count) shown"
        catalogAddButton?.title = count == 0 ? "Add models" : "Add \(count) model\(count == 1 ? "" : "s")"
        catalogAddButton?.isEnabled = count > 0 && !busy && !registrationInProgress && currentAccount() != nil
        catalogEmptyState?.isHidden = !catalogState.visible.isEmpty
        if let empty = catalogEmptyState as? NSStackView {
            let labels = empty.arrangedSubviews.compactMap { $0 as? NSTextField }
            labels.first?.stringValue = catalogLoading ? "Loading models…" : catalogState.entries.isEmpty ? "No catalog loaded" : "No matching models"
            labels.last?.stringValue = catalogLoading ? "Your connection is being checked." : catalogState.entries.isEmpty
                ? "Choose a connection, retry its catalog, or select an exact model ID."
                : "Try fewer words or a different model name. Your selections are kept."
        }
    }

    @objc private func toggleCatalogCheckbox(_ sender: NSButton) {
        guard !busy, !registrationInProgress, let id = sender.identifier?.rawValue, !catalogAdded.contains(id), reservedModelReason(id) == nil else { return }
        catalogState.toggle(id)
        reloadCatalogRows()
    }

    @objc private func selectFilteredCatalog() {
        guard !busy, !registrationInProgress else { return }
        catalogState.selected.formUnion(catalogState.visible.map(\.id).filter { !catalogAdded.contains($0) && reservedModelReason($0) == nil })
        reloadCatalogRows()
    }

    @objc private func clearCatalogSelection() {
        guard !busy, !registrationInProgress else { return }
        catalogState.selected.removeAll()
        reloadCatalogRows()
    }

    @objc private func selectExactModel() {
        do {
            let slug = try selectedModel()
            if let reason = reservedModelReason(slug) { report(reason, error: true); return }
            guard !catalogAdded.contains(slug) else { report("This model is already added on this connection."); return }
            if !catalogState.entries.contains(where: { $0.id == slug }) {
                catalogState.entries.append(CatalogModel(id: slug, name: slug, suggested: true))
            }
            catalogState.selected.insert(slug)
            model.stringValue = ""
            reloadCatalogRows()
        } catch { report(error.localizedDescription, error: true) }
    }

    @objc private func catalogAccountChanged() {
        guard !registrationInProgress, !busy else { return }
        accounts.selectItem(at: catalogAccounts.indexOfSelectedItem)
        accountChanged()
    }

    private func catalogCell(at index: Int) -> NSView? {
        let entries = catalogState.visible
        guard entries.indices.contains(index) else { return nil }
        let entry = entries[index]
        let cell = NSTableCellView()
        let check = NSButton(checkboxWithTitle: "", target: self, action: #selector(toggleCatalogCheckbox(_:)))
        check.identifier = NSUserInterfaceItemIdentifier(entry.id)
        check.state = catalogState.selected.contains(entry.id) || catalogAdded.contains(entry.id) ? .on : .off
        check.isEnabled = !catalogAdded.contains(entry.id) && reservedModelReason(entry.id) == nil && !registrationInProgress && !busy
        check.setAccessibilityLabel("Select \(entry.name)")
        check.setAccessibilityValue(check.state == .on ? "Selected" : "Not selected")
        let heading = title(entry.name, size: 14)
        heading.maximumNumberOfLines = 1
        heading.lineBreakMode = .byTruncatingTail
        let detail = NSTextField(labelWithString: entry.id)
        detail.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        detail.textColor = .secondaryLabelColor
        detail.lineBreakMode = .byTruncatingMiddle
        let state = NSTextField(labelWithString: reservedModelReason(entry.id) != nil ? "Built in" : catalogAdded.contains(entry.id) ? "Added" : catalogFailures[entry.id] != nil ? "Needs attention" : entry.suggested ? "Suggested" : "")
        state.font = .systemFont(ofSize: 11, weight: .medium)
        state.textColor = catalogFailures[entry.id] != nil ? .systemOrange : .secondaryLabelColor
        state.setContentHuggingPriority(.required, for: .horizontal)
        let content = row([check, column([heading, detail], spacing: 4), state])
        content.translatesAutoresizingMaskIntoConstraints = false
        cell.addSubview(content)
        cell.textField = heading
        NSLayoutConstraint.activate([
            content.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 8),
            content.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -10),
            content.centerYAnchor.constraint(equalTo: cell.centerYAnchor)
        ])
        cell.toolTip = reservedModelReason(entry.id) ?? catalogFailures[entry.id] ?? (entry.name + "\n" + entry.id)
        return cell
    }

    private func reservedModelReason(_ id: String) -> String? {
        let normalized = id.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "~"))
        return normalized.hasPrefix("openai/") || normalized.hasPrefix("gpt-") || normalized.hasPrefix("codex-")
            ? "OpenAI models are built into Codex and use your ChatGPT subscription. Their names cannot be replaced by another provider." : nil
    }

    /// Deterministic screenshots use synthetic accounts and never load preferences or credentials.
    private func installBrowserPreview() {
        preferences.accounts = providerPresets.enumerated().map { index, preset in
            SavedAccount(id: "preview-\(index)", name: preset.name, baseURL: preset.base_url, wire: preset.wire, hasKey: true)
        }
        if preferences.accounts.isEmpty {
            preferences.accounts = [SavedAccount(id: "preview", name: "Example connection", baseURL: "https://example.invalid/v1", wire: "chat", hasKey: false)]
        }
        preferences.selectedAccount = preferences.accounts[0].id
        reloadAccounts()
        let generation = catalogState.begin(route: "preview")
        _ = catalogState.receive([
            CatalogModel(id: "kimi-for-coding", name: "Kimi for Coding", suggested: false),
            CatalogModel(id: "kimi-for-coding-highspeed", name: "Kimi for Coding · High speed", suggested: false),
            CatalogModel(id: "kimi-reasoning-preview", name: "Kimi Reasoning Preview", suggested: true),
            CatalogModel(id: "kimi-long-context-preview", name: "Kimi Long Context Preview", suggested: true),
            CatalogModel(id: "kimi-vision-preview", name: "Kimi Vision Preview", suggested: true)
        ], generation: generation)
        catalogState.selected = ["kimi-for-coding", "kimi-for-coding-highspeed"]
        catalogStatus.stringValue = "Synthetic preview · 5 example models. No provider requests or settings changes."
        registeredModelRecords = [
            ["provider": "openai", "model": "gpt-6-astra", "display_name": "GPT-6 Astra", "billing": "ChatGPT subscription"],
            ["provider": "google", "model": "gemini-example", "display_name": "Gemini", "endpoint": "Google Gemini", "billing": "Gemini API · eligible developer credits"],
            ["provider": "deepseek", "model": "deepseek-example", "display_name": "DeepSeek", "endpoint": "DeepSeek", "billing": "DeepSeek API"]
        ]
        registeredModelsSummary.stringValue = "3 example models · preview only"
        modelsInventoryLoaded = true
        filterModelLibrary()
        reloadCatalogRows()
        setModelBrowserVisible(CommandLine.arguments.contains("--catalog"))
        status.stringValue = "Preview only"
        feedback.stringValue = "Preview · Synthetic data. No credentials, provider requests, or settings changes."
    }

    @objc private func toggleAddModelForm() {
        setModelBrowserVisible(addModelForm.isHidden)
        if !addModelForm.isHidden {
            loadCatalog()
            DispatchQueue.main.async {
                self.addModelForm.window?.contentView?.layoutSubtreeIfNeeded()
                if let scroll = self.studioPages[1] as? NSScrollView {
                    scroll.contentView.scroll(to: .zero)
                    scroll.reflectScrolledClipView(scroll.contentView)
                }
                self.addModelForm.window?.makeFirstResponder(self.catalogSearch)
            }
        }
    }

    private func setModelBrowserVisible(_ visible: Bool) {
        addModelForm.isHidden = !visible
        modelLibraryBody.isHidden = visible
        addModelsActionRow.isHidden = visible
    }

    @objc private func toggleReasoning(_ sender: NSButton) {
        reasoningDetails.isHidden = sender.state != .on
    }

    @objc private func refreshModelInventory() {
        guard !busy, !renderingPreview, !attachedPreview else { return }
        setBusy(true)
        registeredModelsSummary.stringValue = "Loading model library…"
        DispatchQueue.global(qos: .userInitiated).async {
            var result: [String: Any]
            do {
                let process = Process()
                let input = Pipe(), output = Pipe()
                let buffer = UsageOutputBuffer(maximumBytes: 131072)
                let finished = DispatchSemaphore(value: 0), drained = DispatchSemaphore(value: 0)
                process.executableURL = URL(fileURLWithPath: Bundle.main.object(forInfoDictionaryKey: "PythonExecutable") as? String ?? "/opt/homebrew/bin/python3")
                process.arguments = ["-B", Bundle.main.resourceURL!.appendingPathComponent("model_catalog.py").path]
                process.standardInput = input
                process.standardOutput = output
                process.standardError = FileHandle.nullDevice
                process.terminationHandler = { _ in finished.signal() }
                output.fileHandleForReading.readabilityHandler = { handle in
                    let bytes = handle.availableData
                    if bytes.isEmpty { handle.readabilityHandler = nil; drained.signal() }
                    else { buffer.append(bytes) }
                }
                defer { output.fileHandleForReading.readabilityHandler = nil }
                try process.run()
                input.fileHandleForWriting.write(Data("{}".utf8))
                input.fileHandleForWriting.closeFile()
                if finished.wait(timeout: .now() + 29) == .timedOut {
                    process.terminate()
                    if finished.wait(timeout: .now() + 0.5) == .timedOut { Darwin.kill(process.processIdentifier, SIGKILL) }
                    throw SettingsError.message("Model library timed out.")
                }
                guard drained.wait(timeout: .now() + 0.5) == .success,
                      let bytes = buffer.snapshot(),
                      let parsed = try JSONSerialization.jsonObject(with: bytes) as? [String: Any] else {
                    throw SettingsError.message("Model library response unavailable.")
                }
                result = parsed
            } catch {
                let error: [String: Any] = ["ok": false, "error": "Could not load this source. Try Refresh."]
                result = ["openai": error, "openrouter": error, "models": []]
            }
            let finalResult = result
            DispatchQueue.main.async {
                self.setBusy(false)
                self.registeredModelRecords = (finalResult["models"] as? [[String: Any]] ?? []).filter {
                    $0["model"] is String && $0["display_name"] is String &&
                    ["openai", "openrouter", "cursor", "kimi", "zai", "minimax", "google", "deepseek", "custom"].contains($0["provider"] as? String ?? "")
                }
                var sourceNotes: [String] = []
                var anyAvailable = false
                let sources = [("openai", "OpenAI"), ("openrouter", "OpenRouter")] + (finalResult["cursor"] == nil ? [] : [("cursor", "Cursor")])
                for (key, label) in sources {
                    let source = finalResult[key] as? [String: Any] ?? [:]
                    if source["ok"] as? Bool == true { anyAvailable = true }
                    else { sourceNotes.append(label + ": " + (source["error"] as? String ?? "Source unavailable")) }
                }
                self.modelsInventoryLoaded = anyAvailable
                let added = self.registeredModelRecords.filter { $0["provider"] as? String != "openai" }.count
                self.registeredModelsSummary.stringValue = sourceNotes.isEmpty
                    ? "\(self.registeredModelRecords.count) models available · \(added) from your endpoints"
                    : sourceNotes.joined(separator: "\n")
                self.filterModelLibrary()
                self.updateCatalogAddedModels()
            }
        }
    }

    @objc private func filterModelLibrary() {
        let query = modelSearch.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let provider = ["", "openai", "external", "cursor"][max(0, min(3, modelProviderFilter.indexOfSelectedItem))]
        let previousModel = selectedLibraryModel()?["model"] as? String
        visibleModelRecords = registeredModelRecords.filter { record in
            let matchesProvider = provider.isEmpty || record["provider"] as? String == provider
                || (provider == "external" && record["provider"] as? String != "openai")
            let text = [record["model"], record["display_name"], record["description"]].compactMap { $0 as? String }.joined(separator: " ").lowercased()
            return matchesProvider && CatalogModel.matches(query, text: text)
        }
        modelLibraryTable.reloadData()
        libraryEmptyState.isHidden = !visibleModelRecords.isEmpty
        if let selected = visibleModelRecords.firstIndex(where: { $0["model"] as? String == previousModel }) {
            modelLibraryTable.selectRowIndexes(IndexSet(integer: selected), byExtendingSelection: false)
        } else if !visibleModelRecords.isEmpty {
            modelLibraryTable.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false)
        } else { modelLibraryTable.deselectAll(nil) }
        updateLibrarySelection()
    }

    func numberOfRows(in tableView: NSTableView) -> Int { tableView === catalogTable ? catalogState.visible.count : visibleModelRecords.count }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row index: Int) -> NSView? {
        if tableView === catalogTable { return catalogCell(at: index) }
        guard visibleModelRecords.indices.contains(index) else { return nil }
        let record = visibleModelRecords[index]
        let cell = NSTableCellView()
        let heading = NSTextField(labelWithString: record["display_name"] as? String ?? "")
        heading.font = .systemFont(ofSize: 14, weight: .semibold)
        heading.lineBreakMode = .byTruncatingTail
        let endpointName = record["endpoint"] as? String ?? "OpenRouter"
        let billed = record["billed"] as? Bool ?? true
        let viaOpenRouter = record["openrouter"] as? Bool ?? (endpointName == "OpenRouter")
        let billing = NSTextField(labelWithString: record["billing"] as? String ?? (record["provider"] as? String == "openai"
            ? "ChatGPT subscription"
            : record["provider"] as? String == "cursor" ? "Cursor SDK · Cursor account"
            : endpointName + (viaOpenRouter ? " · OpenRouter credits" : billed ? " · API key" : " · local, free")))
        billing.font = .systemFont(ofSize: 11)
        billing.textColor = .secondaryLabelColor
        let identifier = NSTextField(labelWithString: record["model"] as? String ?? "")
        identifier.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        identifier.textColor = .secondaryLabelColor
        identifier.lineBreakMode = .byTruncatingMiddle
        let provider = record["provider"] as? String ?? ""
        let preset = providerPresets.first { $0.id == provider }
        let icon = NSImageView(image: NSImage(systemSymbolName: preset?.symbol ?? (provider == "cursor" ? "cursorarrow" : provider == "openai" ? "sparkles" : "cube"), accessibilityDescription: nil)!)
        icon.contentTintColor = preset.map { StudioPalette.color($0.color) } ?? (provider == "openai" ? .systemTeal : provider == "cursor" ? .labelColor : .systemIndigo)
        icon.widthAnchor.constraint(equalToConstant: 28).isActive = true
        let content = row([icon, column([heading, billing, identifier], spacing: 4)])
        content.translatesAutoresizingMaskIntoConstraints = false
        cell.addSubview(content)
        cell.textField = heading
        NSLayoutConstraint.activate([
            content.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 10),
            content.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -10),
            content.centerYAnchor.constraint(equalTo: cell.centerYAnchor)
        ])
        cell.toolTip = (record["display_name"] as? String ?? "") + "\n" + (record["model"] as? String ?? "")
        return cell
    }

    func tableViewSelectionDidChange(_ notification: Notification) {
        if notification.object as? NSTableView === modelLibraryTable { updateLibrarySelection() }
    }

    private func selectedLibraryModel() -> [String: Any]? {
        let selected = modelLibraryTable.selectedRow
        return visibleModelRecords.indices.contains(selected) ? visibleModelRecords[selected] : nil
    }

    private func updateLibrarySelection() {
        guard let record = selectedLibraryModel() else {
            modelDetailTitle.stringValue = "No model selected"
            modelDetailDescription.stringValue = registeredModelRecords.isEmpty
                ? "Refresh to load your library, or connect an endpoint and add a model."
                : "No models match this search."
            modelCopyButton.isHidden = true
            modelRemoveButton.isHidden = true
            displayNameRow.isHidden = true
            return
        }
        modelDetailTitle.stringValue = record["display_name"] as? String ?? ""
        let isAddedModel = record["provider"] as? String != "openai"
        let endpointName = record["endpoint"] as? String ?? "OpenRouter"
        let connection: String
        if record["provider"] as? String == "cursor" {
            connection = "Runs through the Cursor SDK, billed to your Cursor account under its IDE / Cloud Agent pricing and usage pools. Your plan may allow paid overage. Choose this model in Codex, or use its name when spawning a helper."
        } else if !isAddedModel {
            connection = "Billed to your ChatGPT subscription. Pick it in Codex's model picker."
        } else if record["openrouter"] as? Bool ?? (endpointName == "OpenRouter") {
            connection = "Billed to OpenRouter credits. Pick it in Codex's model picker, or have a task spawn a helper with this model name. Fast in Codex routes to OpenRouter's fastest provider."
        } else if let billingNote = record["billing_note"] as? String, !billingNote.isEmpty {
            connection = "Runs on \(endpointName). " + billingNote + " Choose it in Codex or use its name when spawning an agent."
        } else if record["billed"] as? Bool ?? true {
            connection = "Runs on \(endpointName) with that endpoint's API key. Pick it in Codex's model picker, or have a task spawn a helper with this model name."
        } else {
            connection = "Runs on \(endpointName) with no key, so nothing is billed. Pick it in Codex's model picker, or have a task spawn a helper with this model name."
        }
        let description = record["description"] as? String ?? ""
        let pricing = record["pricing"] as? String ?? ""
        modelDetailDescription.stringValue = connection
            + (pricing.isEmpty ? "" : "\n\nProvider list price: " + pricing + ".")
            + (description.isEmpty ? "" : "\n\n" + description)
        modelCopyButton.isHidden = !isAddedModel
        modelRemoveButton.isHidden = !isAddedModel
        displayNameRow.isHidden = !isAddedModel
        displayNameField.stringValue = isAddedModel ? (record["display_name"] as? String ?? "") : ""
    }

    /// Deletes the selected added model's role file. Its endpoint and key are untouched.
    @objc private func removeAddedModel() {
        guard let record = selectedLibraryModel(), record["provider"] as? String != "openai",
              let model = record["model"] as? String, !model.isEmpty else { return }
        let alert = NSAlert()
        alert.messageText = "Remove \(model) from Codex?"
        alert.informativeText = "It leaves Codex's picker on the next turn. The endpoint and its key stay."
        alert.addButton(withTitle: "Remove")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        configure(["action": "remove_agent", "model": model]) { result in
            self.displayStatus(result)
            if result["ok"] as? Bool == true {
                self.report("Removed \(model).")
                self.refreshModelInventory()
            }
        }
    }

    private var displayNamesURL: URL { stateDirectory.appendingPathComponent("display-names.json") }

    /// Saves the picker name Codex shows for the selected OpenRouter model. Empty restores the automatic name.
    @objc private func saveDisplayName() {
        guard !renderingPreview, !attachedPreview else { return }
        guard let record = selectedLibraryModel(), record["provider"] as? String != "openai",
              let model = record["model"] as? String, !model.isEmpty else { return }
        let name = displayNameField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard name.count <= 48, !name.unicodeScalars.contains(where: { $0.value < 32 }) else {
            report("Use a name of at most 48 characters.", error: true)
            return
        }
        var names: [String: String] = [:]
        if let data = try? Data(contentsOf: displayNamesURL), let existing = try? JSONSerialization.jsonObject(with: data) as? [String: String] {
            names = existing
        }
        if name.isEmpty { names.removeValue(forKey: model) } else { names[model] = name }
        do {
            try FileManager.default.createDirectory(at: stateDirectory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            try JSONSerialization.data(withJSONObject: names, options: [.prettyPrinted, .sortedKeys]).write(to: displayNamesURL, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: displayNamesURL.path)
        } catch {
            report("The display name could not be saved.", error: true)
            return
        }
        report(name.isEmpty ? "Automatic name restored. Codex shows it after its next launch from Model Deck."
                            : "Codex will show this model as “\(name)” after its next launch from Model Deck.")
        refreshModelInventory()
    }

    @objc private func copyDelegationPrompt() {
        guard let record = selectedLibraryModel(), record["provider"] as? String != "openai",
              let model = record["model"] as? String, !model.isEmpty else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString("Spawn a helper agent using the model \"\(model)\" to: [describe the task].", forType: .string)
        report("Spawn example copied. Paste it into a Codex task.")
    }

    private func buildKeysPage() -> NSView {
        accounts.target = self
        accounts.action = #selector(accountChanged)
        accounts.setContentHuggingPriority(.defaultLow, for: .horizontal)
        accountName.placeholderString = "A name you’ll recognize"
        endpointURL.placeholderString = SavedAccount.openRouterURL
        endpointFormat.addItems(withTitles: ["Detect automatically", "Responses API", "Chat completions"])
        endpointKind.addItems(withTitles: ["OpenRouter", "Cursor SDK", "OpenAI-compatible API", "Local server"] + providerPresets.map(\.name))
        endpointKind.setSymbol("arrow.triangle.branch", color: .systemIndigo, for: "OpenRouter")
        endpointKind.setSymbol("cursorarrow", color: .labelColor, for: "Cursor SDK")
        endpointKind.setSymbol("desktopcomputer", color: .systemTeal, for: "Local server")
        for preset in providerPresets { endpointKind.setSymbol(preset.symbol, color: StudioPalette.color(preset.color), for: preset.name) }
        endpointKind.target = self
        endpointKind.action = #selector(endpointKindChanged)
        for label in [endpointSummary, endpointHint, cursorSDKStatus] {
            label.font = .systemFont(ofSize: 13)
            label.textColor = .secondaryLabelColor
        }
        cursorSetupCard = card([
            providerLine("Cursor SDK", subtitle: "Your Cursor models, inside Codex", symbol: "cursorarrow", color: .labelColor),
            cursorSDKStatus,
            adaptiveRow([button("Check SDK", #selector(checkCursorSDK)), button("Install SDK", #selector(installCursorSDK))]),
            note("Install once on this Mac, save a Cursor API key, then browse its models. Requests use your Cursor account’s IDE / Cloud Agent usage pools; overage depends on your plan."),
            row([button("Cursor account ↗", #selector(openCursorDashboard)), NSView()])
        ])
        endpointFormatRow = adaptiveRow([title("API format", size: 12), endpointFormat])
        endpointKindChanged()
        return column([
            pageHeading("Connections", subtitle: "Bring your favorite providers. Your keys stay in macOS Keychain."),
            card([
                title("Your connections", size: 16),
                adaptiveRow([accounts, button("Test connection", #selector(checkKey))]),
                endpointSummary
            ]),
            card([
                title("Connect a provider", size: 16),
                endpointKind,
                endpointHint,
                title("Connection name", size: 12), accountName,
                title("Base URL", size: 12), endpointURL,
                title("API key", size: 12), apiKey,
                endpointFormatRow,
                adaptiveRow([primaryButton("Save & choose models", action: #selector(saveKey), symbol: "plus"), button("Get provider key ↗", #selector(openProviderKeyPage))])
            ]),
            cursorSetupCard
        ], spacing: 20)
    }

    @objc private func endpointKindChanged() {
        let kind = endpointKind.indexOfSelectedItem
        endpointURL.isEnabled = kind == 2 || kind == 3
        endpointFormat.isEnabled = kind == 2 || kind == 3
        endpointFormatRow?.isHidden = kind != 2 && kind != 3
        cursorSetupCard?.isHidden = kind != 1 && currentAccount()?.isCursor != true
        if kind >= 4, providerPresets.indices.contains(kind - 4) {
            let preset = providerPresets[kind - 4]
            endpointURL.stringValue = preset.base_url
            accountName.stringValue = preset.name
            apiKey.placeholderString = preset.billing.contains("Plan") || preset.id == "kimi" ? "Your coding plan key" : "Your provider API key"
            endpointHint.stringValue = preset.billing_note
            endpointFormat.selectItem(at: preset.wire == "responses" ? 1 : 2)
        } else if kind == 1 {
            endpointURL.stringValue = SavedAccount.cursorURL
            accountName.stringValue = "Cursor"
            apiKey.placeholderString = "Cursor API key"
            endpointHint.stringValue = "Connect Composer and Auto through Cursor’s official SDK. Uses your Cursor account; it does not use ChatGPT or OpenRouter credits."
        } else if kind == 0 {
            endpointURL.stringValue = SavedAccount.openRouterURL
            accountName.stringValue = "OpenRouter"
            apiKey.placeholderString = "sk-or-…"
            endpointHint.stringValue = "One key for the OpenRouter model catalog. Requests use your OpenRouter credits."
        } else if kind == 3 {
            endpointURL.stringValue = "http://localhost:1234/v1"
            accountName.stringValue = "Local models"
            apiKey.placeholderString = "Optional for local servers"
            endpointHint.stringValue = "LM Studio usually uses port 1234; Ollama uses 11434. Your local server must be running."
        } else {
            endpointURL.stringValue = ""
            accountName.stringValue = ""
            apiKey.placeholderString = "Your provider’s API key"
            endpointHint.stringValue = "Connect any OpenAI-compatible API using its base URL and key. Your provider handles billing."
        }
    }

    @objc private func openProviderKeyPage() {
        let kind = endpointKind.indexOfSelectedItem
        let destination = kind >= 4 && providerPresets.indices.contains(kind - 4) ? providerPresets[kind - 4].key_url
            : kind == 1 ? "https://cursor.com/dashboard" : kind == 0 ? "https://openrouter.ai/settings/keys" : ""
        if let url = URL(string: destination), !destination.isEmpty { NSWorkspace.shared.open(url) }
        else { report("Create a key in your provider’s dashboard, or leave it empty for a local server.") }
    }

    private func cursorRequest(_ action: String) -> [String: Any] {
        var request: [String: Any] = ["action": action, "executable": Bundle.main.executableURL!.path]
        if let account = currentAccount(), account.isCursor { request["account"] = account.id }
        return request
    }

    private func showCursorSDKStatus(_ result: [String: Any]) {
        let installed = result["installed"] as? Bool == true
        let version = result["version"] as? String
        cursorSDKStatus.stringValue = result["ok"] as? Bool == true
            ? (installed ? "SDK installed" + (version.map { " · " + $0 } ?? "") : "SDK not installed on this Mac")
                + "\n" + (result["message"] as? String ?? "")
            : result["error"] as? String ?? result["message"] as? String ?? "Could not check the Cursor SDK."
        report(result["message"] as? String ?? cursorSDKStatus.stringValue, error: result["ok"] as? Bool != true)
    }

    @objc private func checkCursorSDK() {
        cursorSDKStatus.stringValue = "Checking the SDK on this Mac…"
        configure(cursorRequest("cursor_status")) { self.showCursorSDKStatus($0) }
    }

    @objc private func installCursorSDK() {
        cursorSDKStatus.stringValue = "Installing Cursor SDK…"
        report("Installing Cursor SDK on this Mac…")
        configure(cursorRequest("install_cursor_sdk")) { self.showCursorSDKStatus($0) }
    }

    @objc private func openCursorDashboard() {
        NSWorkspace.shared.open(URL(string: "https://cursor.com/dashboard")!)
    }

    private func loadCursorModels(testOnly: Bool = false) {
        guard let account = currentAccount(), account.isCursor else { report("Save and select a Cursor connection first.", error: true); return }
        if !testOnly { loadCatalog(); return }
        report(testOnly ? "Testing the Cursor connection…" : "Loading Cursor models…")
        authorizeAccount(account) { authorized in
            guard authorized else { return }
            self.configure(self.cursorRequest("cursor_models")) { result in
            guard result["ok"] as? Bool == true, let entries = result["models"] as? [[String: Any]] else {
                self.report(result["error"] as? String ?? result["message"] as? String ?? "Could not list Cursor models. Check the SDK and saved key in Endpoints.", error: true)
                return
            }
                self.report(result["message"] as? String ?? "Cursor lists \(entries.count) models. Choose any to add to Codex.")
            }
        }
    }

    private func buildAdvancedPage() -> NSView {
        restoreButton = button("Restore previous default", #selector(restoreSettings))
        let support = "~/Library/Application Support/Model Deck/"
        return column([
            pageHeading("Advanced", subtitle: "Where things live, and recovery for earlier versions."),
            card([
                title("Files", size: 16),
                lead("Router log", support + "router.log"),
                lead("Request ledger", support + "router-ledger.jsonl"),
                lead("Added models", "~/.codex/agents/openrouter_*.toml"),
                note("Delete a model's file and relaunch Codex to remove it. None of these files contain keys.")
            ]),
            card([
                title("Restore the previous default provider", size: 16),
                note("Version 1.0 could replace your global Codex provider. This puts back the settings saved before that change; it leaves added models and keys alone."),
                row([restoreButton])
            ])
        ], spacing: 16)
    }

    private func buildUsagePage() -> NSView {
        usageSelectedOpenRouterAccountID = currentUsageAccountID()
        usageDashboard.onRefresh = { [weak self] in self?.performUsageRefresh(quiet: false) }
        usageDashboard.onOpenRouterAccountSelected = { [weak self] accountID in
            guard let self, self.preferences.accounts.contains(where: { $0.id == accountID && $0.isOpenRouter && !$0.isCursor && $0.keyed }) else { return }
            guard self.currentUsageAccountID() != accountID else { return }
            self.usageSelectedOpenRouterAccountID = accountID
            self.usageRefreshMessage = nil
            self.renderUsageDashboard()
            self.performUsageRefresh(quiet: false)
        }
        if !attachedPreview && !renderingPreview { usageEntries = routerLedgerEntries(limit: 10_000) }
        renderUsageDashboard()
        return usageDashboard
    }

    private func renderUsageDashboard() {
        let accountID = currentUsageAccountID()
        usageDashboard.update(accounts: preferences.accounts, subscription: usageSubscription,
                              openRouter: usageOpenRouterAccountID == accountID ? usageOpenRouter : [:],
                              openRouterAccountID: accountID, entries: usageEntries,
                              updatedAt: usageRefreshedAt, refreshMessage: usageRefreshMessage,
                              isRefreshing: usageRefreshInFlight)
    }

    /// Populated usage previews are synthetic and never touch account APIs or saved state.
    private func installUsagePreview() {
        preferences.accounts = [
            SavedAccount(id: "preview-openrouter", name: "OpenRouter", baseURL: SavedAccount.openRouterURL),
            SavedAccount(id: "preview-cursor", name: "Cursor", baseURL: SavedAccount.cursorURL, wire: "cursor")
        ]
        if CommandLine.arguments.contains("--usage-many") {
            preferences.accounts += [
                SavedAccount(id: "preview-local", name: "LM Studio", baseURL: "http://localhost:1234/v1", hasKey: false),
                SavedAccount(id: "preview-gemini", name: "Google Gemini", baseURL: "https://example.invalid/v1")
            ]
        }
        preferences.selectedAccount = "preview-openrouter"
        usageSelectedOpenRouterAccountID = "preview-openrouter"
        usageOpenRouterAccountID = "preview-openrouter"
        let now = Date()
        usageRefreshedAt = now
        usageSubscription = [
            "ok": true,
            "windows": [
                ["pool_id": "codex", "pool_name": "Codex", "window_kind": "primary", "used_percent": 32.0, "window_minutes": 300, "resets_at": now.addingTimeInterval(7200).timeIntervalSince1970],
                ["pool_id": "codex", "pool_name": "Codex", "window_kind": "secondary", "used_percent": 58.0, "window_minutes": 10080, "resets_at": now.addingTimeInterval(180000).timeIntervalSince1970],
                ["pool_id": "spark", "pool_name": "GPT-5.3-Codex-Spark", "window_kind": "primary", "used_percent": 8.0, "window_minutes": 300, "resets_at": now.addingTimeInterval(5400).timeIntervalSince1970]
            ],
            "lifetime_tokens": 18240000,
            "summary": ["peak_daily_tokens": 2450000, "current_streak_days": 6, "longest_streak_days": 12]
        ]
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd"
        usageSubscription["daily_usage"] = (0..<14).map { offset -> [String: Any] in
            let date = Calendar.current.date(byAdding: .day, value: offset - 13, to: now)!
            return ["date": formatter.string(from: date), "tokens": 180000 + ((offset * 173091) % 2100000)]
        }
        usageOpenRouter = ["ok": true, "usage_daily": 2.84, "usage_weekly": 14.62, "usage_monthly": 38.91,
                           "usage_total": 126.48, "limit": 100, "limit_remaining": 61.09,
                           "limit_known": true, "limit_reset": "monthly", "byok_usage": 1.12,
                           "include_byok_in_limit": false]
        let timestamp = ISO8601DateFormatter()
        usageEntries = (0..<84).map { index -> [String: Any] in
            let route = ["openai", "openrouter", "cursor"][index % 3]
            let model = ["gpt-6-astra", "deepseek/deepseek-v4.1-flash", "cursor/composer-2.5"][index % 3]
            let endpoint = ["ChatGPT", "OpenRouter", "Cursor"][index % 3]
            let day = index / 12
            let date = Calendar.current.date(byAdding: .day, value: day - 6, to: now)!.addingTimeInterval(-Double(index % 12) * 120)
            var record: [String: Any] = ["timestamp": timestamp.string(from: date), "route": route,
                                       "endpoint": endpoint, "model": model, "agent_name": index % 4 == 0 ? "/root" : "/root/implementation",
                                       "status": index == 77 ? 429 : 200]
            if route != "openai" {
                var tokens: [String: Any] = ["input_tokens": 1400 + index * 71, "output_tokens": 400 + index * 23,
                                           "total_tokens": 1800 + index * 94]
                if route == "cursor" { if index % 5 != 0 { record["cost"] = Double(index + 1) * 0.007 } }
                else { tokens["cost"] = Double(index + 1) * 0.0014 }
                record["usage"] = tokens
            }
            return record
        }
        if CommandLine.arguments.contains("--usage-empty") {
            usageSubscription = [:]; usageOpenRouter = [:]; usageEntries = []; usageRefreshedAt = nil
        }
        if CommandLine.arguments.contains("--usage-error") {
            usageRefreshMessage = "OpenRouter spending could not refresh. Previous figures are kept when available."
        }
        reloadAccounts()
        renderUsageDashboard()
        if let selection = CommandLine.arguments.first(where: { $0.hasPrefix("--usage-tab=") }),
           let index = Int(selection.dropFirst("--usage-tab=".count)) { usageDashboard.selectTab(index) }
        if let selection = CommandLine.arguments.first(where: { $0.hasPrefix("--usage-provider=") }) {
            usageDashboard.selectProvider(String(selection.dropFirst("--usage-provider=".count)))
        }
        feedback.stringValue = "Preview · Synthetic usage. No account requests or settings changes."
    }

    private func routerLedgerEntries(limit: Int) -> [[String: Any]] {
        let url = stateDirectory.appendingPathComponent("router-ledger.jsonl")
        guard let handle = try? FileHandle(forReadingFrom: url) else { return [] }
        defer { try? handle.close() }
        let size = (try? handle.seekToEnd()) ?? 0
        let window: UInt64 = 8 * 1024 * 1024
        let start = size > window ? size - window : 0
        try? handle.seek(toOffset: start)
        guard var data = try? handle.read(upToCount: Int(window)) else { return [] }
        // Discard the partial first record before UTF-8 decoding; the seek may split a character.
        if start > 0 {
            guard let newline = data.firstIndex(of: 0x0A) else { return [] }
            data = Data(data[data.index(after: newline)...])
        }
        guard let text = String(data: data, encoding: .utf8) else { return [] }
        let lines = text.split(separator: "\n").map(String.init)
        return lines.suffix(limit).compactMap { (try? JSONSerialization.jsonObject(with: Data($0.utf8))) as? [String: Any] }
    }

    @objc private func openOpenAIPricing() {
        NSWorkspace.shared.open(URL(string: "https://learn.chatgpt.com/docs/pricing")!)
    }

    @objc private func navigateStudio(_ sender: NSButton) {
        showStudioPage(sender.tag)
    }

    @objc private func showKeysPage() {
        showStudioPage(2)
    }

    private func showStudioPage(_ selected: Int) {
        selectedPage = selected
        for (index, page) in studioPages.enumerated() { page.isHidden = index != selected }
        if !attachedPreview, !renderingPreview, !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion, studioPages.indices.contains(selected) {
            let page = studioPages[selected]
            page.alphaValue = 0.65
            NSAnimationContext.runAnimationGroup { context in
                context.duration = 0.12
                page.animator().alphaValue = 1
            }
        }
        for (index, control) in navigationButtons.enumerated() {
            control.state = index == selected ? .on : .off
            control.contentTintColor = index == selected ? .controlAccentColor : .secondaryLabelColor
            control.needsDisplay = true
        }
        companion?.setSelectedPage(selected)
        if selected == 1 && !modelsInventoryLoaded && !attachedPreview && !renderingPreview { refreshModelInventory() }
        setUsagePolling(selected == 3 && !attachedPreview && !renderingPreview)
    }

    /// Keeps the Usage page current without manual refreshes while it is the visible page.
    private func setUsagePolling(_ enabled: Bool) {
        guard enabled else {
            usagePollTimer?.invalidate()
            usagePollTimer = nil
            return
        }
        guard usagePollTimer == nil else { return }
        performUsageRefresh(quiet: true)
        usagePollTimer = Timer.scheduledTimer(withTimeInterval: usagePollInterval, repeats: true) { [weak self] _ in
            guard let self, self.studioPages.indices.contains(3) else { return }
            let page = self.studioPages[3]
            // Skip ticks while no window is actually showing the page.
            guard let host = page.window, host.isVisible, !page.isHiddenOrHasHiddenAncestor else { return }
            self.performUsageRefresh(quiet: true)
        }
    }

    private func buildWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1060, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Model Deck for Codex"
        window.contentMinSize = NSSize(width: 720, height: 520)
        window.titlebarAppearsTransparent = true
        window.toolbarStyle = .unified
        if attachedPreview || renderingPreview { window.center() }
        else {
            if !window.setFrameUsingName("ModelStudioWindow") { window.center() }
            window.setFrameAutosaveName("ModelStudioWindow")
        }
        window.delegate = self
        window.isReleasedWhenClosed = false
        guard let content = window.contentView else { return }
        let sidebar = NSVisualEffectView()
        sidebar.material = .sidebar
        sidebar.blendingMode = .behindWindow
        sidebar.state = .active
        sidebar.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(sidebar)
        let brand = title("Model Deck", size: 21)
        let brandSymbol = NSImageView(image: modelStudioBrandImage())
        brandSymbol.widthAnchor.constraint(equalToConstant: 38).isActive = true
        brandSymbol.heightAnchor.constraint(equalToConstant: 38).isActive = true
        let sidebarContent = column([row([brandSymbol, NSView()]), brand, note("A home for your models")], spacing: 8)
        sidebarContent.translatesAutoresizingMaskIntoConstraints = false
        sidebar.addSubview(sidebarContent)
        let navigation = column([], spacing: 8)
        navigation.translatesAutoresizingMaskIntoConstraints = false
        sidebar.addSubview(navigation)
        for (index, entry) in [("Overview", "square.grid.2x2"), ("Models", "square.stack.3d.up"),
                               ("Connections", "network"), ("Usage", "chart.bar"),
                               ("Advanced", "slider.horizontal.3")].enumerated() {
            let control = StudioNavigationButton(title: entry.0, target: self, action: #selector(navigateStudio(_:)))
            control.tag = index
            control.setButtonType(.toggle)
            control.bezelStyle = .rounded
            control.isBordered = false
            control.font = .systemFont(ofSize: 14, weight: .medium)
            control.image = NSImage(systemSymbolName: entry.1, accessibilityDescription: nil)
            control.imagePosition = .imageLeading
            control.alignment = .left
            control.heightAnchor.constraint(equalToConstant: 42).isActive = true
            navigation.addArrangedSubview(control)
            control.widthAnchor.constraint(equalTo: navigation.widthAnchor).isActive = true
            navigationButtons.append(control)
        }
        let sidebarFootnote = note("Version " + (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? ""))
        sidebarFootnote.translatesAutoresizingMaskIntoConstraints = false
        sidebar.addSubview(sidebarFootnote)
        let pageArea = NSView()
        pageArea.translatesAutoresizingMaskIntoConstraints = false
        standaloneBodyHost.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(standaloneBodyHost)
        sharedBody.addSubview(pageArea)
        feedback.font = .systemFont(ofSize: 12)
        feedback.textColor = .secondaryLabelColor
        feedback.maximumNumberOfLines = 0
        // A slim status line instead of a boxed card: it should read as a footer, not content.
        let footer = NSView()
        let footerRule = NSBox()
        footerRule.boxType = .separator
        footerRule.translatesAutoresizingMaskIntoConstraints = false
        feedback.translatesAutoresizingMaskIntoConstraints = false
        footer.addSubview(footerRule)
        footer.addSubview(feedback)
        NSLayoutConstraint.activate([
            footerRule.leadingAnchor.constraint(equalTo: footer.leadingAnchor),
            footerRule.trailingAnchor.constraint(equalTo: footer.trailingAnchor),
            footerRule.topAnchor.constraint(equalTo: footer.topAnchor),
            feedback.leadingAnchor.constraint(equalTo: footer.leadingAnchor, constant: 4),
            feedback.trailingAnchor.constraint(equalTo: footer.trailingAnchor, constant: -4),
            feedback.topAnchor.constraint(equalTo: footerRule.bottomAnchor, constant: 10),
            feedback.bottomAnchor.constraint(equalTo: footer.bottomAnchor, constant: -6)
        ])
        footerMinimumHeight = footer.heightAnchor.constraint(greaterThanOrEqualToConstant: 30)
        footer.translatesAutoresizingMaskIntoConstraints = false
        sharedBody.addSubview(footer)
        NSLayoutConstraint.activate([
            sidebar.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            sidebar.topAnchor.constraint(equalTo: content.topAnchor),
            sidebar.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            sidebar.widthAnchor.constraint(equalToConstant: 204),
            sidebarContent.leadingAnchor.constraint(equalTo: sidebar.leadingAnchor, constant: 22),
            sidebarContent.trailingAnchor.constraint(equalTo: sidebar.trailingAnchor, constant: -18),
            sidebarContent.topAnchor.constraint(equalTo: sidebar.topAnchor, constant: 34),
            navigation.leadingAnchor.constraint(equalTo: sidebar.leadingAnchor, constant: 14),
            navigation.trailingAnchor.constraint(equalTo: sidebar.trailingAnchor, constant: -14),
            navigation.topAnchor.constraint(equalTo: sidebarContent.bottomAnchor, constant: 34),
            sidebarFootnote.leadingAnchor.constraint(equalTo: sidebar.leadingAnchor, constant: 22),
            sidebarFootnote.trailingAnchor.constraint(equalTo: sidebar.trailingAnchor, constant: -18),
            sidebarFootnote.bottomAnchor.constraint(equalTo: sidebar.bottomAnchor, constant: -24),
            standaloneBodyHost.leadingAnchor.constraint(equalTo: sidebar.trailingAnchor),
            standaloneBodyHost.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            standaloneBodyHost.topAnchor.constraint(equalTo: content.topAnchor),
            standaloneBodyHost.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            pageArea.leadingAnchor.constraint(equalTo: sharedBody.leadingAnchor, constant: 24),
            pageArea.trailingAnchor.constraint(equalTo: sharedBody.trailingAnchor, constant: -24),
            pageArea.topAnchor.constraint(equalTo: sharedBody.topAnchor, constant: 28),
            pageArea.bottomAnchor.constraint(equalTo: footer.topAnchor, constant: -18),
            footer.leadingAnchor.constraint(equalTo: pageArea.leadingAnchor),
            footer.trailingAnchor.constraint(equalTo: pageArea.trailingAnchor),
            footer.bottomAnchor.constraint(equalTo: sharedBody.bottomAnchor, constant: -12),
            footerMinimumHeight!
        ])
        mountSharedBody(in: standaloneBodyHost)
        let pages = [buildOverviewPage(), buildModelsPage(), buildKeysPage(), buildUsagePage(), buildAdvancedPage()]
        for page in pages {
            let scroll = NSScrollView()
            scroll.hasVerticalScroller = true
            scroll.autohidesScrollers = true
            scroll.drawsBackground = false
            scroll.translatesAutoresizingMaskIntoConstraints = false
            let document = StudioPageDocument()
            document.translatesAutoresizingMaskIntoConstraints = false
            scroll.documentView = document
            page.translatesAutoresizingMaskIntoConstraints = false
            document.addSubview(page)
            pageArea.addSubview(scroll)
            studioPages.append(scroll)
            NSLayoutConstraint.activate([
                scroll.leadingAnchor.constraint(equalTo: pageArea.leadingAnchor),
                scroll.trailingAnchor.constraint(equalTo: pageArea.trailingAnchor),
                scroll.topAnchor.constraint(equalTo: pageArea.topAnchor),
                scroll.bottomAnchor.constraint(equalTo: pageArea.bottomAnchor),
                document.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor),
                page.leadingAnchor.constraint(equalTo: document.leadingAnchor),
                page.trailingAnchor.constraint(lessThanOrEqualTo: document.trailingAnchor),
                page.topAnchor.constraint(equalTo: document.topAnchor),
                page.bottomAnchor.constraint(equalTo: document.bottomAnchor)
            ])
            // Text and cards read best under about 720 points; wider windows keep the extra space empty.
            let preferredWidth = page.widthAnchor.constraint(equalTo: document.widthAnchor)
            // Below NSLayoutPriorityWindowSizeStayPut (500), so the user's window size always wins.
            preferredWidth.priority = NSLayoutConstraint.Priority(490)
            preferredWidth.isActive = true
            page.widthAnchor.constraint(lessThanOrEqualToConstant: 720).isActive = true
        }
        setSidebarPresentation(false)
        showStudioPage(0)
    }

    private func savePreferences() throws {
        guard !renderingPreview, !attachedPreview else { throw SettingsError.message("Preview mode does not save settings.") }
        try FileManager.default.createDirectory(at: stateDirectory, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        try JSONEncoder().encode(preferences).write(to: preferencesURL, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: preferencesURL.path)
        // The router reads endpoint settings (name, wire format) from this file, keyed by key account
        // for keyed endpoints and by base URL for keyless ones. It never contains a key.
        var endpoints: [String: [String: String]] = [:]
        for account in preferences.accounts {
            endpoints[account.keyed ? account.id : account.resolvedBaseURL] =
                ["name": account.name, "base_url": account.resolvedBaseURL, "wire": account.resolvedWire]
        }
        let endpointsURL = stateDirectory.appendingPathComponent("endpoints.json")
        try JSONSerialization.data(withJSONObject: endpoints, options: [.prettyPrinted, .sortedKeys]).write(to: endpointsURL, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: endpointsURL.path)
    }

    private func reloadAccounts() {
        accounts.removeAllItems()
        catalogAccounts.removeAllItems()
        if preferences.accounts.isEmpty { accounts.addItem(withTitle: "No connections yet"); catalogAccounts.addItem(withTitle: "Add a connection first") }
        for account in preferences.accounts {
            for picker in [accounts, catalogAccounts] {
                picker.addItem(withTitle: account.name)
                let preset = preset(for: account)
                picker.setSymbol(preset?.symbol ?? (account.isCursor ? "cursorarrow" : account.isOpenRouter ? "arrow.triangle.branch" : "server.rack"),
                                 color: preset.map { StudioPalette.color($0.color) } ?? (account.isCursor ? .labelColor : .systemIndigo), for: account.name)
            }
        }
        if let index = preferences.accounts.firstIndex(where: { $0.id == preferences.selectedAccount }) {
            accounts.selectItem(at: index)
            catalogAccounts.selectItem(at: index)
        }
        updateActiveKeySummary()
    }

    private func currentAccount() -> SavedAccount? {
        let selected = accounts.indexOfSelectedItem
        return preferences.accounts.indices.contains(selected) ? preferences.accounts[selected] : nil
    }

    private func updateActiveKeySummary() {
        if let account = currentAccount() {
            let kind = account.isCursor ? "Cursor SDK" : account.isOpenRouter ? "OpenRouter" : (account.keyed ? "API key" : "no key")
            activeKeySummary.stringValue = preset(for: account)?.billing_note ?? "\(account.name) · \(kind)"
            endpointSummary.stringValue = account.resolvedBaseURL + " · " + (account.keyed ? "key in Keychain" : "no key") + " · " + (account.isCursor ? "Cursor SDK" : account.resolvedWire == "auto" ? "format detected automatically" : account.resolvedWire == "chat" ? "chat completions" : "Responses API")
        } else {
            activeKeySummary.stringValue = "No endpoint selected"
            endpointSummary.stringValue = "Add an endpoint below."
        }
        if !preferences.accounts.contains(where: { $0.id == usageSelectedOpenRouterAccountID && $0.isOpenRouter && !$0.isCursor && $0.keyed }) {
            usageSelectedOpenRouterAccountID = currentUsageAccountID()
        }
        renderUsageDashboard()
        cursorSetupCard?.isHidden = endpointKind.indexOfSelectedItem != 1 && currentAccount()?.isCursor != true
    }

    private func reloadFavorites() {
        favorites.removeAllItems()
        favorites.addItem(withTitle: "Saved shortcuts…")
        favorites.addItems(withTitles: preferences.models)
    }

    private func selectedAccount() throws -> SavedAccount {
        guard let account = currentAccount() else { throw SettingsError.message("Add an endpoint first (Endpoints page).") }
        return account
    }

    private func selectedModel() throws -> String {
        let slug = model.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let openRouter = currentAccount()?.isOpenRouter ?? true
        guard !slug.isEmpty, !slug.contains(where: { $0.isWhitespace || $0.isNewline }), slug.count < 300, !slug.contains("\"") else {
            throw SettingsError.message("Enter a model ID without spaces.")
        }
        guard !openRouter || slug.contains("/") else {
            throw SettingsError.message("Enter an OpenRouter model ID in provider/model-id format.")
        }
        return slug
    }

    private func report(_ message: String, error: Bool = false) {
        feedback.stringValue = message
        feedback.textColor = error ? .systemRed : .secondaryLabelColor
    }

    private func setBusy(_ value: Bool) {
        busy = value
        let blocked = value || registrationInProgress
        actionButtons.forEach { $0.isEnabled = !blocked }
        restoreButton.isEnabled = !blocked && restoreAvailable
        accounts.isEnabled = !blocked
        catalogAccounts.isEnabled = !blocked
        effort.isEnabled = !blocked
        modelLibraryTable.isEnabled = !blocked
        endpointKind.isEnabled = !blocked
        catalogTable.isEnabled = !blocked
        catalogAddButton?.isEnabled = !blocked && !catalogState.selected.isEmpty && currentAccount() != nil
    }

    @objc private func accountChanged() {
        if renderingPreview || attachedPreview { updateActiveKeySummary(); loadCatalog(); return }
        do { preferences.selectedAccount = try selectedAccount().id; try savePreferences() }
        catch { report(error.localizedDescription, error: true) }
        catalogAccounts.selectItem(at: accounts.indexOfSelectedItem)
        updateActiveKeySummary()
        loadCatalog()
    }

    @objc private func saveKey() {
        guard !renderingPreview, !attachedPreview, !busy else { return }
        let name = accountName.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let key = apiKey.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let cursor = endpointKind.indexOfSelectedItem == 1
        var urlText = cursor ? SavedAccount.cursorURL : endpointURL.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        if urlText.isEmpty { urlText = SavedAccount.openRouterURL }
        guard !name.isEmpty, name.count <= 64 else { report("Give this endpoint a short name.", error: true); return }
        guard let url = URL(string: urlText), let scheme = url.scheme?.lowercased(), let host = url.host, ["http", "https"].contains(scheme),
              url.query == nil, !urlText.contains(where: { $0.isWhitespace }) else {
            report("Enter an http(s) base URL such as https://openrouter.ai/api/v1 or http://localhost:1234/v1.", error: true); return
        }
        let candidate = SavedAccount(id: UUID().uuidString, name: name, baseURL: urlText, wire: cursor ? "cursor" : nil, hasKey: nil)
        if scheme == "http", !Self.isLocalHost(host) {
            report("Plain http is only allowed for servers on this Mac or your local network.", error: true); return
        }
        let existing = preferences.accounts.first(where: { $0.name == name })
        if let existing, existing.resolvedBaseURL != candidate.resolvedBaseURL {
            report("That connection name already belongs to a different endpoint. Choose a new name.", error: true); return
        }
        if key.isEmpty {
            if (candidate.isOpenRouter || cursor || endpointKind.indexOfSelectedItem >= 4) && existing?.keyed != true {
                report("Paste this provider’s key before saving the connection.", error: true); return
            }
        } else if candidate.isOpenRouter {
            guard key.hasPrefix("sk-or-"), key.count > 15, !key.contains(where: { $0.isWhitespace }) else {
                report("Paste a valid OpenRouter key starting with sk-or-.", error: true); return
            }
        } else if key.count < 8 || key.contains(where: { $0.isWhitespace }) {
            report("That does not look like an API key.", error: true); return
        }
        var account = existing ?? candidate
        account.baseURL = candidate.isOpenRouter ? nil : candidate.resolvedBaseURL
        account.wire = cursor ? "cursor" : ["auto", "responses", "chat"][max(0, endpointFormat.indexOfSelectedItem)]
        account.hasKey = !key.isEmpty || existing?.keyed == true
        let savedAccount = account
        setBusy(true)
        report("Saving \(account.name)…")
        DispatchQueue.global(qos: .userInitiated).async {
            var failure: String?
            if !key.isEmpty {
                do { try KeychainCredentials.save(key, account: savedAccount.id) }
                catch { failure = error.localizedDescription }
            }
            let message = failure
            DispatchQueue.main.async {
                self.setBusy(false)
                if let message { self.report(message, error: true); return }
                if let index = self.preferences.accounts.firstIndex(where: { $0.id == savedAccount.id }) { self.preferences.accounts[index] = savedAccount }
                else { self.preferences.accounts.append(savedAccount) }
                self.preferences.selectedAccount = savedAccount.id
                do { try self.savePreferences() }
                catch { self.report(error.localizedDescription, error: true); return }
                self.apiKey.stringValue = ""
                self.reloadAccounts()
                self.showStudioPage(1)
                self.setModelBrowserVisible(true)
                self.loadCatalog()
                self.report("\(savedAccount.name) saved. Select the models you want to add.")
            }
        }
    }

    private static func isLocalHost(_ host: String) -> Bool {
        let lowered = host.lowercased()
        if ["localhost", "127.0.0.1", "::1", "0.0.0.0"].contains(lowered) { return true }
        if [".local", ".localhost", ".lan", ".home", ".internal"].contains(where: { lowered.hasSuffix($0) }) { return true }
        let octets = lowered.split(separator: ".").compactMap { Int($0) }
        guard octets.count == 4 else { return false }
        return octets[0] == 10 || (octets[0] == 192 && octets[1] == 168) || (octets[0] == 172 && (16...31).contains(octets[1]))
    }

    @objc private func saveModel() {
        do {
            let slug = try selectedModel()
            if !preferences.models.contains(slug) { preferences.models.append(slug) }
            preferences.selectedModel = slug
            try savePreferences()
            reloadFavorites()
            report("Shortcut saved. Choose Add to Codex to create its native agent registration.")
        } catch { report(error.localizedDescription, error: true) }
    }

    @objc private func favoriteChanged() {
        if favorites.indexOfSelectedItem > 0 { model.stringValue = favorites.titleOfSelectedItem ?? "" }
    }

    @objc private func removeModel() {
        guard favorites.indexOfSelectedItem > 0, let slug = favorites.titleOfSelectedItem else {
            report("Choose a saved shortcut to remove."); return
        }
        do {
            preferences.models.removeAll { $0 == slug }
            try savePreferences()
            reloadFavorites()
            report("Removed the saved shortcut. Codex settings are unchanged.")
        } catch { report(error.localizedDescription, error: true) }
    }

    @objc private func openDashboard() { NSWorkspace.shared.open(URL(string: "https://openrouter.ai/activity")!) }

    // No redirects: never forward an Authorization header to another host.
    @objc private func checkKey() {
        guard !renderingPreview, !attachedPreview, !busy, let account = currentAccount() else { return }
        if currentAccount()?.isCursor == true { loadCursorModels(testOnly: true); return }
        if !account.isOpenRouter {
            authorizeAccount(account) { authorized in
                guard authorized else { return }
                self.configure(["action": "endpoint_models", "account": account.id, "base_url": account.resolvedBaseURL,
                                "wire": account.resolvedWire, "has_key": account.keyed, "executable": Bundle.main.executableURL!.path]) { result in
                    if result["source"] as? String == "suggested" {
                        self.report("\(account.name): catalog unavailable; authentication and inference remain unverified. Suggested IDs are available in Add models.")
                    } else if result["ok"] as? Bool == true {
                        self.report("\(account.name) returned \((result["models"] as? [Any])?.count ?? 0) models. Inference has not been tested.")
                    } else { self.report(result["error"] as? String ?? "Connection check did not complete.", error: true) }
                }
            }
            return
        }
        setBusy(true)
        report("Testing \(account.name)…")
        DispatchQueue.global(qos: .userInitiated).async {
            var key: String?
            do { key = try KeychainCredentials.read(account.id) } catch { }
            let credential = key
            DispatchQueue.main.async {
                guard let credential else { self.setBusy(false); self.report("Authorize this connection’s saved key and try again.", error: true); return }
                var request = URLRequest(url: URL(string: "https://openrouter.ai/api/v1/key")!)
                request.setValue("Bearer \(credential)", forHTTPHeaderField: "Authorization")
                request.timeoutInterval = 20
                self.fetch(request) { bytes, response, error in
                    self.setBusy(false)
                    guard error == nil, response?.statusCode == 200, let bytes,
                          let result = try? JSONSerialization.jsonObject(with: bytes) as? [String: Any], result["data"] != nil else {
                        self.report("OpenRouter’s key check did not succeed (HTTP \(response?.statusCode ?? 0)).", error: true); return
                    }
                    self.report("Key accepted by OpenRouter. Inference has not been tested.")
                }
            }
        }
    }

    @objc private func loadCatalog() {
        guard !registrationInProgress else { return }
        guard !renderingPreview, !attachedPreview else { reloadCatalogRows(); return }
        guard let account = currentAccount() else {
            modelCatalogPresenter.cancel()
            modelCatalogService.cancel()
            _ = catalogState.begin(route: "")
            catalogLoading = false
            catalogProgress.stopAnimation(nil)
            catalogStatus.stringValue = "Save a connection in Connections to start adding models."
            catalogStatus.textColor = .secondaryLabelColor
            catalogRetryButton.isHidden = true
            catalogAuthorizeButton.isHidden = true
            reloadCatalogRows()
            return
        }
        let route = account.id + "|" + account.resolvedBaseURL + "|" + account.resolvedWire
        let changed = catalogState.route != route
        if changed { catalogSearch.stringValue = ""; catalogFailures = [:]; catalogAdded = [] }
        let generation = catalogState.begin(route: route)
        catalogLoading = true
        catalogProgress.startAnimation(nil)
        catalogStatus.stringValue = "Loading \(account.name) models…"
        catalogStatus.textColor = .secondaryLabelColor
        catalogRetryButton.isHidden = true
        catalogAuthorizeButton.isHidden = true
        updateCatalogAddedModels()
        let connection = ModelCatalogConnectionRequest(
            accountID: account.id,
            accountName: account.name,
            baseURL: account.resolvedBaseURL,
            wire: account.resolvedWire,
            hasKey: account.keyed,
            isCursor: account.isCursor,
            executablePath: Bundle.main.executableURL!.path
        )
        modelCatalogPresenter.load(connection: connection, service: modelCatalogService) { outcome in
            guard outcome.applied else { return }
            if let models = outcome.models {
                _ = self.catalogState.receive(models, generation: generation)
            }
            self.catalogLoading = false
            self.catalogProgress.stopAnimation(nil)
            self.catalogStatus.stringValue = outcome.statusMessage
            switch outcome.phase {
            case .ready:
                self.catalogStatus.textColor = .secondaryLabelColor
                self.catalogRetryButton.isHidden = true
                self.catalogAuthorizeButton.isHidden = !account.keyed
            case .empty:
                self.catalogStatus.textColor = .secondaryLabelColor
                self.catalogRetryButton.isHidden = false
                self.catalogAuthorizeButton.isHidden = !account.keyed
            case .failure, .unavailable:
                self.catalogStatus.textColor = .systemOrange
                self.catalogRetryButton.isHidden = false
                self.catalogAuthorizeButton.isHidden = !account.keyed
            case .loading, .idle:
                break
            }
            if outcome.suggested {
                self.catalogStatus.textColor = .systemOrange
                self.catalogRetryButton.isHidden = false
            }
            self.reloadCatalogRows()
        }
    }

    private func preset(for account: SavedAccount) -> ProviderPreset? {
        providerPresets.first { $0.base_url.trimmingCharacters(in: CharacterSet(charactersIn: "/")) == account.resolvedBaseURL }
    }

    private func updateCatalogAddedModels() {
        guard let account = currentAccount() else { catalogAdded = []; reloadCatalogRows(); return }
        catalogAdded = Set(registeredModelRecords.compactMap { record -> String? in
            guard record["endpoint_base_url"] as? String == account.resolvedBaseURL,
                  (record["endpoint_account"] as? String ?? "") == (account.keyed ? account.id : ""),
                  (record["endpoint_wire"] as? String ?? "auto") == account.resolvedWire else { return nil }
            return record["model"] as? String
        })
        catalogState.selected.subtract(catalogAdded)
        reloadCatalogRows()
    }

    private func authorizeAccount(_ account: SavedAccount, completion: @escaping (Bool) -> Void) {
        guard !renderingPreview, !attachedPreview else { completion(false); return }
        guard account.keyed else { completion(true); return }
        setBusy(true)
        report("Authorize access to the saved key for \(account.name) if macOS asks.")
        DispatchQueue.global(qos: .userInitiated).async {
            var failure: String?
            do { _ = try KeychainCredentials.read(account.id) }
            catch { failure = error.localizedDescription }
            let message = failure
            DispatchQueue.main.async {
                self.setBusy(false)
                if let message { self.report(message, error: true); completion(false) }
                else { completion(true) }
            }
        }
    }

    @objc private func authorizeCatalogConnection() {
        guard let account = currentAccount() else { return }
        authorizeAccount(account) { ok in if ok && self.currentAccount()?.id == account.id { self.loadCatalog() } }
    }

    private func currentUsageAccountID() -> String {
        // Usage selection is independent of the endpoint used to add models.
        let eligible = preferences.accounts.filter { $0.isOpenRouter && !$0.isCursor && $0.keyed }
        return eligible.first { $0.id == usageSelectedOpenRouterAccountID }?.id
            ?? eligible.first { $0.id == preferences.selectedAccount }?.id
            ?? eligible.first?.id ?? ""
    }

    @objc private func refreshUsage() { performUsageRefresh(quiet: false) }

    private func performUsageRefresh(quiet: Bool) {
        guard !attachedPreview, !renderingPreview, !usageRefreshInFlight, !(quiet && busy) else { return }
        usageRefreshInFlight = true
        let accountID = currentUsageAccountID()
        usageEntries = routerLedgerEntries(limit: 10_000)
        usageRefreshMessage = nil
        renderUsageDashboard()
        DispatchQueue.global(qos: .userInitiated).async {
            var result: [String: Any]
            do {
                let process = Process()
                let input = Pipe(), output = Pipe()
                let buffer = UsageOutputBuffer()
                let finished = DispatchSemaphore(value: 0)
                let drained = DispatchSemaphore(value: 0)
                process.executableURL = URL(fileURLWithPath: Bundle.main.object(forInfoDictionaryKey: "PythonExecutable") as? String ?? "/opt/homebrew/bin/python3")
                process.arguments = ["-B", Bundle.main.resourceURL!.appendingPathComponent("provider_usage.py").path]
                process.standardInput = input
                process.standardOutput = output
                process.standardError = FileHandle.nullDevice
                process.terminationHandler = { _ in finished.signal() }
                output.fileHandleForReading.readabilityHandler = { handle in
                    let bytes = handle.availableData
                    if bytes.isEmpty { handle.readabilityHandler = nil; drained.signal() }
                    else { buffer.append(bytes) }
                }
                defer { output.fileHandleForReading.readabilityHandler = nil }
                try process.run()
                input.fileHandleForWriting.write(try JSONSerialization.data(withJSONObject: ["account_id": accountID]))
                input.fileHandleForWriting.closeFile()
                if finished.wait(timeout: .now() + 34) == .timedOut {
                    process.terminate()
                    if finished.wait(timeout: .now() + 0.5) == .timedOut {
                        Darwin.kill(process.processIdentifier, SIGKILL)
                    }
                    throw SettingsError.message("Usage refresh timed out. Try again.")
                }
                guard drained.wait(timeout: .now() + 1) == .success,
                      let bytes = buffer.snapshot(),
                      let parsed = try JSONSerialization.jsonObject(with: bytes) as? [String: Any] else {
                    throw SettingsError.message("Usage response was unavailable or exceeded its size limit.")
                }
                result = parsed
            } catch {
                let failure: [String: Any] = ["ok": false, "error": "Usage refresh did not complete. Try Refresh again."]
                result = ["openai": failure, "openrouter": failure]
            }
            let finalResult = result
            DispatchQueue.main.async {
                self.usageRefreshInFlight = false
                self.usageEntries = self.routerLedgerEntries(limit: 10_000)
                // A picker change during an in-flight read must never display another key's totals.
                guard self.currentUsageAccountID() == accountID else {
                    self.renderUsageDashboard()
                    self.performUsageRefresh(quiet: false)
                    return
                }
                let subscription = finalResult["openai"] as? [String: Any] ?? [:]
                let openRouter = finalResult["openrouter"] as? [String: Any] ?? [:]
                var failures: [String] = []
                if subscription["ok"] as? Bool == true {
                    self.usageSubscription = subscription
                } else {
                    if self.usageSubscription["ok"] as? Bool != true { self.usageSubscription = subscription }
                    failures.append("ChatGPT allowance could not refresh")
                }
                if openRouter["ok"] as? Bool == true || self.usageOpenRouterAccountID != accountID || self.usageOpenRouter["ok"] as? Bool != true {
                    self.usageOpenRouter = openRouter
                    self.usageOpenRouterAccountID = accountID
                }
                if !accountID.isEmpty && openRouter["ok"] as? Bool != true { failures.append("OpenRouter spending could not refresh") }
                if failures.isEmpty { self.usageRefreshedAt = Date() }
                self.usageRefreshMessage = failures.isEmpty ? nil : failures.joined(separator: "; ") + ". Previous figures are kept when available."
                self.renderUsageDashboard()
            }
        }
    }

    private func fetch(_ request: URLRequest, completion: @escaping (Data?, HTTPURLResponse?, Error?) -> Void) {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        let session = URLSession(configuration: configuration, delegate: RejectRedirects(), delegateQueue: nil)
        session.dataTask(with: request) { data, response, error in
            session.finishTasksAndInvalidate()
            DispatchQueue.main.async { completion(data, response as? HTTPURLResponse, error) }
        }.resume()
    }

    private func configure(_ request: [String: Any], blocksUI: Bool = true, completion: @escaping ([String: Any]) -> Void) {
        guard !renderingPreview, !attachedPreview else {
            completion(["ok": false, "error": "Preview mode does not contact providers or save settings."])
            return
        }
        if blocksUI { setBusy(true) }
        DispatchQueue.global(qos: .userInitiated).async {
            var result: [String: Any]
            do {
                let process = Process()
                let input = Pipe(), output = Pipe(), errors = Pipe()
                process.executableURL = URL(fileURLWithPath: Bundle.main.object(forInfoDictionaryKey: "PythonExecutable") as? String ?? "/opt/homebrew/bin/python3")
                process.arguments = [Bundle.main.resourceURL!.appendingPathComponent("codex_settings.py").path]
                var environment = ProcessInfo.processInfo.environment
                environment["PYTHONPATH"] = Bundle.main.resourceURL!.appendingPathComponent("vendor").path
                environment["PYTHONDONTWRITEBYTECODE"] = "1"
                process.environment = environment
                process.standardInput = input; process.standardOutput = output; process.standardError = errors
                try process.run()
                input.fileHandleForWriting.write(try JSONSerialization.data(withJSONObject: request))
                try input.fileHandleForWriting.close()
                let bytes = output.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                result = (try? JSONSerialization.jsonObject(with: bytes) as? [String: Any]) ?? ["ok": false, "error": "Could not read Codex settings. The Python runtime or app resources may be unavailable."]
            } catch { result = ["ok": false, "error": "Could not start the settings writer. Check that the app and its Python runtime are installed."] }
            let finalResult = result
            DispatchQueue.main.async { if blocksUI { self.setBusy(false) }; completion(finalResult) }
        }
    }

    private func displayStatus(_ result: [String: Any]) {
        if result["ok"] as? Bool == true {
            status.stringValue = "Codex on disk: \(result["provider"] as? String ?? "openai")  ·  \(result["model"] as? String ?? "default")"
            restoreAvailable = result["can_restore"] as? Bool ?? false
            restoreButton.isEnabled = !busy && restoreAvailable
        } else { report(result["error"] as? String ?? "Unable to read Codex settings.", error: true) }
    }

    private func refreshStatus() { configure(["action": "status"]) { self.displayStatus($0) } }

    @objc private func registerAgent() {
        guard !renderingPreview, !attachedPreview, !busy, !registrationInProgress,
              let account = currentAccount(), !catalogState.selected.isEmpty else { return }
        let models = catalogState.selected.sorted()
        let selectedEffort = effort.indexOfSelectedItem == 0 ? "default" : effort.titleOfSelectedItem ?? "default"
        registrationInProgress = true
        catalogFailures = [:]
        setBusy(true)
        authorizeAccount(account) { authorized in
            guard authorized else {
                self.registrationInProgress = false
                self.setBusy(false)
                return
            }
            self.registerBatch(models, index: 0, account: account, effort: selectedEffort, added: 0, skipped: 0)
        }
    }

    private func registerBatch(_ models: [String], index: Int, account: SavedAccount, effort: String, added: Int, skipped: Int) {
        guard index < models.count else {
            registrationInProgress = false
            setBusy(false)
            let failed = catalogFailures.count
            let summary = "\(added) added" + (skipped > 0 ? " · \(skipped) already added" : "") + (failed > 0 ? " · \(failed) need attention" : "")
            catalogStatus.stringValue = summary + (failed > 0 ? ". Failed models stay selected. Hover a marked row for details.\n" + models.compactMap { id in catalogFailures[id].map { id + ": " + $0 } }.prefix(6).joined(separator: "\n") : ". Ready to choose in Codex.")
            catalogStatus.textColor = failed > 0 ? .systemOrange : .secondaryLabelColor
            report(summary + " on \(account.name).", error: failed > 0)
            if added > 0 {
                companionRestartRequired = true
                refreshCompanionVisibility()
                preferences.selectedAccount = account.id
                if let last = models.last { preferences.selectedModel = last }
                do { try savePreferences() } catch { report(error.localizedDescription, error: true) }
            }
            reloadCatalogRows()
            refreshModelInventory()
            return
        }
        let slug = models[index]
        if catalogAdded.contains(slug) {
            catalogState.selected.remove(slug)
            registerBatch(models, index: index + 1, account: account, effort: effort, added: added, skipped: skipped + 1)
            return
        }
        catalogStatus.stringValue = "Adding \(index + 1) of \(models.count): \(slug)…"
        var request: [String: Any] = ["action": "register_agent", "model": slug,
            "executable": Bundle.main.executableURL!.path, "base_url": account.resolvedBaseURL,
            "endpoint_name": account.name, "wire": account.resolvedWire, "effort": effort]
        if account.keyed { request["account"] = account.id }
        configure(request) { result in
            let succeeded = result["ok"] as? Bool == true
            let alreadyAdded = result["already_registered"] as? Bool == true
            if succeeded {
                self.catalogState.selected.remove(slug)
                self.catalogAdded.insert(slug)
            } else { self.catalogFailures[slug] = result["error"] as? String ?? "Registration did not complete. Try again." }
            self.reloadCatalogRows()
            self.registerBatch(models, index: index + 1, account: account, effort: effort,
                               added: added + (succeeded && !alreadyAdded ? 1 : 0), skipped: skipped + (succeeded && alreadyAdded ? 1 : 0))
        }
    }

    @objc private func restoreSettings() {
        configure(["action": "restore"]) { result in
            self.displayStatus(result)
            if result["ok"] as? Bool == true { self.report("Previous provider settings restored. Fully quit and reopen Codex for new tasks to use them.") }
        }
    }

    @objc private func launchIntegratedCodex() {
        configure(["action": "runtime_info"]) { result in
            guard result["ok"] as? Bool == true,
                  let runtime = result["runtime"] as? [String: Any],
                  let applicationPath = runtime["application_path"] as? String,
                  let bundleIdentifier = runtime["bundle_identifier"] as? String else {
                self.report(result["error"] as? String ?? "Could not locate the current ChatGPT/Codex application.", error: true)
                return
            }
            if NSWorkspace.shared.runningApplications.contains(where: { $0.bundleIdentifier == bundleIdentifier }) {
                self.showCompanion()
                self.report("Companion opened beside your workspace. Codex is already running; its integration state is unknown. If added models are missing, manually quit and relaunch through Model Deck. Running tasks were not changed.")
                return
            }
            let launcher = Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/CodexProviderBridge")
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/open")
            process.arguments = ["-a", applicationPath, "--env", "CODEX_CLI_PATH=" + launcher.path]
            process.standardOutput = FileHandle.nullDevice
            process.standardError = FileHandle.nullDevice
            process.terminationHandler = { completed in
                DispatchQueue.main.async {
                    guard completed.terminationStatus == 0 else {
                        self.report("The application launcher did not complete successfully. Your restart reminder has been kept.", error: true)
                        return
                    }
                    self.companionLaunchRequested = true
                    self.companionRestartRequired = false
                    self.showCompanion()
                    let name = URL(fileURLWithPath: applicationPath).deletingPathExtension().lastPathComponent
                    self.report("Requested integrated launch of \(name) and opened the companion. Existing tasks keep their provider; start a new task to choose a different provider.")
                }
            }
            do {
                try process.run()
            } catch {
                self.report("Could not launch the resolved ChatGPT/Codex application. Check that it is still installed.", error: true)
            }
        }
    }
}

final class RejectRedirects: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

if CommandLine.arguments.count == 2 && CommandLine.arguments[1] == "--self-test-companion" {
    var failures: [String] = []
    func expect(_ passed: Bool, _ label: String) { if !passed { failures.append(label) } }
    let original = NSRect(x: -1000, y: 100, width: 1200, height: 700)
    var fake = original
    let write: (NSRect) -> WindowReservation.WriteReceipt = { fake = $0; return .init(mutated: true, complete: true) }
    let expanded = WindowReservation.change(current: fake, reserved: 0, desired: 600, read: { fake }, write: write)
    expect(expanded.accepted?.width == 600 && fake.height == original.height, "reserve expanded space")
    let collapsed = WindowReservation.change(current: fake, reserved: 600, desired: 48, read: { fake }, write: write)
    expect(collapsed.accepted?.width == 1152 && fake.maxX + 48 == original.maxX, "collapse returns 552 pixels")
    _ = WindowReservation.change(current: fake, reserved: 48, desired: 0, read: { fake }, write: write)
    expect(fake == original, "release restores current owned footprint")
    fake = original
    let partial = WindowReservation.change(current: original, reserved: 0, desired: 600, read: { fake }, write: {
        fake = $0
        return .init(mutated: true, complete: $0 == original)
    })
    expect(partial.accepted == nil && fake == original, "known partial write rolls back")
    fake = original
    let clamped = WindowReservation.change(current: original, reserved: 0, desired: 600, read: { fake }, write: {
        fake = NSRect(x: $0.minX, y: $0.minY, width: max(768, $0.width), height: $0.height)
        return .init(mutated: true, complete: true)
    })
    expect(clamped.accepted == nil && fake == original, "minimum width refusal rolls back")
    fake = original
    var noMutationCalls = 0
    _ = WindowReservation.change(current: original, reserved: 0, desired: 600, read: { fake }, write: { _ in
        noMutationCalls += 1
        fake.origin.x += 100
        return .init(mutated: false, complete: false)
    })
    expect(noMutationCalls == 1 && fake.minX == original.minX + 100, "no rollback of unrelated user change")
    var calls = 0
    _ = WindowReservation.change(current: original, reserved: 0, desired: 600, read: { fake }, write: { _ in
        calls += 1
        return .init(mutated: true, complete: true)
    })
    expect(calls == 0, "stale observed frame prevents write")
    fake = original
    let generation = TrackingGeneration()
    let admitted = generation.advance()
    let canceled = WindowReservation.change(current: original, reserved: 0, desired: 600, read: { fake }, write: {
        fake = $0
        _ = generation.advance()
        return .init(mutated: true, complete: true)
    })
    expect(!generation.matches(admitted) && canceled.accepted == fake, "admitted canceled transaction keeps accepted ownership")
    _ = WindowReservation.change(current: fake, reserved: 600, desired: 0, read: { fake }, write: write)
    expect(fake == original, "queued cancellation cleanup restores owned space")
    fake = original
    var reads = 0
    let unavailable = WindowReservation.change(current: original, reserved: 0, desired: 600, read: {
        reads += 1
        return reads == 1 ? fake : nil
    }, write: write)
    expect(unavailable.accepted == nil && unavailable.pendingOwnedFrame != nil, "readback failure is unverified with guarded cleanup candidate")
    let recovery = WindowReservationRecovery<Int>(sameWindow: { $0 == $1 })
    recovery.recordFailure(window: 1, current: original, priorReserved: 0, outcome: unavailable)
    expect(recovery.owns(1) && recovery.blocksPolling(1), "failed owned window cannot appear attached on next poll")
    var recoveryWrites = 0
    let recoveryWrite: (Int, NSRect) -> WindowReservation.WriteReceipt = { _, frame in
        recoveryWrites += 1
        fake = frame
        return .init(mutated: true, complete: true)
    }
    expect(recovery.release(read: { _ in nil }, write: recoveryWrite, allowed: { true }) == .pending,
           "unreadable cleanup remains pending")
    expect(recovery.owns(1) && recovery.ownership?.reservedWidth == 600, "unreadable cleanup retains reserved ownership")
    expect(recovery.release(read: { _ in fake }, write: recoveryWrite, allowed: { false }) == .pending && recoveryWrites == 0,
           "cancelled cleanup retains ownership without writes")
    let deniedWrite: (Int, NSRect) -> WindowReservation.WriteReceipt = { _, _ in .init(mutated: false, complete: false) }
    let switchCanProceed = recovery.release(read: { _ in fake }, write: deniedWrite, allowed: { true }) != .pending
    expect(!switchCanProceed && recovery.owns(1) && !recovery.owns(2) && recovery.blocksPolling(2),
           "failed old window cleanup blocks replacement ownership")
    expect(recovery.release(read: { _ in fake }, write: recoveryWrite, allowed: { true }) == .released && fake == original,
           "explicit same window cleanup retry restores retained space")
    recovery.recordFailure(window: 1, current: fake, priorReserved: 0, outcome: .init(accepted: nil, pendingOwnedFrame: nil))
    expect(recovery.ownership == nil && recovery.blocksPolling(1), "cleanup completed requires explicit retry, not automatic poll attachment")
    fake.size.width = 600
    recovery.recordSuccess(window: 1, frame: fake, reserved: 600)
    expect(!recovery.blocksPolling(1), "successful explicit attachment clears failure block")
    var partialCleanupWrites = 0
    let partialCleanup = recovery.release(read: { _ in fake }, write: { _, _ in
        partialCleanupWrites += 1
        if partialCleanupWrites == 1 {
            fake.size.width = 900
            return .init(mutated: true, complete: false)
        }
        return .init(mutated: false, complete: false)
    }, allowed: { true })
    expect(partialCleanup == .pending && recovery.ownership?.reservedWidth == 300 && recovery.ownership?.frame.width == 900,
           "partial cleanup reconciles actual remaining reservation")
    expect(recovery.release(read: { _ in fake }, write: recoveryWrite, allowed: { true }) == .released && fake == original,
           "partial cleanup retry restores only remaining space")
    fake.size.width = 600
    recovery.recordSuccess(window: 1, frame: fake, reserved: 600)
    var cleanupReads = 0
    let missingCleanupReadback = recovery.release(read: { _ in
        cleanupReads += 1
        return cleanupReads <= 2 ? fake : nil
    }, write: recoveryWrite, allowed: { true })
    expect(missingCleanupReadback == .pending && recovery.owns(1), "cleanup failed readback keeps cleanup candidate")
    let writesBeforeReconcile = recoveryWrites
    expect(recovery.release(read: { _ in fake }, write: recoveryWrite, allowed: { true }) == .released && recoveryWrites == writesBeforeReconcile,
           "verified fully restored candidate clears without double expansion")
    fake.size.width = 600
    recovery.recordSuccess(window: 1, frame: fake, reserved: 600)
    cleanupReads = 0
    _ = recovery.release(read: { _ in
        cleanupReads += 1
        return cleanupReads <= 2 ? fake : nil
    }, write: { _, _ in .init(mutated: true, complete: false) }, allowed: { true })
    expect(recovery.release(read: { _ in fake }, write: recoveryWrite, allowed: { true }) == .released && fake == original,
           "unverified candidate can reconcile prior frame before retry")
    fake.size.width = 600
    recovery.recordSuccess(window: 1, frame: fake, reserved: 600)
    fake.origin.x += 50
    let beforeManualRelease = recoveryWrites
    expect(recovery.release(read: { _ in fake }, write: recoveryWrite, allowed: { true }) == .manualChange && recovery.ownership == nil && recoveryWrites == beforeManualRelease,
           "manual geometry change relinquishes without stale restore")
    fake = original
    fake.size.width = 600
    recovery.recordSuccess(window: 1, frame: fake, reserved: 600)
    var cleanupAllowed = true
    let admittedCleanup = recovery.release(read: { _ in fake }, write: { _, frame in
        fake = frame
        cleanupAllowed = false
        return .init(mutated: true, complete: true)
    }, allowed: { cleanupAllowed })
    expect(admittedCleanup == .released && recovery.ownership == nil && fake == original,
           "admitted cleanup completes and records result despite later cancellation")
    expect(CompanionGeometry.appKitFrame(position: CGPoint(x: -1200, y: -200), size: CGSize(width: 900, height: 300), primaryTop: 900).minY == 800, "display above primary conversion")
    expect(CompanionGeometry.appKitFrame(position: CGPoint(x: -1200, y: 1000), size: CGSize(width: 900, height: 300), primaryTop: 900).minY == -400, "display below primary conversion")
    let own = "com.cooper.model-deck"
    for active in ["com.openai.codex", own] {
        expect(CompanionGeometry.shouldTrack(enabled: true, trusted: true, activeBundle: active, ownBundle: own, hostAvailable: true, hidden: false, minimized: false), "host/self visibility")
    }
    expect(!CompanionGeometry.shouldTrack(enabled: false, trusted: true, activeBundle: own, ownBundle: own, hostAvailable: true, hidden: false, minimized: false), "explicit opt-in required")
    expect(!CompanionGeometry.shouldTrack(enabled: true, trusted: false, activeBundle: own, ownBundle: own, hostAvailable: true, hidden: false, minimized: false), "permission required")
    expect(!CompanionGeometry.shouldTrack(enabled: true, trusted: true, activeBundle: "other", ownBundle: own, hostAvailable: true, hidden: false, minimized: false), "unrelated app hidden")
    expect(!CompanionGeometry.shouldTrack(enabled: true, trusted: true, activeBundle: own, ownBundle: own, hostAvailable: true, hidden: true, minimized: false), "hidden host rejected")
    expect(!CompanionGeometry.shouldTrack(enabled: true, trusted: true, activeBundle: own, ownBundle: own, hostAvailable: true, hidden: false, minimized: true), "minimized host rejected")
    print(failures.isEmpty ? "PASS: synthetic reservation, cancellation, rollback, coordinates, and visibility." : "FAIL: " + failures.joined(separator: ", "))
    exit(failures.isEmpty ? 0 : 1)
}

if CommandLine.arguments.contains("--self-test-model-browser") {
    var failures: [String] = []
    func expect(_ condition: Bool, _ name: String) { if !condition { failures.append(name) } }
    var browser = ModelBrowserState()
    let first = browser.begin(route: "account-one")
    let fixture = [CatalogModel(id: "kimi-fast", name: "Kimi Coding Fast", suggested: false),
                   CatalogModel(id: "kimi-think", name: "Kimi Reasoning", suggested: false),
                   CatalogModel(id: "kimi-fast", name: "Duplicate", suggested: false)]
    expect(browser.receive(fixture, generation: first), "current catalog accepted")
    expect(browser.entries.count == 2, "duplicate IDs collapse")
    browser.toggle("kimi-fast")
    browser.query = "KIMI reasoning"
    expect(browser.visible.map(\.id) == ["kimi-think"], "case insensitive multi-term substring search")
    expect(browser.selected == ["kimi-fast"], "hidden selection survives filtering")
    browser.selected.formUnion(browser.visible.map(\.id))
    expect(browser.selected.count == 2, "select shown preserves previous filters")
    let refresh = browser.begin(route: "account-one")
    expect(browser.selected.count == 2, "same route refresh preserves selection")
    expect(!browser.receive([], generation: first), "stale same-route response rejected")
    browser.selected.remove("kimi-fast")
    expect(browser.selected == ["kimi-think"], "successful batch item removed while failed item remains")
    let next = browser.begin(route: "account-two")
    expect(browser.selected.isEmpty && browser.entries.isEmpty && browser.query.isEmpty, "connection change resets scope")
    expect(!browser.receive(fixture, generation: refresh), "previous connection response rejected")
    expect(browser.receive([], generation: next), "empty current catalog accepted")
    expect(CatalogModel.matches("cafe", text: "Café"), "diacritic insensitive search")
    print(failures.isEmpty ? "PASS: model browser search, multi-select, refresh, stale responses, batch retention, and route isolation." : "FAIL: " + failures.joined(separator: ", "))
    exit(failures.isEmpty ? 0 : 1)
}

if CommandLine.arguments.contains("--self-test-usage") {
    exit(UsageDashboardView.selfTest() ? 0 : 1)
}

MainActor.assumeIsolated {
    let application = NSApplication.shared
    let delegate = OpenRouterSettingsApp()
    application.setActivationPolicy(.regular)
    application.delegate = delegate
    application.run()
}

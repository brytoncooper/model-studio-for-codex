import AppKit
import Darwin

struct SavedAccount: Codable {
    var id: String
    var name: String
}

struct SavedPreferences: Codable {
    var accounts: [SavedAccount] = []
    var models: [String] = []
    var selectedAccount: String = ""
    var selectedModel: String = "openai/gpt-6-astra"
}

enum SettingsError: LocalizedError {
    case message(String)
    var errorDescription: String? {
        switch self { case .message(let message): return message }
    }
}

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
        FileHandle.standardError.write(Data("OpenRouter Keychain access failed. Open OpenRouter Settings to check your saved key.\n".utf8))
        exit(1)
    }
}

final class StudioCard: NSView {
    override func draw(_ dirtyRect: NSRect) {
        let shape = NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), xRadius: 14, yRadius: 14)
        NSColor.controlBackgroundColor.setFill()
        shape.fill()
        NSColor.separatorColor.withAlphaComponent(0.35).setStroke()
        shape.stroke()
    }
    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        needsDisplay = true
    }
}

final class StudioPageDocument: NSView {
    override var isFlipped: Bool { true }
}

final class StudioNavigationButton: NSButton {
    override func draw(_ dirtyRect: NSRect) {
        if state == .on {
            NSColor.controlAccentColor.withAlphaComponent(0.14).setFill()
            NSBezierPath(roundedRect: bounds.insetBy(dx: 1, dy: 1), xRadius: 9, yRadius: 9).fill()
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

final class OpenRouterSettingsApp: NSObject, NSApplicationDelegate, NSWindowDelegate, NSTableViewDataSource, NSTableViewDelegate {
    private var window: NSWindow!
    private let accounts = NSPopUpButton()
    private let accountName = NSTextField()
    private let apiKey = NSSecureTextField()
    private let model = NSComboBox()
    private let favorites = NSPopUpButton()
    private let effort = NSPopUpButton()
    private let status = NSTextField(wrappingLabelWithString: "Loading Codex settings…")
    private let feedback = NSTextField(wrappingLabelWithString: "Choose a page to manage your models, keys, or usage.")
    private let activeKeySummary = NSTextField(wrappingLabelWithString: "No API key selected")
    private let subscriptionUsageSummary = NSTextField(wrappingLabelWithString: "Usage limits have not been loaded.")
    private let openRouterUsageSummary = NSTextField(wrappingLabelWithString: "Selected-key spending has not been loaded.")
    private let usageUpdatedAt = NSTextField(wrappingLabelWithString: "Not refreshed yet")
    private let subscriptionUsageWindows = NSStackView()
    private let openRouterUsageScope = NSTextField(wrappingLabelWithString: "No API key selected")
    private let openRouterUsageDetails = NSStackView()
    private let tokenActivityDetails = NSStackView()
    private let modelLibraryTable = NSTableView()
    private let modelSearch = NSSearchField()
    private let modelProviderFilter = NSPopUpButton()
    private let modelDetailTitle = NSTextField(wrappingLabelWithString: "Select a model to preview its connection")
    private let modelDetailDescription = NSTextField(wrappingLabelWithString: "Selection here previews a model. Choose it inside Codex to start using it.")
    private var modelCopyButton: NSButton!
    private var addModelForm: NSView!
    private let registeredModelsSummary = NSTextField(wrappingLabelWithString: "Model inventory has not been loaded.")
    private var registeredModelRecords: [[String: Any]] = []
    private var visibleModelRecords: [[String: Any]] = []
    private var modelsInventoryLoaded = false
    private var reasoningDetails: NSView!
    private var studioPages: [NSView] = []
    private var navigationButtons: [NSButton] = []
    private var actionButtons: [NSButton] = []
    private var preferences = SavedPreferences()
    private var catalog: [String] = []
    private var busy = false
    private var restoreAvailable = false
    private var restoreButton: NSButton!
    private let stateDirectory = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/Codex OpenRouter", isDirectory: true)
    private var preferencesURL: URL { stateDirectory.appendingPathComponent("preferences.json") }

    func applicationDidFinishLaunching(_ notification: Notification) {
        do {
            if FileManager.default.fileExists(atPath: preferencesURL.path) {
                preferences = try JSONDecoder().decode(SavedPreferences.self, from: Data(contentsOf: preferencesURL))
            }
        } catch { feedback.stringValue = "Saved preferences could not be loaded. Your Keychain keys and Codex settings have not been changed." }
        buildMenu()
        buildWindow()
        reloadAccounts()
        reloadFavorites()
        model.stringValue = preferences.selectedModel
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        if CommandLine.arguments.count == 3 && CommandLine.arguments[1] == "--render-preview" {
            // Development-only snapshot of our own NSView tree, without reading the desktop.
            DispatchQueue.main.async {
                let content = self.window.contentView!
                content.layoutSubtreeIfNeeded()
                guard let bitmap = content.bitmapImageRepForCachingDisplay(in: content.bounds) else { exit(1) }
                content.cacheDisplay(in: content.bounds, to: bitmap)
                guard let png = bitmap.representation(using: .png, properties: [:]) else { exit(1) }
                do {
                    try png.write(to: URL(fileURLWithPath: CommandLine.arguments[2]))
                    print("Rendered app content at \(Int(content.bounds.width)) × \(Int(content.bounds.height)).")
                    NSApp.terminate(nil)
                } catch { exit(1) }
            }
            return
        }
        refreshStatus()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    private func buildMenu() {
        let menu = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Quit OpenRouter Settings", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
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
        return label
    }

    private func button(_ text: String, _ action: Selector) -> NSButton {
        let button = NSButton(title: text, target: self, action: action)
        button.bezelStyle = .rounded
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
        NSLayoutConstraint.activate([
            content.leadingAnchor.constraint(equalTo: background.leadingAnchor, constant: 20),
            content.trailingAnchor.constraint(equalTo: background.trailingAnchor, constant: -20),
            content.topAnchor.constraint(equalTo: background.topAnchor, constant: 20),
            content.bottomAnchor.constraint(equalTo: background.bottomAnchor, constant: -20)
        ])
        return background
    }

    private func pageHeading(_ heading: String, subtitle: String) -> NSView {
        let headingLabel = title(heading, size: 30)
        headingLabel.font = .systemFont(ofSize: 30, weight: .bold)
        return column([headingLabel, note(subtitle)], spacing: 8)
    }

    private func primaryButton(_ text: String, action: Selector, symbol: String) -> NSButton {
        let control = button(text, action)
        control.controlSize = .large
        control.bezelColor = .controlAccentColor
        control.image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)
        control.imagePosition = .imageLeading
        return control
    }

    private func buildOverviewPage() -> NSView {
        let hero = NSTextField(wrappingLabelWithString: "Your models.\nOne workspace.")
        hero.font = .systemFont(ofSize: 36, weight: .bold)
        let subscription = card([
            title("OpenAI", size: 17), title("→ Subscription", size: 20),
            note("Your existing Codex connection for OpenAI models.")
        ])
        let router = card([
            title("OpenRouter", size: 17), title("→ API credits", size: 20),
            note("Bring other models into Codex with your own API key.")
        ])
        let connections = row([subscription, router])
        connections.distribution = .fillEqually
        connections.alignment = .top
        subscription.heightAnchor.constraint(equalTo: router.heightAnchor).isActive = true
        let launch = primaryButton("Launch Codex", action: #selector(launchIntegratedCodex), symbol: "arrow.up.right.square")
        status.font = .systemFont(ofSize: 13, weight: .medium)
        status.textColor = .secondaryLabelColor
        return column([
            hero,
            note("A little studio for choosing the models you work with in Codex."),
            connections,
            card([
                title("Ready when you are", size: 18),
                note("Quit ChatGPT/Codex completely first, then launch it here to include registered models in its model picker."),
                row([launch]),
                note("Opening Codex normally leaves this integration off. Start a new task when changing providers.")
            ]),
            card([title("Default settings on disk", size: 14), status,
                  note("This shows saved configuration, not the connection of an active task.")])
        ], spacing: 22)
    }

    private func buildModelsPage() -> NSView {
        model.addItems(withObjectValues: preferences.models)
        model.completes = true
        model.numberOfVisibleItems = 12
        model.placeholderString = "e.g. qwen/qwen3.8-27b"
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
        modelProviderFilter.addItems(withTitles: ["All providers", "OpenAI", "OpenRouter"])
        modelProviderFilter.target = self
        modelProviderFilter.action = #selector(filterModelLibrary)
        let modelColumn = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("model"))
        modelColumn.resizingMask = .autoresizingMask
        modelLibraryTable.addTableColumn(modelColumn)
        modelLibraryTable.headerView = nil
        modelLibraryTable.rowHeight = 70
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
        list.borderType = .bezelBorder
        list.heightAnchor.constraint(equalToConstant: 288).isActive = true
        modelDetailTitle.font = .systemFont(ofSize: 17, weight: .semibold)
        modelDetailDescription.font = .systemFont(ofSize: 13)
        modelDetailDescription.textColor = .secondaryLabelColor
        modelCopyButton = button("Copy example request", #selector(copyDelegationPrompt))
        modelCopyButton.isHidden = true
        let disclosure = NSButton(title: "", target: self, action: #selector(toggleReasoning(_:)))
        disclosure.setButtonType(.onOff)
        disclosure.bezelStyle = .disclosure
        disclosure.setAccessibilityLabel("Show advanced reasoning settings")
        reasoningDetails = column([
            row([title("Reasoning", size: 13), effort]),
            note("How much effort the model spends thinking; support varies.")
        ])
        reasoningDetails.isHidden = true
        addModelForm = card([
            title("Add an OpenRouter model", size: 18),
            row([activeKeySummary, button("Manage API keys", #selector(showKeysPage))]),
            note("This key will be used for this model. Enabling an existing model updates its saved key."),
            title("OpenRouter model ID", size: 13), model,
            row([button("Browse model catalog", #selector(loadCatalog))]),
            note("Browse loads OpenRouter’s public catalog into this field. Type to search its suggestions."),
            row([disclosure, note("Advanced: reasoning effort"), NSView()]),
            reasoningDetails,
            row([primaryButton("Enable this model in Codex", action: #selector(registerAgent), symbol: "plus")])
        ])
        addModelForm.isHidden = true
        return column([
            pageHeading("Models", subtitle: "Browse available models; choose one inside Codex to start using it."),
            row([modelSearch, modelProviderFilter, button("Refresh", #selector(refreshModelInventory))]),
            registeredModelsSummary,
            list,
            card([modelDetailTitle, modelDetailDescription, row([modelCopyButton])]),
            row([button("Add OpenRouter model…", #selector(toggleAddModelForm))]),
            addModelForm
        ], spacing: 16)
    }

    @objc private func toggleAddModelForm() {
        addModelForm.isHidden.toggle()
    }

    @objc private func toggleReasoning(_ sender: NSButton) {
        reasoningDetails.isHidden = sender.state != .on
    }

    @objc private func refreshModelInventory() {
        guard !busy else { return }
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
                    ["openai", "openrouter"].contains($0["provider"] as? String ?? "")
                }
                var sourceNotes: [String] = []
                var anyAvailable = false
                for (key, label) in [("openai", "OpenAI"), ("openrouter", "OpenRouter")] {
                    let source = finalResult[key] as? [String: Any] ?? [:]
                    if source["ok"] as? Bool == true { anyAvailable = true }
                    else { sourceNotes.append(label + ": " + (source["error"] as? String ?? "Source unavailable")) }
                }
                self.modelsInventoryLoaded = anyAvailable
                self.registeredModelsSummary.stringValue = sourceNotes.isEmpty
                    ? "Model names from Codex and your local OpenRouter registrations. Selection here is a preview."
                    : sourceNotes.joined(separator: "\n")
                self.filterModelLibrary()
            }
        }
    }

    @objc private func filterModelLibrary() {
        let query = modelSearch.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let provider = modelProviderFilter.indexOfSelectedItem == 1 ? "openai" : modelProviderFilter.indexOfSelectedItem == 2 ? "openrouter" : ""
        let previousModel = selectedLibraryModel()?["model"] as? String
        visibleModelRecords = registeredModelRecords.filter { record in
            let matchesProvider = provider.isEmpty || record["provider"] as? String == provider
            let text = [record["model"], record["display_name"], record["description"]].compactMap { $0 as? String }.joined(separator: " ").lowercased()
            return matchesProvider && (query.isEmpty || text.contains(query))
        }
        modelLibraryTable.reloadData()
        if let selected = visibleModelRecords.firstIndex(where: { $0["model"] as? String == previousModel }) {
            modelLibraryTable.selectRowIndexes(IndexSet(integer: selected), byExtendingSelection: false)
        } else if !visibleModelRecords.isEmpty {
            modelLibraryTable.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false)
        } else { modelLibraryTable.deselectAll(nil) }
        updateLibrarySelection()
    }

    func numberOfRows(in tableView: NSTableView) -> Int { visibleModelRecords.count }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row index: Int) -> NSView? {
        guard visibleModelRecords.indices.contains(index) else { return nil }
        let record = visibleModelRecords[index]
        let cell = NSTableCellView()
        let heading = NSTextField(labelWithString: record["display_name"] as? String ?? "")
        heading.font = .systemFont(ofSize: 14, weight: .semibold)
        heading.lineBreakMode = .byTruncatingTail
        let billing = NSTextField(labelWithString: record["provider"] as? String == "openai" ? "OpenAI · OpenAI connection" : "OpenRouter · API credits")
        billing.font = .systemFont(ofSize: 11)
        billing.textColor = .secondaryLabelColor
        let identifier = NSTextField(labelWithString: record["model"] as? String ?? "")
        identifier.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        identifier.textColor = .secondaryLabelColor
        identifier.lineBreakMode = .byTruncatingMiddle
        let content = column([heading, billing, identifier], spacing: 3)
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

    func tableViewSelectionDidChange(_ notification: Notification) { updateLibrarySelection() }

    private func selectedLibraryModel() -> [String: Any]? {
        let selected = modelLibraryTable.selectedRow
        return visibleModelRecords.indices.contains(selected) ? visibleModelRecords[selected] : nil
    }

    private func updateLibrarySelection() {
        guard let record = selectedLibraryModel() else {
            modelDetailTitle.stringValue = "No model selected"
            modelDetailDescription.stringValue = registeredModelRecords.isEmpty
                ? "The library is empty or unavailable. Refresh to read available sources, or add an OpenRouter model."
                : "No models match this search or provider filter."
            modelCopyButton.isHidden = true
            return
        }
        modelDetailTitle.stringValue = record["display_name"] as? String ?? ""
        let isOpenRouter = record["provider"] as? String == "openrouter"
        let connection = isOpenRouter
            ? "Configured for OpenRouter API credits. Launch Codex from Overview and select this model. Existing tasks can change models within their provider; start a new task to change providers."
            : "Available through your normal Codex connection. Choose it in Codex’s model picker; no OpenRouter setup is needed."
        let description = record["description"] as? String ?? ""
        modelDetailDescription.stringValue = connection + (description.isEmpty ? "" : "\n\n" + description)
        modelCopyButton.isHidden = !isOpenRouter || !(record["role"] is String)
    }

    @objc private func copyDelegationPrompt() {
        guard let record = selectedLibraryModel(), record["provider"] as? String == "openrouter",
              let role = record["role"] as? String, !role.isEmpty else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString("Delegate this task to the \(role) agent: [describe your task].", forType: .string)
        report("Example request copied. Paste it into Codex.")
    }

    private func buildKeysPage() -> NSView {
        accounts.target = self
        accounts.action = #selector(accountChanged)
        accounts.widthAnchor.constraint(greaterThanOrEqualToConstant: 330).isActive = true
        accounts.setContentHuggingPriority(.defaultLow, for: .horizontal)
        accountName.placeholderString = "Personal, Work, or a project name"
        apiKey.placeholderString = "sk-or-…"
        return column([
            pageHeading("API keys", subtitle: "Choose which OpenRouter account your models use."),
            card([
                title("Saved keys", size: 18),
                row([accounts, button("Check key", #selector(checkKey))]),
                note("Checking a key contacts OpenRouter to verify access.")
            ]),
            card([
                title("Save an API key", size: 18),
                title("Key name", size: 13), accountName,
                title("OpenRouter API key", size: 13), apiKey,
                row([primaryButton("Save key", action: #selector(saveKey), symbol: "key")]),
                note("Stored in macOS Keychain. Reuse a saved name to replace its key. The API key is never written into Codex configuration.")
            ]),
            row([button("OpenRouter activity ↗", #selector(openDashboard))])
        ], spacing: 24)
    }

    private func buildAdvancedPage() -> NSView {
        restoreButton = button("Restore previous default", #selector(restoreSettings))
        return column([
            pageHeading("Advanced", subtitle: "Compatibility settings for earlier versions of this app."),
            card([
                title("Legacy default switch", size: 18),
                note("Earlier versions could replace your default Codex provider. If a restore record exists, this restores the model settings saved before that switch."),
                row([restoreButton]),
                note("This does not remove native agent registrations or saved API keys.")
            ]),
            card([
                title("Your default provider", size: 18),
                note("Adding models creates individual agent registrations. Your global default provider is unchanged. You can see the current settings on disk in Overview.")
            ])
        ], spacing: 24)
    }

    private func buildUsagePage() -> NSView {
        for label in [subscriptionUsageSummary, openRouterUsageSummary, usageUpdatedAt, openRouterUsageScope] {
            label.font = .systemFont(ofSize: 13)
            label.textColor = .secondaryLabelColor
        }
        for stack in [subscriptionUsageWindows, tokenActivityDetails, openRouterUsageDetails] {
            stack.orientation = .vertical
            stack.alignment = .leading
            stack.spacing = 16
        }
        appendUsageView(note("Refresh usage to load recent token activity."), to: tokenActivityDetails)
        return column([
            pageHeading("Usage & spending", subtitle: "Each model follows its own billing connection."),
            card([
                title("OpenAI → included allowance / eligible OpenAI credits", size: 15),
                note("OpenAI models never use OpenRouter credits."),
                title("Other models → OpenRouter credit balance", size: 15),
                note("Parent and child agents are billed independently by their route. Token context affects usage. Allowance percentages are not request counts or dollar amounts.")
            ]),
            row([primaryButton("Refresh usage", action: #selector(refreshUsage), symbol: "arrow.clockwise"),
                 button("OpenAI pricing ↗", #selector(openOpenAIPricing)),
                 button("OpenRouter activity ↗", #selector(openDashboard))]),
            card([
                title("OpenAI allowance", size: 20),
                subscriptionUsageSummary,
                subscriptionUsageWindows,
                note("Multiple windows in one pool apply at the same time. Do not add their percentages together.")
            ]),
            card([
                title("Recent token activity", size: 20),
                note("Latest available dates reported by OpenAI; tokens are not dollars or messages."),
                tokenActivityDetails
            ]),
            card([
                title("OpenRouter spending", size: 20),
                openRouterUsageScope,
                openRouterUsageSummary,
                openRouterUsageDetails,
                note("UTC periods across all apps using this key, not just Codex. Overlapping time periods — not separate charges."),
                note("A key spending cap is not your account credit balance.")
            ]),
            usageUpdatedAt,
            note("Refresh is read-only and never requests model inference or Keychain dialogs.")
        ], spacing: 20)
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
        for (index, page) in studioPages.enumerated() { page.isHidden = index != selected }
        for (index, control) in navigationButtons.enumerated() {
            control.state = index == selected ? .on : .off
            control.contentTintColor = index == selected ? .controlAccentColor : .secondaryLabelColor
            control.needsDisplay = true
        }
        if selected == 1 && !modelsInventoryLoaded { refreshModelInventory() }
    }

    private func buildWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1000, height: 760),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Model Studio for Codex"
        window.contentMinSize = NSSize(width: 840, height: 520)
        if !window.setFrameUsingName("ModelStudioWindow") { window.center() }
        window.setFrameAutosaveName("ModelStudioWindow")
        window.delegate = self
        guard let content = window.contentView else { return }
        let sidebar = NSVisualEffectView()
        sidebar.material = .sidebar
        sidebar.blendingMode = .behindWindow
        sidebar.state = .active
        sidebar.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(sidebar)
        let brand = title("Model Studio", size: 21)
        let brandSymbol = NSImageView(image: NSImage(systemSymbolName: "square.stack.3d.up.fill", accessibilityDescription: "Model Studio")!)
        brandSymbol.contentTintColor = .controlAccentColor
        brandSymbol.widthAnchor.constraint(equalToConstant: 29).isActive = true
        brandSymbol.heightAnchor.constraint(equalToConstant: 29).isActive = true
        let sidebarContent = column([row([brandSymbol, NSView()]), brand, note("for Codex")], spacing: 8)
        sidebarContent.translatesAutoresizingMaskIntoConstraints = false
        sidebar.addSubview(sidebarContent)
        let navigation = column([], spacing: 8)
        navigation.translatesAutoresizingMaskIntoConstraints = false
        sidebar.addSubview(navigation)
        for (index, entry) in [("Overview", "square.grid.2x2"), ("Models", "square.stack.3d.up"),
                               ("API keys", "key"), ("Usage", "chart.bar"),
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
            control.heightAnchor.constraint(equalToConstant: 38).isActive = true
            navigation.addArrangedSubview(control)
            control.widthAnchor.constraint(equalTo: navigation.widthAnchor).isActive = true
            navigationButtons.append(control)
        }
        let sidebarFootnote = note("Made for your\nCodex workspace")
        sidebarFootnote.translatesAutoresizingMaskIntoConstraints = false
        sidebar.addSubview(sidebarFootnote)
        let pageArea = NSView()
        pageArea.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(pageArea)
        feedback.font = .systemFont(ofSize: 13)
        feedback.textColor = .secondaryLabelColor
        feedback.maximumNumberOfLines = 0
        let footer = card([feedback])
        footer.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(footer)
        NSLayoutConstraint.activate([
            sidebar.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            sidebar.topAnchor.constraint(equalTo: content.topAnchor),
            sidebar.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            sidebar.widthAnchor.constraint(equalToConstant: 205),
            sidebarContent.leadingAnchor.constraint(equalTo: sidebar.leadingAnchor, constant: 22),
            sidebarContent.trailingAnchor.constraint(equalTo: sidebar.trailingAnchor, constant: -18),
            sidebarContent.topAnchor.constraint(equalTo: sidebar.topAnchor, constant: 34),
            navigation.leadingAnchor.constraint(equalTo: sidebar.leadingAnchor, constant: 14),
            navigation.trailingAnchor.constraint(equalTo: sidebar.trailingAnchor, constant: -14),
            navigation.topAnchor.constraint(equalTo: sidebarContent.bottomAnchor, constant: 34),
            sidebarFootnote.leadingAnchor.constraint(equalTo: sidebar.leadingAnchor, constant: 22),
            sidebarFootnote.trailingAnchor.constraint(equalTo: sidebar.trailingAnchor, constant: -18),
            sidebarFootnote.bottomAnchor.constraint(equalTo: sidebar.bottomAnchor, constant: -24),
            pageArea.leadingAnchor.constraint(equalTo: sidebar.trailingAnchor, constant: 34),
            pageArea.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -34),
            pageArea.topAnchor.constraint(equalTo: content.topAnchor, constant: 32),
            pageArea.bottomAnchor.constraint(equalTo: footer.topAnchor, constant: -18),
            footer.leadingAnchor.constraint(equalTo: pageArea.leadingAnchor),
            footer.trailingAnchor.constraint(equalTo: pageArea.trailingAnchor),
            footer.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -22),
            footer.heightAnchor.constraint(greaterThanOrEqualToConstant: 72)
        ])
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
                page.trailingAnchor.constraint(equalTo: document.trailingAnchor),
                page.topAnchor.constraint(equalTo: document.topAnchor),
                page.bottomAnchor.constraint(equalTo: document.bottomAnchor)
            ])
        }
        showStudioPage(0)
    }

    private func savePreferences() throws {
        try FileManager.default.createDirectory(at: stateDirectory, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        try JSONEncoder().encode(preferences).write(to: preferencesURL, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: preferencesURL.path)
    }

    private func reloadAccounts() {
        accounts.removeAllItems()
        if preferences.accounts.isEmpty { accounts.addItem(withTitle: "No saved keys yet") }
        for account in preferences.accounts { accounts.addItem(withTitle: account.name) }
        if let index = preferences.accounts.firstIndex(where: { $0.id == preferences.selectedAccount }) {
            accounts.selectItem(at: index)
        }
        updateActiveKeySummary()
    }

    private func updateActiveKeySummary() {
        let selected = accounts.indexOfSelectedItem
        activeKeySummary.stringValue = preferences.accounts.indices.contains(selected)
            ? "API key: \(preferences.accounts[selected].name)"
            : "No API key selected"
        openRouterUsageScope.stringValue = activeKeySummary.stringValue
        openRouterUsageSummary.stringValue = "Selected-key spending has not been loaded. Refresh usage to check this key."
        clearUsageStack(openRouterUsageDetails)
    }

    private func reloadFavorites() {
        favorites.removeAllItems()
        favorites.addItem(withTitle: "Saved shortcuts…")
        favorites.addItems(withTitles: preferences.models)
    }

    private func selectedAccount() throws -> SavedAccount {
        let index = accounts.indexOfSelectedItem
        guard preferences.accounts.indices.contains(index) else { throw SettingsError.message("Save an OpenRouter API key first.") }
        return preferences.accounts[index]
    }

    private func selectedModel() throws -> String {
        let slug = model.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard slug.contains("/"), !slug.contains(where: { $0.isWhitespace || $0.isNewline }), slug.count < 300, !slug.contains("\"") else {
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
        actionButtons.forEach { $0.isEnabled = !value }
        restoreButton.isEnabled = !value && restoreAvailable
        accounts.isEnabled = !value
        effort.isEnabled = !value
        modelLibraryTable.isEnabled = !value
    }

    @objc private func accountChanged() {
        do { preferences.selectedAccount = try selectedAccount().id; try savePreferences() }
        catch { report(error.localizedDescription, error: true) }
        updateActiveKeySummary()
    }

    @objc private func saveKey() {
        let name = accountName.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let key = apiKey.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty, name.count < 100 else { report("Give this key a short name.", error: true); return }
        guard key.hasPrefix("sk-or-"), key.count > 15, !key.contains(where: { $0.isWhitespace }) else {
            report("Paste a valid OpenRouter key starting with sk-or-.", error: true); return
        }
        do {
            let existing = preferences.accounts.first(where: { $0.name == name })
            let account = existing ?? SavedAccount(id: UUID().uuidString, name: name)
            try KeychainCredentials.save(key, account: account.id)
            if existing == nil { preferences.accounts.append(account) }
            preferences.selectedAccount = account.id
            try savePreferences()
            apiKey.stringValue = ""
            accountName.stringValue = ""
            reloadAccounts()
            report("Saved in Keychain. Choose a non-OpenAI model, then register its native subagent.")
        } catch { report(error.localizedDescription, error: true) }
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
        do {
            let key = try KeychainCredentials.read(selectedAccount().id)
            var request = URLRequest(url: URL(string: "https://openrouter.ai/api/v1/key")!)
            request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
            request.timeoutInterval = 20
            setBusy(true)
            report("Checking the saved key…")
            fetch(request) { bytes, response, error in
                self.setBusy(false)
                if error != nil { self.report("Could not reach OpenRouter. Check your connection and try again.", error: true); return }
                guard response?.statusCode == 200, let bytes,
                      let result = try? JSONSerialization.jsonObject(with: bytes) as? [String: Any], result["data"] != nil else {
                    self.report("OpenRouter rejected the key check (HTTP \(response?.statusCode ?? 0)). Check or replace the saved key.", error: true); return
                }
                self.report("Key accepted by OpenRouter. No model inference was requested; model compatibility is not yet tested.")
            }
        } catch { report(error.localizedDescription, error: true) }
    }

    @objc private func loadCatalog() {
        setBusy(true)
        report("Loading OpenRouter’s public model catalog…")
        var request = URLRequest(url: URL(string: "https://openrouter.ai/api/v1/models")!)
        request.timeoutInterval = 20
        fetch(request) { bytes, response, error in
            self.setBusy(false)
            guard error == nil, response?.statusCode == 200, let bytes,
                  let result = try? JSONSerialization.jsonObject(with: bytes) as? [String: Any],
                  let entries = result["data"] as? [[String: Any]] else {
                self.report("Could not load the catalog. You can still type an exact model ID.", error: true); return
            }
            self.catalog = entries.compactMap { $0["id"] as? String }.sorted()
            let selection = self.model.stringValue
            self.model.removeAllItems()
            self.model.addItems(withObjectValues: self.catalog)
            self.model.stringValue = selection
            self.report("Loaded \(self.catalog.count) models. Type a provider name in the model field to autocomplete, or use its dropdown.")
        }
    }

    private func currentUsageAccountID() -> String {
        let selected = accounts.indexOfSelectedItem
        return preferences.accounts.indices.contains(selected) ? preferences.accounts[selected].id : ""
    }

    private func clearUsageWindows() {
        clearUsageStack(subscriptionUsageWindows)
        clearUsageStack(tokenActivityDetails)
        clearUsageStack(openRouterUsageDetails)
    }

    private func clearUsageStack(_ stack: NSStackView) {
        for view in stack.arrangedSubviews {
            stack.removeArrangedSubview(view)
            view.removeFromSuperview()
        }
    }

    private func usageNumber(_ value: Any?) -> Double? {
        guard let number = value as? NSNumber, number.doubleValue.isFinite else { return nil }
        return number.doubleValue
    }

    private func usageDate(_ timestamp: Double) -> String {
        let formatter = DateFormatter()
        formatter.dateStyle = .medium
        formatter.timeStyle = .short
        return formatter.string(from: Date(timeIntervalSince1970: timestamp))
    }

    private func usageDollars(_ value: Any?) -> String {
        guard let amount = usageNumber(value) else { return "Unavailable" }
        let formatter = NumberFormatter()
        formatter.numberStyle = .currency
        formatter.currencyCode = "USD"
        formatter.locale = Locale(identifier: "en_US")
        formatter.maximumFractionDigits = 4
        return formatter.string(from: NSNumber(value: amount)) ?? "Unavailable"
    }

    private func appendUsageView(_ view: NSView, to stack: NSStackView) {
        stack.addArrangedSubview(view)
        view.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
    }

    private func usageCount(_ number: Double) -> String {
        let formatter = NumberFormatter()
        formatter.numberStyle = .decimal
        formatter.maximumFractionDigits = 0
        return formatter.string(from: NSNumber(value: number)) ?? "Unavailable"
    }

    private func resetDescription(_ timestamp: Double) -> String {
        let seconds = timestamp - Date().timeIntervalSince1970
        let relative: String
        if seconds <= 0 { relative = "reset time reached; refresh for current limits" }
        else if seconds < 3600 { relative = "in \(Int(ceil(seconds / 60))) min" }
        else if seconds < 86400 { relative = "in \(Int(seconds / 3600)) hr \(Int(seconds.truncatingRemainder(dividingBy: 3600) / 60)) min" }
        else { relative = "in \(Int(seconds / 86400)) days \(Int(seconds.truncatingRemainder(dividingBy: 86400) / 3600)) hr" }
        return "Resets \(usageDate(timestamp)) · \(relative)"
    }

    private func usageWindowView(_ window: [String: Any]) -> NSView {
        var name = window["window_kind"] as? String ?? window["name"] as? String ?? "Usage window"
        if let minutes = usageNumber(window["window_minutes"]), minutes > 0 {
            let duration: String
            if minutes.truncatingRemainder(dividingBy: 1440) == 0 { duration = String(format: "%.0f-day allowance", minutes / 1440) }
            else if minutes.truncatingRemainder(dividingBy: 60) == 0 { duration = String(format: "%.0f-hour allowance", minutes / 60) }
            else { duration = String(format: "%g-minute allowance", minutes) }
            name = duration
        }
        let heading = NSTextField(wrappingLabelWithString: name)
        heading.font = .systemFont(ofSize: 14, weight: .semibold)
        var views: [NSView] = [heading]
        if let used = usageNumber(window["used_percent"]) {
            let remaining = min(100, max(0, 100 - used))
            let allowance = note(String(format: "%.0f%% remaining · %.0f%% used%@", remaining, used, remaining < 20 ? " · Low remaining allowance" : ""))
            if remaining < 20 { allowance.textColor = .systemOrange }
            views += [allowance, AllowanceBar(remaining: remaining)]
        } else { views.append(note("Remaining and used allowance unavailable")) }
        views.append(note(usageNumber(window["resets_at"]).map { resetDescription($0) } ?? "Reset time unavailable"))
        return column(views, spacing: 7)
    }

    private func displaySubscriptionUsage(_ subscription: [String: Any]) {
        if subscription["ok"] as? Bool == true {
            let windows = subscription["windows"] as? [[String: Any]] ?? []
            subscriptionUsageSummary.stringValue = windows.isEmpty ? "Allowance windows unavailable." : "Separate pools and their simultaneous limits"
            var poolIDs: [String] = []
            for window in windows {
                let id = window["pool_id"] as? String ?? "unidentified"
                if !poolIDs.contains(id) { poolIDs.append(id) }
            }
            func poolRank(_ id: String) -> Int {
                if id.lowercased() == "codex" { return 0 }
                let name = windows.first { ($0["pool_id"] as? String ?? "unidentified") == id }?["pool_name"] as? String ?? ""
                return name.lowercased() == "gpt-5.3-codex-spark" ? 1 : 2
            }
            poolIDs.sort { first, second in
                let firstRank = poolRank(first), secondRank = poolRank(second)
                return firstRank == secondRank ? first < second : firstRank < secondRank
            }
            for poolID in poolIDs {
                let poolWindows = windows.filter { ($0["pool_id"] as? String ?? "unidentified") == poolID }
                let suppliedName = poolWindows.first?["pool_name"] as? String ?? ""
                let heading: String
                var explanation: String? = nil
                if poolID.lowercased() == "codex" {
                    heading = "General Codex allowance"
                    explanation = "Shared general allowance reported by Codex. Eligible OpenAI work uses this connection, not OpenRouter; this report does not itemize charges per model."
                }
                else if suppliedName.lowercased() == "gpt-5.3-codex-spark" {
                    heading = "Codex Spark allowance"
                    explanation = "Separate allowance reported for GPT-5.3-Codex-Spark. Using this model follows the OpenAI connection, not your OpenRouter key."
                }
                else {
                    heading = (suppliedName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? poolID : suppliedName) + " — additional allowance"
                    explanation = "OpenAI reports this pool separately, but its purpose and eligible models are not provided here. It is not an OpenRouter balance."
                }
                let headingLabel = NSTextField(wrappingLabelWithString: heading)
                headingLabel.font = .systemFont(ofSize: 17, weight: .semibold)
                var views: [NSView] = [headingLabel]
                if let explanation = explanation { views.append(note(explanation)) }
                views.append(contentsOf: poolWindows.map { usageWindowView($0) })
                views.append(note("Reported pool ID: " + poolID))
                appendUsageView(column(views, spacing: 12), to: subscriptionUsageWindows)
            }
        } else {
            subscriptionUsageSummary.stringValue = "Unavailable now.\n" + (subscription["error"] as? String ?? "Could not read subscription limits.")
        }
        var metrics: [String] = []
        if let tokens = usageNumber(subscription["lifetime_tokens"]) { metrics.append("Lifetime tokens: " + usageCount(tokens)) }
        let summary = subscription["summary"] as? [String: Any] ?? [:]
        if let peak = usageNumber(summary["peak_daily_tokens"]) { metrics.append("Peak daily tokens: " + usageCount(peak)) }
        if let streak = usageNumber(summary["current_streak_days"]) { metrics.append("Current streak: " + usageCount(streak) + " days") }
        if let streak = usageNumber(summary["longest_streak_days"]) { metrics.append("Longest streak: " + usageCount(streak) + " days") }
        if !metrics.isEmpty { appendUsageView(note(metrics.joined(separator: "  ·  ")), to: tokenActivityDetails) }
        let days = (subscription["daily_usage"] as? [[String: Any]] ?? []).sorted {
            ($0["date"] as? String ?? "") < ($1["date"] as? String ?? "")
        }.suffix(14)
        let samples: [(date: String, tokens: Double?)] = days.compactMap { day in
            guard let date = day["date"] as? String else { return nil }
            return (date, usageNumber(day["tokens"]))
        }
        if samples.contains(where: { $0.tokens != nil }) {
            appendUsageView(TokenActivityChart(samples: samples), to: tokenActivityDetails)
            appendUsageView(note("Bars show reported dates only. A dash means unavailable, not zero. Hover for the exact daily values."), to: tokenActivityDetails)
        } else { appendUsageView(note("Recent daily token activity is unavailable."), to: tokenActivityDetails) }
        if let error = subscription["usage_error"] as? String { appendUsageView(note(error), to: tokenActivityDetails) }
    }

    private func displayOpenRouterUsage(_ router: [String: Any]) {
        guard router["ok"] as? Bool == true else {
            openRouterUsageSummary.stringValue = "Unavailable now.\n" + (router["error"] as? String ?? "Could not read selected-key spending.")
            return
        }
        openRouterUsageSummary.stringValue = "Provider-reported spending in USD"
        var tiles: [NSView] = []
        for (label, key) in [("Today", "usage_daily"), ("This week", "usage_weekly"), ("This month", "usage_monthly"), ("All time", "usage_total")] {
            let amount = NSTextField(wrappingLabelWithString: usageDollars(router[key]))
            amount.font = .systemFont(ofSize: 22, weight: .semibold)
            tiles.append(column([note(label), amount], spacing: 7))
        }
        let amounts = row(tiles)
        amounts.distribution = .fillEqually
        amounts.alignment = .top
        for tile in tiles.dropFirst() { tile.widthAnchor.constraint(equalTo: tiles[0].widthAnchor).isActive = true }
        appendUsageView(amounts, to: openRouterUsageDetails)
        if let limit = usageNumber(router["limit"]) {
            appendUsageView(note("Key spending cap: \(usageDollars(limit)) · Remaining: \(usageDollars(router["limit_remaining"]))"), to: openRouterUsageDetails)
            if limit > 0, let remaining = usageNumber(router["limit_remaining"]) {
                appendUsageView(AllowanceBar(remaining: 100 * remaining / limit), to: openRouterUsageDetails)
            }
        } else if router["limit_known"] as? Bool == true && router["limit"] is NSNull {
            appendUsageView(note("No key spending cap"), to: openRouterUsageDetails)
        } else { appendUsageView(note("Key spending cap unavailable"), to: openRouterUsageDetails) }
        if let reset = router["limit_reset"] as? String, ["daily", "weekly", "monthly"].contains(reset) {
            appendUsageView(note("Key cap resets " + reset + "."), to: openRouterUsageDetails)
        }
        if usageNumber(router["byok_usage"]) != nil {
            appendUsageView(note("External-provider (BYOK) usage, reported separately: " + usageDollars(router["byok_usage"]) + ". Not added to the spending totals above."), to: openRouterUsageDetails)
        }
        if let included = router["include_byok_in_limit"] as? Bool {
            appendUsageView(note(included ? "BYOK usage counts toward this key’s spending cap." : "BYOK usage does not count toward this key’s spending cap."), to: openRouterUsageDetails)
        }
    }

    private func displayUsage(_ result: [String: Any]) {
        clearUsageWindows()
        displaySubscriptionUsage(result["openai"] as? [String: Any] ?? [:])
        displayOpenRouterUsage(result["openrouter"] as? [String: Any] ?? [:])
        let timestamp = result["fetched_at"] as? String ?? ""
        let fractionalDateFormatter = ISO8601DateFormatter()
        fractionalDateFormatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let date = fractionalDateFormatter.date(from: timestamp) ?? ISO8601DateFormatter().date(from: timestamp)
        usageUpdatedAt.stringValue = date.map { "Last refresh attempt: \(usageDate($0.timeIntervalSince1970)). Provider errors above are current." }
            ?? "Refresh finished. Provider errors above are current; timestamp unavailable."
    }

    @objc private func refreshUsage() {
        let accountID = currentUsageAccountID()
        setBusy(true)
        clearUsageWindows()
        subscriptionUsageSummary.stringValue = "Refreshing subscription limits…"
        openRouterUsageSummary.stringValue = "Refreshing selected-key spending…"
        usageUpdatedAt.stringValue = "Refresh in progress…"
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
                let failure: [String: Any] = ["ok": false, "error": "Usage refresh did not complete. Check the app resources and try again."]
                result = ["openai": failure, "openrouter": failure]
            }
            let finalResult = result
            DispatchQueue.main.async {
                self.setBusy(false)
                guard self.currentUsageAccountID() == accountID else {
                    self.subscriptionUsageSummary.stringValue = "Refresh again after changing the selected key."
                    self.openRouterUsageSummary.stringValue = "Selected key changed. Refresh to load its spending."
                    self.usageUpdatedAt.stringValue = "Previous response ignored because the selected key changed."
                    return
                }
                self.displayUsage(finalResult)
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

    private func configure(_ request: [String: Any], completion: @escaping ([String: Any]) -> Void) {
        setBusy(true)
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
            DispatchQueue.main.async { self.setBusy(false); completion(finalResult) }
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
        do {
            let account = try selectedAccount()
            _ = try KeychainCredentials.read(account.id)
            let slug = try selectedModel()
            preferences.selectedAccount = account.id
            preferences.selectedModel = slug
            try savePreferences()
            let request: [String: Any] = ["action": "register_agent", "model": slug, "account": account.id,
                "executable": Bundle.main.executableURL!.path,
                "effort": effort.indexOfSelectedItem == 0 ? "default" : effort.titleOfSelectedItem!]
            configure(request) { result in
                self.displayStatus(result)
                if result["ok"] as? Bool == true {
                    self.report("Enabled \(slug) locally. Quit ChatGPT/Codex, then use Overview → Launch Codex. Choose the model in Codex; start a new task if changing providers.")
                    self.refreshModelInventory()
                }
            }
        } catch { report(error.localizedDescription, error: true) }
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
                self.report("Quit ChatGPT/Codex completely first, after finishing active work, then click Launch Codex again. Your running tasks will not be interrupted.", error: true)
                return
            }
            let launcher = Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/CodexProviderBridge")
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/open")
            process.arguments = ["-a", applicationPath, "--env", "CODEX_CLI_PATH=" + launcher.path]
            do {
                try process.run()
                let name = URL(fileURLWithPath: applicationPath).deletingPathExtension().lastPathComponent
                self.report("Requested integrated launch of \(name). Existing tasks can use models on their current provider. Start a new task to choose a different provider.")
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

let application = NSApplication.shared
let delegate = OpenRouterSettingsApp()
application.setActivationPolicy(.regular)
application.delegate = delegate
application.run()

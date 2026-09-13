import AppKit
import Foundation
import ModelDeckClient
import ModelDeckContracts
import ModelDeckPlatform
import ModelDeckPresentation

private struct DemoArguments {
    let rendezvousURL: URL
    let credentialURL: URL
    let initialPanelID: String?

    static func parse(_ arguments: [String]) throws -> DemoArguments {
        var values: [String: String] = [:]
        var index = 1
        while index < arguments.count {
            let flag = arguments[index]
            guard ["--rendezvous", "--credential", "--panel"].contains(flag), index + 1 < arguments.count else {
                throw DemoError.usage
            }
            values[flag] = arguments[index + 1]
            index += 2
        }
        guard let rendezvousPath = values["--rendezvous"], let credentialPath = values["--credential"] else {
            throw DemoError.usage
        }
        return DemoArguments(
            rendezvousURL: URL(fileURLWithPath: rendezvousPath),
            credentialURL: URL(fileURLWithPath: credentialPath),
            initialPanelID: values["--panel"]
        )
    }
}

private enum DemoError: Error, CustomStringConvertible {
    case usage
    case noPanels
    case unknownPanel(String)

    var description: String {
        switch self {
        case .usage:
            return "usage: ModelDeckPanelDemo --rendezvous PATH --credential PATH [--panel PANEL_ID]"
        case .noPanels:
            return "the engine returned no panel contributions"
        case .unknownPanel(let panelID):
            return "requested panel was not discovered: \(panelID)"
        }
    }
}

@MainActor
private final class PanelDemoController: NSViewController {
    private let service: EngineExtensionPanelService
    private let panels: [ExtensionPanelContribution]
    private let availableOperationIDs: Set<String>
    private let panelPicker = NSPopUpButton()
    private let statusLabel = NSTextField(wrappingLabelWithString: "")
    private let panelContainer = NSView()
    private var renderer: PanelRenderer?

    init(service: EngineExtensionPanelService, panels: [ExtensionPanelContribution], operations: [ExtensionOperationDescriptor]) {
        self.service = service
        self.panels = panels
        self.availableOperationIDs = Set(operations.map(\.operationID))
        super.init(nibName: nil, bundle: nil)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) is unavailable") }

    override func loadView() {
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 10
        root.edgeInsets = NSEdgeInsets(top: 12, left: 12, bottom: 12, right: 12)

        panelPicker.addItems(withTitles: panels.map { $0.title ?? $0.panelID })
        panelPicker.target = self
        panelPicker.action = #selector(panelSelectionChanged)
        root.addArrangedSubview(panelPicker)

        statusLabel.textColor = .secondaryLabelColor
        statusLabel.maximumNumberOfLines = 8
        root.addArrangedSubview(statusLabel)

        panelContainer.translatesAutoresizingMaskIntoConstraints = false
        root.addArrangedSubview(panelContainer)
        NSLayoutConstraint.activate([
            panelContainer.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -24),
            panelContainer.heightAnchor.constraint(greaterThanOrEqualToConstant: 360),
        ])
        view = root
    }

    func show(panelID: String) throws {
        guard let index = panels.firstIndex(where: { $0.panelID == panelID }) else {
            throw DemoError.unknownPanel(panelID)
        }
        panelPicker.selectItem(at: index)
        try renderSelectedPanel()
    }

    @objc private func panelSelectionChanged() {
        do { try renderSelectedPanel() } catch { show(error: error) }
    }

    private func renderSelectedPanel() throws {
        let panel = panels[panelPicker.indexOfSelectedItem]
        let document = try decodePanel(service.fetchPanel(panelID: panel.panelID))
        installRenderer(document: document)
        statusLabel.stringValue = "Loaded \(panel.panelID), revision \(document.revision)"
    }

    private func installRenderer(document: PanelDocument) {
        renderer?.view.removeFromSuperview()
        renderer?.removeFromParent()

        let availableOperationIDs = self.availableOperationIDs
        let nextRenderer = PanelRenderer(
            document: document,
            operationLookup: { operationID in
                availableOperationIDs.contains(operationID)
                    ? TrustedPanelOperationDescriptor(operationID: operationID)
                    : nil
            },
            onActionIntent: { [weak self] intent in self?.invoke(intent: intent) }
        )
        addChild(nextRenderer)
        nextRenderer.view.translatesAutoresizingMaskIntoConstraints = false
        panelContainer.addSubview(nextRenderer.view)
        NSLayoutConstraint.activate([
            nextRenderer.view.leadingAnchor.constraint(equalTo: panelContainer.leadingAnchor),
            nextRenderer.view.trailingAnchor.constraint(equalTo: panelContainer.trailingAnchor),
            nextRenderer.view.topAnchor.constraint(equalTo: panelContainer.topAnchor),
            nextRenderer.view.bottomAnchor.constraint(equalTo: panelContainer.bottomAnchor),
        ])
        renderer = nextRenderer
    }

    private func invoke(intent: PanelActionIntent) {
        statusLabel.stringValue = "Invoking \(intent.operationID)…"
        let service = self.service
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let result = try service.invokeOperation(operationID: intent.operationID, input: .object(intent.params))
                let output = try Self.prettyJSON(result.output)
                let refreshed = try self.map { _ in try Self.decodePanel(service.fetchPanel(panelID: intent.panelID)) }
                DispatchQueue.main.async {
                    guard let self else { return }
                    self.statusLabel.stringValue = output
                    if let refreshed { _ = self.renderer?.update(document: refreshed) }
                }
            } catch {
                DispatchQueue.main.async { self?.show(error: error) }
            }
        }
    }

    private func show(error: Error) { statusLabel.stringValue = "Error: \(error)" }

    private func decodePanel(_ panel: JSONValue) throws -> PanelDocument { try Self.decodePanel(panel) }

    nonisolated private static func decodePanel(_ panel: JSONValue) throws -> PanelDocument {
        try PanelDocumentCodec.decode(JSONEncoder().encode(panel))
    }

    nonisolated private static func prettyJSON(_ value: JSONValue) throws -> String {
        let encoded = try JSONEncoder().encode(value)
        let object = try JSONSerialization.jsonObject(with: encoded)
        return String(decoding: try JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys]), as: UTF8.self)
    }
}

private final class PanelDemoAppDelegate: NSObject, NSApplicationDelegate {
    private let arguments: DemoArguments
    private var window: NSWindow?

    init(arguments: DemoArguments) { self.arguments = arguments }

    func applicationDidFinishLaunching(_ notification: Notification) {
        do {
            let descriptor = try EngineRendezvousDescriptor.load(from: arguments.rendezvousURL)
            let service = EngineExtensionPanelService(
                rendezvous: descriptor,
                transport: UnixSocketEngineTransport(socketPath: descriptor.socketPath),
                credentialProvider: EngineFileCredentialProvider(credentialURL: arguments.credentialURL)
            )
            try service.connect()
            let panels = try service.listPanels()
            guard let firstPanel = panels.first else { throw DemoError.noPanels }
            let controller = PanelDemoController(service: service, panels: panels, operations: try service.listOperations())
            _ = controller.view
            try controller.show(panelID: arguments.initialPanelID ?? firstPanel.panelID)

            let window = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 680, height: 600),
                styleMask: [.titled, .closable, .resizable, .miniaturizable],
                backing: .buffered,
                defer: false
            )
            window.title = "Model Deck Generic Panel Demo"
            window.contentViewController = controller
            window.center()
            window.makeKeyAndOrderFront(nil)
            self.window = window
            NSApp.activate(ignoringOtherApps: true)
        } catch {
            fputs("ModelDeckPanelDemo: \(error)\n", stderr)
            NSApp.terminate(nil)
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

do {
    let arguments = try DemoArguments.parse(CommandLine.arguments)
    let application = NSApplication.shared
    let delegate = PanelDemoAppDelegate(arguments: arguments)
    application.delegate = delegate
    application.setActivationPolicy(.regular)
    application.run()
} catch {
    fputs("ModelDeckPanelDemo: \(error)\n", stderr)
    exit(64)
}

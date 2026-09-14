import AppKit
import Foundation
import ModelDeckClient
import ModelDeckContracts
import ModelDeckPresentation
import UniformTypeIdentifiers

@MainActor
final class V2WorkspaceController: NSViewController {
    private let extensionPicker = NSPopUpButton()
    private let extensionStateButton = NSButton()
    private let panelPicker = NSPopUpButton()
    private let statusLabel = NSTextField(wrappingLabelWithString: "Starting isolated engine…")
    private let panelContainer = NSView()
    private let placeholderLabel = NSTextField(wrappingLabelWithString: "Enable an extension to show its panels.")
    private let codingLabel = NSTextField(wrappingLabelWithString: "Coding route unavailable")
    private let usageLabel = NSTextField(wrappingLabelWithString: "Usage not loaded")
    private let refreshUsageButton = NSButton()

    private var service: EngineExtensionPanelService?
    private var usageService: EngineUsageService?
    private var extensions: [ExtensionDetail] = []
    private var panels: [ExtensionPanelContribution] = []
    private var availableOperationIDs: Set<String> = []
    private var renderer: PanelRenderer?

    private var jobObservers: [String: JobObservationViewController] = [:]
    private var preferredExtensionID: String?

    override func loadView() {
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 10
        root.edgeInsets = NSEdgeInsets(top: 16, left: 16, bottom: 16, right: 16)

        let heading = NSTextField(labelWithString: "Model Deck V2")
        heading.font = .systemFont(ofSize: 20, weight: .semibold)
        root.addArrangedSubview(heading)
        codingLabel.textColor = .secondaryLabelColor
        root.addArrangedSubview(codingLabel)
        refreshUsageButton.title = "Refresh usage"
        refreshUsageButton.target = self
        refreshUsageButton.action = #selector(refreshUsageRequested)
        let usageControls = NSStackView(views: [refreshUsageButton, usageLabel])
        usageControls.spacing = 8
        root.addArrangedSubview(usageControls)

        let extensionControls = NSStackView()
        extensionControls.orientation = .horizontal
        extensionControls.spacing = 8
        extensionPicker.target = self
        extensionPicker.action = #selector(extensionSelectionChanged)
        extensionPicker.setContentHuggingPriority(.defaultLow, for: .horizontal)
        extensionControls.addArrangedSubview(extensionPicker)
        extensionControls.addArrangedSubview(makeButton("Install…", action: #selector(chooseExtensionArchive)))
        extensionControls.addArrangedSubview(makeButton("Refresh", action: #selector(refreshRequested)))
        extensionStateButton.target = self
        extensionStateButton.action = #selector(toggleSelectedExtension)
        extensionControls.addArrangedSubview(extensionStateButton)
        root.addArrangedSubview(extensionControls)

        panelPicker.target = self
        panelPicker.action = #selector(panelSelectionChanged)
        panelPicker.setContentHuggingPriority(.defaultLow, for: .horizontal)
        root.addArrangedSubview(panelPicker)

        statusLabel.textColor = .secondaryLabelColor
        statusLabel.maximumNumberOfLines = 3
        root.addArrangedSubview(statusLabel)

        placeholderLabel.textColor = .secondaryLabelColor
        placeholderLabel.alignment = .center
        placeholderLabel.translatesAutoresizingMaskIntoConstraints = false
        panelContainer.addSubview(placeholderLabel)
        panelContainer.translatesAutoresizingMaskIntoConstraints = false
        root.addArrangedSubview(panelContainer)

        NSLayoutConstraint.activate([
            extensionControls.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -32),
            extensionPicker.widthAnchor.constraint(greaterThanOrEqualToConstant: 260),
            panelPicker.widthAnchor.constraint(greaterThanOrEqualToConstant: 300),
            panelContainer.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -32),
            panelContainer.heightAnchor.constraint(greaterThanOrEqualToConstant: 420),
            placeholderLabel.centerXAnchor.constraint(equalTo: panelContainer.centerXAnchor),
            placeholderLabel.centerYAnchor.constraint(equalTo: panelContainer.centerYAnchor),
            placeholderLabel.widthAnchor.constraint(lessThanOrEqualTo: panelContainer.widthAnchor, constant: -40),
        ])

        view = root
        updateControls()
    }

    func attach(service: EngineExtensionPanelService) {
        self.service = service
        statusLabel.stringValue = "V2 engine connected."
        refreshWorkspace()
    }

    func attach(usageService: EngineUsageService, bridgeSummary: V2BridgeSummary?) {
        self.usageService = usageService
        codingLabel.stringValue = bridgeSummary.map { "Coding route: \($0.provider) / \($0.model) — \($0.billing)" } ?? "Coding route unavailable"
        refreshUsageRequested()
    }

    @objc private func refreshUsageRequested() {
        guard let usageService else { return }
        usageLabel.stringValue = "Refreshing usage…"
        runInBackground({ try usageService.query() }, success: { [weak self] records in
            let totals = records.reduce(into: [String: Double]()) { $0[$1.unitKind, default: 0] += $1.units }
            let parts = ["input_tokens", "output_tokens", "cached_tokens"].compactMap { key in totals[key].map { "\(key): \($0)" } }
            self?.usageLabel.stringValue = parts.isEmpty ? "No completed usage records" : parts.joined(separator: " · ")
        })
    }

    func showStartupFailure(_ error: Error) {
        statusLabel.stringValue = "V2 engine failed to start: \(error)"
        updateControls()
    }

    @objc private func refreshRequested() {
        refreshWorkspace()
    }

    @objc private func extensionSelectionChanged() {
        preferredExtensionID = selectedExtension?.extensionID
        updateControls()
    }

    @objc private func panelSelectionChanged() {
        guard panelPicker.indexOfSelectedItem >= 0 else { return }
        loadPanel(panels[panelPicker.indexOfSelectedItem])
    }

    @objc private func chooseExtensionArchive() {
        let picker = NSOpenPanel()
        picker.allowedContentTypes = [.zip]
        picker.allowsMultipleSelection = false
        picker.canChooseDirectories = false
        picker.message = "Choose a packaged Model Deck extension"
        picker.begin { [weak self] response in
            guard response == .OK, let archive = picker.url else { return }
            self?.installExtension(at: archive)
        }
    }

    @objc private func toggleSelectedExtension() {
        guard let service, let selectedExtension else { return }
        let shouldEnable = selectedExtension.status != "enabled"
        statusLabel.stringValue = shouldEnable ? "Enabling extension…" : "Disabling extension…"
        runInBackground(
            {
                try service.setExtensionEnabled(
                    extensionID: selectedExtension.extensionID,
                    revision: selectedExtension.revision,
                    enabled: shouldEnable
                )
            },
            success: { [weak self] in
                self?.preferredExtensionID = selectedExtension.extensionID
                self?.statusLabel.stringValue = shouldEnable ? "Extension enabled." : "Extension disabled."
                self?.refreshWorkspace()
            }
        )
    }

    private var selectedExtension: ExtensionDetail? {
        let index = extensionPicker.indexOfSelectedItem
        guard extensions.indices.contains(index) else { return nil }
        return extensions[index]
    }

    private func installExtension(at archive: URL) {
        guard let service else { return }
        statusLabel.stringValue = "Installing extension…"
        runInBackground(
            { try service.installExtension(archivePath: archive.path) },
            success: { [weak self] extensionID in
                self?.preferredExtensionID = extensionID
                self?.statusLabel.stringValue = "Extension installed."
                self?.refreshWorkspace()
            }
        )
    }

    private func refreshWorkspace() {
        guard let service else {
            updateControls()
            return
        }
        statusLabel.stringValue = "Refreshing extensions and panels…"
        runInBackground(
            {
                let summaries = try service.listInstalledExtensions()
                let details = try summaries.map {
                    try service.extensionDetail(extensionID: $0.extensionID)
                }
                let panels = try service.listPanels()
                let operations = try service.listOperations()
                return (details, panels, Set(operations.map(\.operationID)))
            },
            success: { [weak self] workspace in
                guard let self else { return }
                let (details, panels, operationIDs) = workspace
                self.extensions = details
                self.panels = panels
                self.availableOperationIDs = operationIDs
                self.reloadExtensionPicker()
                self.reloadPanelPicker()
                self.statusLabel.stringValue = details.isEmpty
                    ? "Install an extension to begin."
                    : "Extensions refreshed."
            }
        )
    }

    private func reloadExtensionPicker() {
        extensionPicker.removeAllItems()
        extensionPicker.addItems(withTitles: extensions.map { "\($0.extensionID) — \($0.status)" })
        if let preferredExtensionID,
           let index = extensions.firstIndex(where: { $0.extensionID == preferredExtensionID }) {
            extensionPicker.selectItem(at: index)
        } else if !extensions.isEmpty {
            extensionPicker.selectItem(at: 0)
            preferredExtensionID = extensions[0].extensionID
        }
        updateControls()
    }

    private func reloadPanelPicker() {
        let selectedPanelID = renderer?.document.panelID
        panelPicker.removeAllItems()
        panelPicker.addItems(withTitles: panels.map { $0.title ?? $0.panelID })
        if let selectedPanelID,
           let index = panels.firstIndex(where: { $0.panelID == selectedPanelID }) {
            panelPicker.selectItem(at: index)
            loadPanel(panels[index])
        } else if let first = panels.first {
            panelPicker.selectItem(at: 0)
            loadPanel(first)
        } else {
            removeRenderer(message: "No enabled extension panels are available.")
        }
        updateControls()
    }

    private func loadPanel(_ panel: ExtensionPanelContribution) {
        guard let service else { return }
        statusLabel.stringValue = "Loading \(panel.title ?? panel.panelID)…"
        runInBackground(
            { try Self.decodePanel(service.fetchPanel(panelID: panel.panelID)) },
            success: { [weak self] document in
                self?.display(document: document)
                self?.statusLabel.stringValue = "Panel loaded."
            }
        )
    }

    private func invoke(_ intent: PanelActionIntent) {
        guard let service else { return }
        statusLabel.stringValue = "Saving or refreshing…"
        runInBackground(
            {
                let result = try service.invokeOperation(
                    operationID: intent.operationID,
                    input: .object(intent.params)
                )
                let document: PanelDocument
                if let panel = result.panel {
                    document = try Self.decodePanel(panel)
                } else {
                    document = try Self.decodePanel(service.fetchPanel(panelID: intent.panelID))
                }
                return (document, result.jobID)
            },
            success: { [weak self] outcome in
                guard let self else { return }
                let (document, jobID) = outcome
                self.display(document: document)
                if let jobID {
                    self.presentJobObservation(jobID: jobID)
                    self.statusLabel.stringValue = "Action started async job \(jobID)."
                } else {
                    self.statusLabel.stringValue = "Action completed."
                }
            }
        )
    }

    /// Presents a bounded observation child controller for ``jobID``. The
    /// controller drives its own polling via ``engine.v1.jobs.get`` and
    /// offers a single "Request cancel" button. We never parse plugin output
    /// here — the child renders the canonical JSON envelope verbatim.
    private func presentJobObservation(jobID: String) {
        if let existing = jobObservers[jobID] {
            view.window?.makeFirstResponder(existing.view)
            return
        }
        guard let service else { return }
        let controller = JobObservationViewController(
            jobID: jobID,
            service: service,
            onDismiss: { [weak self] in self?.removeJobObserver(jobID: jobID) }
        )
        addChild(controller)
        controller.view.translatesAutoresizingMaskIntoConstraints = false
        panelContainer.addSubview(controller.view)
        NSLayoutConstraint.activate([
            controller.view.leadingAnchor.constraint(equalTo: panelContainer.leadingAnchor),
            controller.view.trailingAnchor.constraint(equalTo: panelContainer.trailingAnchor),
            controller.view.topAnchor.constraint(equalTo: panelContainer.topAnchor),
            controller.view.bottomAnchor.constraint(equalTo: panelContainer.bottomAnchor),
        ])
        jobObservers[jobID] = controller
        placeholderLabel.isHidden = true
        controller.beginObservation()
    }

    private func removeJobObserver(jobID: String) {
        guard let controller = jobObservers.removeValue(forKey: jobID) else { return }
        controller.view.removeFromSuperview()
        controller.removeFromParent()
    }

    private func display(document: PanelDocument) {
        if let index = panels.firstIndex(where: { $0.panelID == document.panelID }) {
            panelPicker.selectItem(at: index)
        }

        if let renderer {
            _ = renderer.update(document: document)
            placeholderLabel.isHidden = true
            return
        }
        installRenderer(document: document)
    }

    private func installRenderer(document: PanelDocument) {
        renderer?.view.removeFromSuperview()
        renderer?.removeFromParent()

        let operationIDs = availableOperationIDs
        let nextRenderer = PanelRenderer(
            document: document,
            operationLookup: { operationID in
                operationIDs.contains(operationID)
                    ? TrustedPanelOperationDescriptor(operationID: operationID)
                    : nil
            },
            onActionIntent: { [weak self] intent in self?.invoke(intent) }
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
        placeholderLabel.isHidden = true
    }

    private func removeRenderer(message: String) {
        renderer?.view.removeFromSuperview()
        renderer?.removeFromParent()
        renderer = nil
        placeholderLabel.stringValue = message
        placeholderLabel.isHidden = false
    }

    private func updateControls() {
        let connected = service != nil
        extensionPicker.isEnabled = connected && !extensions.isEmpty
        panelPicker.isEnabled = connected && !panels.isEmpty
        extensionStateButton.isEnabled = connected && selectedExtension != nil
        extensionStateButton.title = selectedExtension?.status == "enabled" ? "Disable" : "Enable"
    }

    private func makeButton(_ title: String, action: Selector) -> NSButton {
        let button = NSButton(title: title, target: self, action: action)
        button.bezelStyle = .rounded
        return button
    }

    private func runInBackground<Result>(
        _ work: @escaping () throws -> Result,
        success: @escaping @MainActor (Result) -> Void
    ) {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let result = try work()
                DispatchQueue.main.async { success(result) }
            } catch {
                DispatchQueue.main.async {
                    self?.statusLabel.stringValue = "Error: \(error)"
                }
            }
        }
    }

    nonisolated private static func decodePanel(_ panel: JSONValue) throws -> PanelDocument {
        try PanelDocumentCodec.decode(JSONEncoder().encode(panel))
    }
}

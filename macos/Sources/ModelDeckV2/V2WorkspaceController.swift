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
    private let extensionUpdateButton = NSButton()
    private let panelPicker = NSPopUpButton()
    private let statusLabel = NSTextField(wrappingLabelWithString: "Starting isolated engine…")
    private let panelContainer = NSView()
    private let placeholderLabel = NSTextField(wrappingLabelWithString: "Enable an extension to show its panels.")
    private let codingLabel = NSTextField(wrappingLabelWithString: "Coding route unavailable")
    private let usageLabel = NSTextField(wrappingLabelWithString: "Usage not loaded")
    private let refreshUsageButton = NSButton()
    private let priceSnapshotLabel = NSTextField(wrappingLabelWithString: "Prices not loaded")
    private let refreshPricesButton = NSButton()
    private let providerSetupController = ProviderSetupViewController()
    private let codexConnectionController = CodexConnectionViewController()
    private let modelManagementController = ModelManagementViewController()

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

        refreshPricesButton.title = "Refresh prices"
        refreshPricesButton.target = self
        refreshPricesButton.action = #selector(refreshPricesRequested)
        priceSnapshotLabel.textColor = .secondaryLabelColor
        let priceControls = NSStackView(views: [refreshPricesButton, priceSnapshotLabel])
        priceControls.spacing = 8
        root.addArrangedSubview(priceControls)

        addChild(providerSetupController)
        root.addArrangedSubview(providerSetupController.view)
        addChild(codexConnectionController)
        root.addArrangedSubview(codexConnectionController.view)
        addChild(modelManagementController)
        root.addArrangedSubview(modelManagementController.view)

        let extensionControls = NSStackView()
        extensionControls.orientation = .horizontal
        extensionControls.spacing = 8
        extensionPicker.target = self
        extensionPicker.action = #selector(extensionSelectionChanged)
        extensionPicker.setContentHuggingPriority(.defaultLow, for: .horizontal)
        extensionControls.addArrangedSubview(extensionPicker)
        extensionControls.addArrangedSubview(makeButton("Install…", action: #selector(chooseExtensionArchive)))
        extensionUpdateButton.title = "Update…"
        extensionUpdateButton.target = self
        extensionUpdateButton.action = #selector(chooseUpdateArchive)
        extensionControls.addArrangedSubview(extensionUpdateButton)
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
        // Chained, not concurrent: both calls serialize on EngineUsageService's
        // own lock anyway (one authenticated connection), and sequencing them
        // keeps `md-N` JSON-RPC request ids — and this attach path — deterministic.
        refreshUsage { [weak self] in self?.loadPriceSnapshot() }
    }

    func attachModelManagement(
        connectionService: EngineConnectionService,
        modelService: EngineModelRegistrationService,
        projectionService: EngineHostProjectionStatusService,
        configuredRoute: V2ConfiguredModelRoute?
    ) {
        modelManagementController.attach(
            connectionService: connectionService,
            modelService: modelService,
            projectionService: projectionService,
            configuredRoute: configuredRoute
        )
    }

    func attachProviderSetup(
        setupService: V2ProviderSetupService,
        configuredRoute: V2ConfiguredModelRoute?,
        providerProfileSaved: @escaping (URL) -> Void
    ) {
        providerSetupController.providerProfileSaved = providerProfileSaved
        providerSetupController.attach(
            setupService: setupService,
            configuredRoute: configuredRoute
        )
        if configuredRoute != nil {
            providerSetupController.showEngineReady()
        }
    }

    func prepareForEngineRestart() {
        codingLabel.stringValue = "Coding route restarting…"
        providerSetupController.showEngineRestart()
        modelManagementController.prepareForEngineRestart()
        codexConnectionController.prepareForEngineRestart()
    }

    func attachCodexConnection(
        hostService: EngineHostService,
        desktopService: V2CodexDesktopConnectionService?
    ) {
        codexConnectionController.attach(
            hostService: hostService,
            desktopService: desktopService
        )
    }

    @objc private func refreshUsageRequested() {
        refreshUsage(completion: nil)
    }

    /// Same work as the "Refresh usage" button; ``completion`` runs on the
    /// main thread whether the query succeeds or fails, so ``attach(usageService:bridgeSummary:)``
    /// can chain ``loadPriceSnapshot()`` after it without racing it for the
    /// engine connection's request ids.
    private func refreshUsage(completion: (() -> Void)?) {
        guard let usageService else {
            completion?()
            return
        }
        usageLabel.stringValue = "Refreshing usage…"
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let records = try usageService.query()
                let totals = records.reduce(into: [String: Double]()) { $0[$1.unitKind, default: 0] += $1.units }
                let parts = ["input_tokens", "output_tokens", "cached_tokens"].compactMap { key in totals[key].map { "\(key): \($0)" } }
                DispatchQueue.main.async {
                    self?.usageLabel.stringValue = parts.isEmpty ? "No completed usage records" : parts.joined(separator: " · ")
                    completion?()
                }
            } catch {
                DispatchQueue.main.async {
                    self?.statusLabel.stringValue = "Error: \(error)"
                    completion?()
                }
            }
        }
    }

    /// Loads the price cache snapshot age/staleness row (five-state
    /// convention: loading/ready/empty/failure/unavailable, same as
    /// ``ModelCatalogPhase`` elsewhere in V2). `unavailable` means the
    /// engine has not wired `prices.query` up yet
    /// (``EngineClientError/unavailable(_:)``); any other error is a
    /// genuine read failure.
    private func loadPriceSnapshot() {
        guard let usageService else {
            applyPriceSnapshot(EvidencePresenter.evidenceFailureSummary(message: "no usage service attached", capabilityMissing: true))
            return
        }
        applyPriceSnapshot(PriceSnapshotSummary(phase: .loading, stale: false, statusMessage: "Loading prices…"))
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let result = try usageService.queryPrices()
                let summary = EvidencePresenter.priceSnapshotSummary(from: result)
                DispatchQueue.main.async { self?.applyPriceSnapshot(summary) }
            } catch {
                let failure = Self.evidenceFailure(error)
                let summary = EvidencePresenter.evidenceFailureSummary(
                    message: failure.message,
                    capabilityMissing: failure.capabilityMissing
                )
                DispatchQueue.main.async { self?.applyPriceSnapshot(summary) }
            }
        }
    }

    /// Splits an engine call failure into the two things the evidence rows
    /// need from it: what to show, and whether the engine simply does not
    /// implement the operation yet (``EngineClientError/unavailable(_:)``)
    /// rather than having genuinely failed the call.
    nonisolated private static func evidenceFailure(_ error: Error) -> (message: String, capabilityMissing: Bool) {
        guard let engineError = error as? EngineClientError else {
            return ("\(error)", false)
        }
        if case .unavailable = engineError {
            return (engineError.description, true)
        }
        return (engineError.description, false)
    }

    private func applyPriceSnapshot(_ summary: PriceSnapshotSummary) {
        priceSnapshotLabel.stringValue = summary.statusMessage
        priceSnapshotLabel.textColor = summary.stale ? .systemOrange : .secondaryLabelColor
        refreshPricesButton.isEnabled = summary.phase != .loading && usageService != nil
    }

    /// Shows the started refresh job's id without claiming a new snapshot
    /// phase: the cached prices are unchanged until the job finishes, so the
    /// row keeps the phase its last read left it in and only re-enables the
    /// button, which the in-flight ``.loading`` state had disabled.
    private func applyPriceRefreshStarted(jobID: String) {
        priceSnapshotLabel.stringValue = EvidencePresenter.refreshStartedMessage(jobID: jobID)
        priceSnapshotLabel.textColor = .secondaryLabelColor
        refreshPricesButton.isEnabled = usageService != nil
    }

    /// Starts a price refresh job. A refresh that cannot even be started
    /// settles the row in `failure`/`unavailable`; it must never leave the
    /// row in the in-flight ``.loading`` state, which keeps the button
    /// disabled and so would strand the user with no way to retry.
    @objc private func refreshPricesRequested() {
        guard let usageService else { return }
        applyPriceSnapshot(PriceSnapshotSummary(phase: .loading, stale: false, statusMessage: "Starting price refresh…"))
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let job = try usageService.refreshPrices(idempotencyKey: EngineUsageService.newIdempotencyKey())
                DispatchQueue.main.async {
                    guard let self else { return }
                    self.applyPriceRefreshStarted(jobID: job.jobID)
                    self.presentJobObservation(jobID: job.jobID, service: usageService)
                }
            } catch {
                let failure = Self.evidenceFailure(error)
                let summary = EvidencePresenter.refreshFailureSummary(
                    message: failure.message,
                    capabilityMissing: failure.capabilityMissing
                )
                DispatchQueue.main.async { self?.applyPriceSnapshot(summary) }
            }
        }
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

    @objc private func chooseUpdateArchive() {
        guard selectedExtension != nil else { return }
        let picker = NSOpenPanel()
        picker.allowedContentTypes = [.zip]
        picker.allowsMultipleSelection = false
        picker.canChooseDirectories = false
        picker.message = "Choose an updated Model Deck extension"
        picker.begin { [weak self] response in
            guard response == .OK, let archive = picker.url else { return }
            self?.updateExtension(at: archive)
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

    private func updateExtension(at archive: URL) {
        guard let service, let selectedExtension else { return }
        let extensionID = selectedExtension.extensionID
        let revision = selectedExtension.revision
        statusLabel.stringValue = "Updating extension…"
        runInBackground(
            { try service.updateExtension(extensionID: extensionID, archivePath: archive.path, expectedRevision: revision) },
            success: { [weak self] version in
                self?.preferredExtensionID = extensionID
                self?.refreshWorkspace(successMessage: "Extension updated to \(version).")
            }
        )
    }

    private func refreshWorkspace(successMessage: String? = nil) {
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
                self.reloadPanelPicker(successMessage: successMessage)
                if panels.isEmpty {
                    self.statusLabel.stringValue = successMessage ?? (details.isEmpty
                        ? "Install an extension to begin."
                        : "Extensions refreshed.")
                }
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

    private func reloadPanelPicker(successMessage: String? = nil) {
        let selectedPanelID = renderer?.document.panelID
        panelPicker.removeAllItems()
        panelPicker.addItems(withTitles: panels.map { $0.title ?? $0.panelID })
        if let selectedPanelID,
           let index = panels.firstIndex(where: { $0.panelID == selectedPanelID }) {
            panelPicker.selectItem(at: index)
            loadPanel(panels[index], successMessage: successMessage)
        } else if let first = panels.first {
            panelPicker.selectItem(at: 0)
            loadPanel(first, successMessage: successMessage)
        } else {
            removeRenderer(message: "No enabled extension panels are available.")
        }
        updateControls()
    }

    private func loadPanel(
        _ panel: ExtensionPanelContribution,
        successMessage: String? = nil
    ) {
        guard let service else { return }
        statusLabel.stringValue = "Loading \(panel.title ?? panel.panelID)…"
        runInBackground(
            { try Self.decodePanel(service.fetchPanel(panelID: panel.panelID)) },
            success: { [weak self] document in
                self?.display(document: document)
                self?.statusLabel.stringValue = successMessage ?? "Panel loaded."
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
                if let jobID, let panelService = self.service {
                    self.presentJobObservation(jobID: jobID, service: panelService)
                    self.statusLabel.stringValue = "Action started async job \(jobID)."
                } else {
                    self.statusLabel.stringValue = "Action completed."
                }
            }
        )
    }

    /// Presents a bounded observation child controller for ``jobID`` against
    /// any ``JobObservationService`` — the extension panel service for
    /// panel-triggered jobs, or ``usageService`` for a
    /// prices/benchmarks refresh job. The controller drives its own polling
    /// via ``engine.v1.jobs.get`` and offers a single "Request cancel"
    /// button. We never parse plugin output here — the child renders the
    /// canonical JSON envelope verbatim.
    private func presentJobObservation(jobID: String, service: JobObservationService) {
        if let existing = jobObservers[jobID] {
            view.window?.makeFirstResponder(existing.view)
            return
        }
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
        extensionUpdateButton.isEnabled = connected && selectedExtension != nil
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

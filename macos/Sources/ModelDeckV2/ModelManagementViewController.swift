import AppKit
import ModelDeckClient

@MainActor
final class ModelManagementViewController: NSViewController {
    private let connectionStatusLabel = NSTextField(wrappingLabelWithString: "Connection not added")
    private let projectionStatusLabel = NSTextField(wrappingLabelWithString: "Host projection not checked")
    private let saveConnectionButton = NSButton()
    private let providerModelField = NSTextField()
    private let displayNameField = NSTextField()
    private let modelPicker = NSPopUpButton()
    private let renamedDisplayNameField = NSTextField()
    private let registerModelButton = NSButton()
    private let renameModelButton = NSButton()
    private let removeModelButton = NSButton()

    private var connectionService: EngineConnectionService?
    private var modelService: EngineModelRegistrationService?
    private var projectionService: EngineHostProjectionStatusService?
    private var configuredRoute: V2ConfiguredModelRoute?
    private var connections: [ConnectionRef] = []
    private var models: [RegisteredModel] = []

    override func loadView() {
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 8

        let heading = NSTextField(labelWithString: "Model management")
        heading.font = .systemFont(ofSize: 15, weight: .semibold)
        root.addArrangedSubview(heading)

        saveConnectionButton.title = "Add configured connection"
        saveConnectionButton.target = self
        saveConnectionButton.action = #selector(saveConnectionRequested)
        root.addArrangedSubview(NSStackView(views: [saveConnectionButton, connectionStatusLabel]))

        providerModelField.placeholderString = "Provider model ID"
        displayNameField.placeholderString = "Display name"
        registerModelButton.title = "Add model"
        registerModelButton.target = self
        registerModelButton.action = #selector(registerModelRequested)
        let registerControls = NSStackView(views: [providerModelField, displayNameField, registerModelButton])
        registerControls.spacing = 8
        root.addArrangedSubview(registerControls)

        modelPicker.target = self
        modelPicker.action = #selector(modelSelectionChanged)
        renamedDisplayNameField.placeholderString = "New display name"
        renameModelButton.title = "Rename"
        renameModelButton.target = self
        renameModelButton.action = #selector(renameModelRequested)
        removeModelButton.title = "Remove"
        removeModelButton.target = self
        removeModelButton.action = #selector(removeModelRequested)
        let modelControls = NSStackView(views: [modelPicker, renamedDisplayNameField, renameModelButton, removeModelButton])
        modelControls.spacing = 8
        root.addArrangedSubview(modelControls)

        projectionStatusLabel.textColor = .secondaryLabelColor
        let refreshButton = NSButton(title: "Refresh model state", target: self, action: #selector(refreshRequested))
        root.addArrangedSubview(NSStackView(views: [refreshButton, projectionStatusLabel]))

        NSLayoutConstraint.activate([
            root.widthAnchor.constraint(greaterThanOrEqualToConstant: 760),
            providerModelField.widthAnchor.constraint(greaterThanOrEqualToConstant: 220),
            displayNameField.widthAnchor.constraint(greaterThanOrEqualToConstant: 180),
            modelPicker.widthAnchor.constraint(greaterThanOrEqualToConstant: 260),
            renamedDisplayNameField.widthAnchor.constraint(greaterThanOrEqualToConstant: 180),
        ])
        view = root
        updateControls()
    }

    func attach(
        connectionService: EngineConnectionService,
        modelService: EngineModelRegistrationService,
        projectionService: EngineHostProjectionStatusService,
        configuredRoute: V2ConfiguredModelRoute?
    ) {
        self.connectionService = connectionService
        self.modelService = modelService
        self.projectionService = projectionService
        self.configuredRoute = configuredRoute
        providerModelField.stringValue = configuredRoute?.providerModelID ?? ""
        displayNameField.stringValue = configuredRoute?.displayName ?? ""
        refresh()
    }

    func prepareForEngineRestart() {
        connectionService = nil
        modelService = nil
        projectionService = nil
        connections = []
        models = []
        connectionStatusLabel.stringValue = "Connection restarting…"
        projectionStatusLabel.stringValue = "Host projection restarting…"
        modelPicker.removeAllItems()
        updateControls()
    }

    @objc private func refreshRequested() {
        refresh()
    }

    @objc private func modelSelectionChanged() {
        renamedDisplayNameField.stringValue = selectedModel?.displayName ?? ""
        updateControls()
    }

    @objc private func saveConnectionRequested() {
        guard let connectionService, let route = configuredRoute else { return }
        let revision = connections.first(where: { $0.connectionID == route.connectionID })?.revision ?? 0
        projectionStatusLabel.stringValue = revision == 0 ? "Adding connection…" : "Updating connection…"
        Task {
            do {
                _ = try await connectionService.saveConnection(
                    input: ConnectionSaveInput(
                        connectionID: route.connectionID,
                        providerID: route.providerID,
                        endpointConfigRef: route.endpointConfigRef,
                        credentialRef: route.credentialRef
                    ),
                    expectedRevision: revision,
                    idempotencyKey: EngineConnectionService.newIdempotencyKey()
                )
                refresh()
            } catch {
                show(error)
                refresh()
            }
        }
    }

    @objc private func registerModelRequested() {
        guard let modelService, let route = configuredRoute else { return }
        let providerModelID = providerModelField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let displayName = displayNameField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !providerModelID.isEmpty else { return }
        projectionStatusLabel.stringValue = "Adding model…"
        Task {
            do {
                _ = try await modelService.registerModel(
                    connectionID: route.connectionID,
                    providerModelID: providerModelID,
                    displayName: displayName,
                    expectedRevision: 0,
                    idempotencyKey: EngineModelRegistrationService.newIdempotencyKey()
                )
                refresh()
            } catch {
                show(error)
                refresh()
            }
        }
    }

    @objc private func renameModelRequested() {
        guard let modelService, let model = selectedModel else { return }
        let name = renamedDisplayNameField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        projectionStatusLabel.stringValue = "Renaming model…"
        Task {
            do {
                _ = try await modelService.renameModel(
                    registrationID: model.registrationID,
                    displayName: name,
                    expectedRevision: model.revision,
                    idempotencyKey: EngineModelRegistrationService.newIdempotencyKey()
                )
                refresh()
            } catch {
                show(error)
                refresh()
            }
        }
    }

    @objc private func removeModelRequested() {
        guard let modelService, let model = selectedModel else { return }
        projectionStatusLabel.stringValue = "Removing model…"
        Task {
            do {
                _ = try await modelService.removeModel(
                    registrationID: model.registrationID,
                    expectedRevision: model.revision,
                    idempotencyKey: EngineModelRegistrationService.newIdempotencyKey()
                )
                refresh()
            } catch {
                show(error)
                refresh()
            }
        }
    }

    private var selectedModel: RegisteredModel? {
        let index = modelPicker.indexOfSelectedItem
        return models.indices.contains(index) ? models[index] : nil
    }

    private func refresh() {
        guard let connectionService, let modelService, let projectionService else {
            updateControls()
            return
        }
        Task {
            do {
                async let connectionRows = connectionService.listConnections()
                async let modelPage = modelService.listRegisteredModels()
                connections = try await connectionRows
                let page = try await modelPage
                models = page.items
                let status: HostProjectionStatus?
                if configuredRoute == nil {
                    status = nil
                } else {
                    status = try await projectionService.status(
                        hostID: "com.modeldeck.host.codex.managed-agent"
                    )
                }
                reload(status: status)
            } catch {
                show(error)
            }
        }
    }

    private func reload(status: HostProjectionStatus?) {
        let configuredConnection = configuredRoute.flatMap { route in
            connections.first(where: { $0.connectionID == route.connectionID })
        }
        connectionStatusLabel.stringValue = configuredConnection.map {
            "Connected at revision \($0.revision)"
        } ?? "Connection not added"
        saveConnectionButton.title = configuredConnection == nil
            ? "Add configured connection"
            : "Update configured connection"

        let selectedRegistrationID = selectedModel?.registrationID
        modelPicker.removeAllItems()
        modelPicker.addItems(withTitles: models.map { "\($0.displayName) — \($0.providerModelID)" })
        if let selectedRegistrationID,
           let index = models.firstIndex(where: { $0.registrationID == selectedRegistrationID }) {
            modelPicker.selectItem(at: index)
        } else if !models.isEmpty {
            modelPicker.selectItem(at: 0)
        }
        renamedDisplayNameField.stringValue = selectedModel?.displayName ?? ""
        projectionStatusLabel.stringValue = status.map {
            "Host projection: \($0.rawValue)"
        } ?? "Host projection unavailable until a provider is configured"
        updateControls()
    }

    private func show(_ error: Error) {
        projectionStatusLabel.stringValue = "Model management error: \(error)"
        updateControls()
    }

    private func updateControls() {
        let connected = connectionService != nil && modelService != nil
        let hasConfiguredRoute = configuredRoute != nil
        saveConnectionButton.isEnabled = connected && hasConfiguredRoute
        registerModelButton.isEnabled = connected && hasConfiguredRoute && !providerModelField.stringValue.isEmpty
        modelPicker.isEnabled = connected && !models.isEmpty
        renameModelButton.isEnabled = connected && selectedModel != nil
        removeModelButton.isEnabled = connected && selectedModel != nil
    }
}

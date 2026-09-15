import AppKit

@MainActor
final class ProviderSetupViewController: NSViewController {
    private let apiKeyField = NSSecureTextField()
    private let modelField = NSTextField()
    private let displayNameField = NSTextField()
    private let saveButton = NSButton()
    private let statusLabel = NSTextField(wrappingLabelWithString: "Configure OpenRouter to enable coding.")

    private var setupService: V2ProviderSetupService?
    private var saveInProgress = false
    var providerProfileSaved: ((URL) -> Void)?

    override func loadView() {
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 8

        let heading = NSTextField(labelWithString: "Provider connection")
        heading.font = .systemFont(ofSize: 15, weight: .semibold)
        root.addArrangedSubview(heading)

        apiKeyField.placeholderString = "OpenRouter API key"
        apiKeyField.setAccessibilityLabel("OpenRouter API key")
        modelField.placeholderString = "Model ID, for example deepseek/deepseek-v4.1-flash"
        modelField.setAccessibilityLabel("OpenRouter model ID")
        displayNameField.placeholderString = "Display name (optional)"
        displayNameField.setAccessibilityLabel("Model display name")
        saveButton.title = "Save connection"
        saveButton.target = self
        saveButton.action = #selector(saveRequested)

        let fields = NSStackView(views: [apiKeyField, modelField, displayNameField, saveButton])
        fields.spacing = 8
        root.addArrangedSubview(fields)
        statusLabel.textColor = .secondaryLabelColor
        statusLabel.maximumNumberOfLines = 2
        root.addArrangedSubview(statusLabel)

        NSLayoutConstraint.activate([
            root.widthAnchor.constraint(greaterThanOrEqualToConstant: 760),
            apiKeyField.widthAnchor.constraint(greaterThanOrEqualToConstant: 180),
            modelField.widthAnchor.constraint(greaterThanOrEqualToConstant: 280),
            displayNameField.widthAnchor.constraint(greaterThanOrEqualToConstant: 180),
        ])
        view = root
        updateControls()
    }

    func attach(setupService: V2ProviderSetupService, configuredRoute: V2ConfiguredModelRoute?) {
        self.setupService = setupService
        if let configuredRoute {
            modelField.stringValue = configuredRoute.providerModelID
            displayNameField.stringValue = configuredRoute.displayName
            statusLabel.stringValue = "OpenRouter is configured. The API key remains in Keychain."
        }
        updateControls()
    }

    func showEngineRestart() {
        statusLabel.stringValue = "Connection saved. Restarting the isolated V2 engine…"
        apiKeyField.stringValue = ""
        saveInProgress = true
        updateControls()
    }

    func showEngineReady() {
        statusLabel.stringValue = "OpenRouter is connected. The registered model is ready below."
        saveInProgress = false
        updateControls()
    }

    func showFailure(_ error: Error) {
        statusLabel.stringValue = "Provider setup failed: \(error)"
        apiKeyField.stringValue = ""
        saveInProgress = false
        updateControls()
    }

    @objc private func saveRequested() {
        guard let setupService, !saveInProgress else { return }
        saveInProgress = true
        statusLabel.stringValue = "Saving the API key in Keychain…"
        updateControls()
        let apiKey = apiKeyField.stringValue
        let model = modelField.stringValue
        let displayName = displayNameField.stringValue
        Task.detached {
            do {
                let result = try setupService.saveOpenRouterConnection(
                    apiKey: apiKey,
                    providerModelID: model,
                    displayName: displayName
                )
                await MainActor.run {
                    self.showEngineRestart()
                    self.providerProfileSaved?(result.profileURL)
                }
            } catch {
                await MainActor.run { self.showFailure(error) }
            }
        }
    }

    private func updateControls() {
        saveButton.isEnabled = setupService != nil && !saveInProgress
        apiKeyField.isEnabled = !saveInProgress
        modelField.isEnabled = !saveInProgress
        displayNameField.isEnabled = !saveInProgress
    }
}

import AppKit
import ModelDeckClient

@MainActor
final class CodexConnectionViewController: NSViewController {
    private let statusLabel = NSTextField(wrappingLabelWithString: "Configure a provider before connecting Codex.")
    private let refreshButton = NSButton()
    private let prepareButton = NSButton()
    private let connectButton = NSButton()

    private var hostService: EngineHostService?
    private var desktopService: V2CodexDesktopConnectionService?
    private var hostAvailable = false
    private var connectorReady = false
    private var actionInProgress = false

    override func loadView() {
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 8
        let heading = NSTextField(labelWithString: "Codex Desktop")
        heading.font = .systemFont(ofSize: 15, weight: .semibold)
        root.addArrangedSubview(heading)

        refreshButton.title = "Check compatibility"
        refreshButton.target = self
        refreshButton.action = #selector(refreshRequested)
        prepareButton.title = "Prepare Codex"
        prepareButton.target = self
        prepareButton.action = #selector(prepareRequested)
        connectButton.title = "Connect Codex"
        connectButton.target = self
        connectButton.action = #selector(connectRequested)
        let buttons = NSStackView(views: [refreshButton, prepareButton, connectButton])
        buttons.spacing = 8
        root.addArrangedSubview(buttons)
        statusLabel.textColor = .secondaryLabelColor
        statusLabel.maximumNumberOfLines = 3
        root.addArrangedSubview(statusLabel)
        view = root
        updateControls()
    }

    func attach(
        hostService: EngineHostService,
        desktopService: V2CodexDesktopConnectionService?
    ) {
        self.hostService = hostService
        self.desktopService = desktopService
        refresh()
    }

    func prepareForEngineRestart() {
        hostService = nil
        desktopService = nil
        hostAvailable = false
        connectorReady = false
        statusLabel.stringValue = "Codex connection is restarting with the provider."
        updateControls()
    }

    @objc private func refreshRequested() {
        refresh()
    }

    @objc private func prepareRequested() {
        guard let hostService, !actionInProgress else { return }
        actionInProgress = true
        statusLabel.stringValue = "Preparing Codex compatibility…"
        updateControls()
        Task {
            do {
                let prepared = try await hostService.prepareHost(hostID: "com.openai.codex")
                statusLabel.stringValue = prepared
                    ? "Codex is prepared. Choose Connect Codex."
                    : "Codex preparation was not available. Check compatibility and try again."
                if prepared {
                    actionInProgress = false
                    refresh()
                    return
                }
            } catch {
                statusLabel.stringValue = actionableMessage(for: error)
            }
            actionInProgress = false
            updateControls()
        }
    }

    @objc private func connectRequested() {
        guard let desktopService, !actionInProgress else { return }
        actionInProgress = true
        statusLabel.stringValue = "Requesting the integrated Codex launch…"
        updateControls()
        Task.detached {
            do {
                let outcome = try desktopService.connect()
                await MainActor.run {
                    self.statusLabel.stringValue = outcome.reason
                    self.connectorReady = outcome.status == .ready
                    self.actionInProgress = false
                    self.updateControls()
                }
            } catch {
                await MainActor.run {
                    self.statusLabel.stringValue = "Codex connection failed: \(error)"
                    self.actionInProgress = false
                    self.updateControls()
                }
            }
        }
    }

    private func refresh() {
        guard let hostService, !actionInProgress else {
            updateControls()
            return
        }
        actionInProgress = true
        connectorReady = false
        statusLabel.stringValue = "Checking Codex compatibility…"
        updateControls()
        Task {
            do {
                let hosts = try await hostService.listHosts()
                hostAvailable = hosts.contains {
                    $0.hostID == "com.openai.codex" && $0.apiProfile == "codex.app-server.v1"
                }
                if !hostAvailable {
                    statusLabel.stringValue = "This Codex installation is unsupported. Update Codex and check again."
                } else if let desktopService {
                    let outcome = try await Task.detached { try desktopService.inspect() }.value
                    statusLabel.stringValue = outcome.reason
                    connectorReady = outcome.status == .ready
                } else {
                    statusLabel.stringValue = "Configure and register a provider model before connecting Codex."
                }
            } catch {
                hostAvailable = false
                statusLabel.stringValue = actionableMessage(for: error)
            }
            actionInProgress = false
            updateControls()
        }
    }

    private func actionableMessage(for error: Error) -> String {
        let message = String(describing: error)
        if message.contains("not_found") {
            return "Codex Desktop was not found in Applications. Install it, then check again."
        }
        if message.contains("version_mismatch") {
            return "This Codex app-server version is unsupported. Update Codex, then check again."
        }
        if message.contains("conflict") || message.localizedCaseInsensitiveContains("already running") {
            return "Codex is already running. Fully quit it, then prepare and connect again."
        }
        return "Codex compatibility check failed: \(message)"
    }

    private func updateControls() {
        refreshButton.isEnabled = hostService != nil && !actionInProgress
        prepareButton.isEnabled = hostAvailable && desktopService != nil && !actionInProgress
        connectButton.isEnabled = connectorReady && desktopService != nil && !actionInProgress
    }
}

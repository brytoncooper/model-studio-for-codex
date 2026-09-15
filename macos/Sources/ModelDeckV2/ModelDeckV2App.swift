import AppKit
import Foundation
import ModelDeckClient
import ModelDeckPlatform

@main
@MainActor
final class ModelDeckV2App: NSObject, NSApplicationDelegate {
    private let engineQueue = DispatchQueue(label: "com.coopertechnology.modeldeck.v2.engine")
    private let workspaceController = V2WorkspaceController()
    private var window: NSWindow?
    private var ownedEngine: V2OwnedEngine?
    private var runtimeConfiguration: V2RuntimeConfiguration?
    private var terminationRequested = false

    static func main() {
        let application = NSApplication.shared
        let delegate = ModelDeckV2App()
        application.delegate = delegate
        application.setActivationPolicy(.regular)
        application.run()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        installMainMenu()
        showWorkspaceWindow()
        startOwnedEngine()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !terminationRequested, let ownedEngine else {
            return ownedEngine == nil ? .terminateNow : .terminateLater
        }
        terminationRequested = true
        engineQueue.async {
            ownedEngine.stop()
            DispatchQueue.main.async {
                sender.reply(toApplicationShouldTerminate: true)
            }
        }
        return .terminateLater
    }

    private func startOwnedEngine() {
        do {
            let configuration = try V2RuntimeConfiguration.load(arguments: CommandLine.arguments)
            runtimeConfiguration = configuration
            attachProviderSetup(configuration: configuration)
            startOwnedEngine(configuration: configuration)
        } catch {
            workspaceController.showStartupFailure(error)
        }
    }

    private func startOwnedEngine(configuration: V2RuntimeConfiguration) {
        let engine = V2OwnedEngine(configuration: configuration)
        ownedEngine = engine
        engineQueue.async { [weak self] in
                do {
                    let connectionFiles = try engine.startAndWaitForConnection()
                    let descriptor = try EngineRendezvousDescriptor.load(
                        from: connectionFiles.rendezvous
                    )
                    let service = EngineExtensionPanelService(
                        rendezvous: descriptor,
                        transport: UnixSocketEngineTransport(socketPath: descriptor.socketPath),
                        credentialProvider: EngineFileCredentialProvider(
                            credentialURL: connectionFiles.credential
                        )
                    )
                    let usageService = EngineUsageService(
                        rendezvous: descriptor,
                        transport: UnixSocketEngineTransport(socketPath: descriptor.socketPath),
                        credentialProvider: EngineFileCredentialProvider(credentialURL: connectionFiles.credential)
                    )
                    let connectionService = EngineConnectionService(
                        rendezvous: descriptor,
                        transport: UnixSocketEngineTransport(socketPath: descriptor.socketPath),
                        credentialProvider: EngineFileCredentialProvider(credentialURL: connectionFiles.credential)
                    )
                    let modelService = EngineModelRegistrationService(
                        rendezvous: descriptor,
                        transport: UnixSocketEngineTransport(socketPath: descriptor.socketPath),
                        credentialProvider: EngineFileCredentialProvider(credentialURL: connectionFiles.credential)
                    )
                    let projectionService = EngineHostProjectionStatusService(
                        rendezvous: descriptor,
                        transport: UnixSocketEngineTransport(socketPath: descriptor.socketPath),
                        credentialProvider: EngineFileCredentialProvider(credentialURL: connectionFiles.credential)
                    )
                    let hostService = EngineHostService(
                        rendezvous: descriptor,
                        transport: UnixSocketEngineTransport(socketPath: descriptor.socketPath),
                        credentialProvider: EngineFileCredentialProvider(credentialURL: connectionFiles.credential)
                    )
                    let configuredRoute = try configuration.providerConfig.map {
                        try V2ConfiguredModelRoute.load(from: $0)
                    }
                    try service.connect()
                    try usageService.connect()
                    try connectionService.connect()
                    try modelService.connect()
                    try projectionService.connect()
                    try hostService.connect()
                    let desktopService = connectionFiles.bridgeSummary.map { _ in
                        let connector = configuration.resourceRoot.appendingPathComponent("CodexDesktopBridge")
                        return V2CodexDesktopConnectionService(
                            connectorURL: connector,
                            engineRendezvousURL: connectionFiles.rendezvous,
                            engineCredentialURL: connectionFiles.credential,
                            bridgeDescriptorURL: configuration.paths.applicationState
                                .appendingPathComponent("engine/codex-bridge.json")
                        )
                    }
                    DispatchQueue.main.async { [weak self] in
                        guard self?.terminationRequested == false else { return }
                        self?.attachProviderSetup(
                            configuration: configuration,
                            configuredRoute: configuredRoute
                        )
                        self?.workspaceController.attach(service: service)
                        self?.workspaceController.attach(usageService: usageService, bridgeSummary: connectionFiles.bridgeSummary)
                        self?.workspaceController.attachModelManagement(
                            connectionService: connectionService,
                            modelService: modelService,
                            projectionService: projectionService,
                            configuredRoute: configuredRoute
                        )
                        self?.workspaceController.attachCodexConnection(
                            hostService: hostService,
                            desktopService: desktopService
                        )
                    }
                } catch {
                    DispatchQueue.main.async { [weak self] in
                        self?.workspaceController.showStartupFailure(error)
                    }
                }
            }
    }

    private func attachProviderSetup(
        configuration: V2RuntimeConfiguration,
        configuredRoute: V2ConfiguredModelRoute? = nil
    ) {
        let helper = configuration.resourceRoot
            .deletingLastPathComponent()
            .appendingPathComponent("Helpers/OpenRouterCredentialHelper")
        let setup = V2ProviderSetupService(
            profileURL: configuration.paths.providerProfile,
            credentialHelperURL: helper
        )
        workspaceController.attachProviderSetup(
            setupService: setup,
            configuredRoute: configuredRoute,
            providerProfileSaved: { [weak self] profileURL in
                self?.restartOwnedEngine(providerConfig: profileURL)
            }
        )
    }

    private func restartOwnedEngine(providerConfig: URL) {
        guard let currentConfiguration = runtimeConfiguration,
              let currentEngine = ownedEngine,
              !terminationRequested else { return }
        workspaceController.prepareForEngineRestart()
        let nextConfiguration = V2RuntimeConfiguration(
            stateRoot: currentConfiguration.paths.stateRoot,
            pythonExecutable: currentConfiguration.pythonExecutable,
            resourceRoot: currentConfiguration.resourceRoot,
            providerConfig: providerConfig
        )
        engineQueue.async { [weak self] in
            currentEngine.stop()
            DispatchQueue.main.async {
                guard let self, !self.terminationRequested else { return }
                self.ownedEngine = nil
                self.runtimeConfiguration = nextConfiguration
                self.startOwnedEngine(configuration: nextConfiguration)
            }
        }
    }

    private func showWorkspaceWindow() {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 900, height: 700),
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Model Deck V2"
        window.contentViewController = workspaceController
        window.center()
        window.makeKeyAndOrderFront(nil)
        self.window = window
        NSApp.activate(ignoringOtherApps: true)
    }

    private func installMainMenu() {
        let mainMenu = NSMenu()
        let applicationItem = NSMenuItem()
        mainMenu.addItem(applicationItem)

        let applicationMenu = NSMenu()
        applicationMenu.addItem(
            withTitle: "Quit Model Deck V2",
            action: #selector(NSApplication.terminate(_:)),
            keyEquivalent: "q"
        )
        applicationItem.submenu = applicationMenu
        NSApp.mainMenu = mainMenu
    }
}

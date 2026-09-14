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
                    try service.connect()
                    try usageService.connect()
                    DispatchQueue.main.async { [weak self] in
                        guard self?.terminationRequested == false else { return }
                        self?.workspaceController.attach(service: service)
                        self?.workspaceController.attach(usageService: usageService, bridgeSummary: connectionFiles.bridgeSummary)
                    }
                } catch {
                    DispatchQueue.main.async { [weak self] in
                        self?.workspaceController.showStartupFailure(error)
                    }
                }
            }
        } catch {
            workspaceController.showStartupFailure(error)
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

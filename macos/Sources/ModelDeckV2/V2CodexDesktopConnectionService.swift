import Foundation

enum V2CodexDesktopConnectionStatus: String, Decodable, Sendable {
    case missingConfiguration = "missing_configuration"
    case missingRuntime = "missing_runtime"
    case unsupportedHost = "unsupported_host"
    case restartRequired = "restart_required"
    case ready
    case connected
}

struct V2CodexDesktopConnectionOutcome: Decodable, Sendable {
    let status: V2CodexDesktopConnectionStatus
    let reason: String
}

protocol V2CodexConnectorRunning {
    func run(executable: URL, arguments: [String]) throws -> (status: Int32, output: Data)
}

struct V2CodexConnectorProcess: V2CodexConnectorRunning {
    func run(executable: URL, arguments: [String]) throws -> (status: Int32, output: Data) {
        let process = Process()
        let output = Pipe()
        process.executableURL = executable
        process.arguments = arguments
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        try process.run()
        let response = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return (process.terminationStatus, response)
    }
}

struct V2CodexDesktopConnectionService {
    let connectorURL: URL
    let engineRendezvousURL: URL
    let engineCredentialURL: URL
    let bridgeDescriptorURL: URL
    let applicationsDirectoryURL: URL
    private let runner: V2CodexConnectorRunning

    init(
        connectorURL: URL,
        engineRendezvousURL: URL,
        engineCredentialURL: URL,
        bridgeDescriptorURL: URL,
        applicationsDirectoryURL: URL = URL(fileURLWithPath: "/Applications", isDirectory: true),
        runner: V2CodexConnectorRunning? = nil
    ) {
        self.connectorURL = connectorURL
        self.engineRendezvousURL = engineRendezvousURL
        self.engineCredentialURL = engineCredentialURL
        self.bridgeDescriptorURL = bridgeDescriptorURL
        self.applicationsDirectoryURL = applicationsDirectoryURL
        self.runner = runner ?? V2CodexConnectorProcess()
    }

    func inspect() throws -> V2CodexDesktopConnectionOutcome {
        try invoke(action: "inspect")
    }

    func connect() throws -> V2CodexDesktopConnectionOutcome {
        try invoke(action: "connect")
    }

    private func invoke(action: String) throws -> V2CodexDesktopConnectionOutcome {
        let arguments = [
            "--model-deck-connect", action,
            "--engine-rendezvous", engineRendezvousURL.path,
            "--engine-credential", engineCredentialURL.path,
            "--bridge-descriptor", bridgeDescriptorURL.path,
            "--bridge-script", connectorURL.path,
            "--applications-dir", applicationsDirectoryURL.path,
            "--protocol-version", "codex.app-server.v1",
        ]
        let result = try runner.run(executable: connectorURL, arguments: arguments)
        let outcome = try JSONDecoder().decode(V2CodexDesktopConnectionOutcome.self, from: result.output)
        if result.status != 0,
           outcome.status == .ready || outcome.status == .connected {
            throw CocoaError(.executableRuntimeMismatch)
        }
        return outcome
    }
}

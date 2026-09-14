import Darwin
import Foundation
import ModelDeckClient

enum V2OwnedEngineError: Error, CustomStringConvertible {
    case alreadyStarted
    case failedToCreateLog(String)
    case exitedDuringStartup(Int32)
    case readinessTimedOut

    var description: String {
        switch self {
        case .alreadyStarted:
            return "the V2 engine is already started"
        case .failedToCreateLog(let path):
            return "could not create V2 engine log: \(path)"
        case .exitedDuringStartup(let status):
            return "the V2 engine exited during startup (status \(status))"
        case .readinessTimedOut:
            return "the V2 engine did not publish readiness within 10 seconds"
        }
    }
}

struct V2EngineConnectionFiles {
    let rendezvous: URL
    let credential: URL
    let bridgeSummary: V2BridgeSummary?
}

struct V2BridgeSummary: Codable, Equatable {
    let schemaVersion: Int
    let baseURL: String
    let provider: String
    let model: String
    let billing: String
    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case baseURL = "base_url"
        case provider = "provider_id"
        case model
        case billing = "billing_description"
        case tokenPath = "token_path"
    }
    let tokenPath: String
}

/// Mutable process handles are confined to the application's serial engine queue.
final class V2OwnedEngine: @unchecked Sendable {
    private let configuration: V2RuntimeConfiguration
    private var process: Process?
    private var standardOutput: FileHandle?
    private var standardError: FileHandle?

    init(configuration: V2RuntimeConfiguration) {
        self.configuration = configuration
    }

    func startAndWaitForConnection() throws -> V2EngineConnectionFiles {
        guard process == nil else { throw V2OwnedEngineError.alreadyStarted }
        try configuration.validateStateRootSafety()
        try configuration.prepareDirectories()
        try removeStaleConnectionFiles()

        let output = try openLog(at: configuration.paths.engineStandardOutput)
        let error = try openLog(at: configuration.paths.engineStandardError)
        standardOutput = output
        standardError = error

        let engine = Process()
        engine.executableURL = configuration.pythonExecutable
        engine.arguments = configuration.engineArguments
        engine.environment = configuration.engineEnvironment
        engine.currentDirectoryURL = configuration.resourceRoot
        engine.standardOutput = output
        engine.standardError = error
        process = engine

        do {
            try engine.run()
            return try waitForConnectionFiles(engine: engine)
        } catch {
            stop()
            throw error
        }
    }

    func stop() {
        guard let engine = process else {
            closeLogs()
            return
        }
        if engine.isRunning {
            _ = Darwin.kill(engine.processIdentifier, SIGINT)
            waitForExit(engine, timeout: 5)
        }
        if engine.isRunning {
            engine.terminate()
            waitForExit(engine, timeout: 2)
        }
        if engine.isRunning {
            _ = Darwin.kill(engine.processIdentifier, SIGKILL)
            waitForExit(engine, timeout: 2)
        }
        process = nil
        closeLogs()
    }

    private func openLog(at url: URL) throws -> FileHandle {
        let fileManager = FileManager.default
        if !fileManager.fileExists(atPath: url.path) {
            guard fileManager.createFile(
                atPath: url.path,
                contents: nil,
                attributes: [.posixPermissions: 0o600]
            ) else {
                throw V2OwnedEngineError.failedToCreateLog(url.path)
            }
        }
        let handle = try FileHandle(forWritingTo: url)
        try handle.truncate(atOffset: 0)
        return handle
    }

    private func removeStaleConnectionFiles() throws {
        let fileManager = FileManager.default
        var urls = [configuration.paths.engineRendezvous, configuration.paths.operatorCredential]
        if configuration.providerConfig != nil {
            let engineState = configuration.paths.applicationState.appendingPathComponent("engine")
            urls.append(engineState.appendingPathComponent("codex-bridge.json"))
            urls.append(engineState.appendingPathComponent("codex-bridge-token"))
        }
        for url in urls {
            if fileManager.fileExists(atPath: url.path) {
                try fileManager.removeItem(at: url)
            }
        }
    }

    private func waitForConnectionFiles(engine: Process) throws -> V2EngineConnectionFiles {
        let deadline = Date().addingTimeInterval(10)
        while Date() < deadline {
            if !engine.isRunning {
                throw V2OwnedEngineError.exitedDuringStartup(engine.terminationStatus)
            }
            let connection = connectionFilesAreReadable()
            if connection.ready {
                return V2EngineConnectionFiles(
                    rendezvous: configuration.paths.engineRendezvous,
                    credential: configuration.paths.operatorCredential,
                    bridgeSummary: connection.summary
                )
            }
            Thread.sleep(forTimeInterval: 0.05)
        }
        throw V2OwnedEngineError.readinessTimedOut
    }

    private func connectionFilesAreReadable() -> (ready: Bool, summary: V2BridgeSummary?) {
        guard
            let descriptor = try? EngineRendezvousDescriptor.load(
                from: configuration.paths.engineRendezvous
            ),
            socketBelongsToV2(descriptor.socketPath),
            let credential = try? String(
                contentsOf: configuration.paths.operatorCredential,
                encoding: .utf8
            )
        else {
            return (false, nil)
        }
        guard !credential.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return (false, nil) }
        guard configuration.providerConfig != nil else { return (true, nil) }
        guard let data = try? Data(contentsOf: configuration.paths.applicationState.appendingPathComponent("engine/codex-bridge.json")),
              let summary = try? JSONDecoder().decode(V2BridgeSummary.self, from: data),
              summary.schemaVersion == 1, summary.baseURL.hasPrefix("http://127.0.0.1:") && summary.baseURL.hasSuffix("/v1"),
              !summary.provider.isEmpty, !summary.model.isEmpty, !summary.billing.isEmpty,
              summary.tokenPath == configuration.paths.applicationState.appendingPathComponent("engine/codex-bridge-token").path else { return (false, nil) }
        return (true, summary)
    }

    private func socketBelongsToV2(_ socketPath: String) -> Bool {
        let publishedDirectory = URL(fileURLWithPath: socketPath).deletingLastPathComponent()
        guard
            let publishedPath = realPath(publishedDirectory),
            let ownedPath = realPath(configuration.paths.sockets)
        else {
            return false
        }
        return publishedPath == ownedPath
    }

    private func realPath(_ url: URL) -> String? {
        var resolved = [CChar](repeating: 0, count: Int(PATH_MAX))
        let result = url.path.withCString { path in
            Darwin.realpath(path, &resolved)
        }
        guard result != nil else { return nil }
        return String(cString: resolved)
    }

    private func waitForExit(_ engine: Process, timeout: TimeInterval) {
        let deadline = Date().addingTimeInterval(timeout)
        while engine.isRunning, Date() < deadline {
            Thread.sleep(forTimeInterval: 0.05)
        }
    }

    private func closeLogs() {
        try? standardOutput?.close()
        try? standardError?.close()
        standardOutput = nil
        standardError = nil
    }

    deinit {
        stop()
    }
}

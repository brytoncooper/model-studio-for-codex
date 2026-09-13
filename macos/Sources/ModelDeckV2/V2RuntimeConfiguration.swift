import Foundation

enum V2RuntimeConfigurationError: Error, CustomStringConvertible {
    case usage
    case stateRootMustBeAbsolute
    case missingResource(String)
    case invalidRuntimeConfiguration
    case unsafeStatePath(String)
    case protectedStatePath(String)

    var description: String {
        switch self {
        case .usage:
            return "usage: ModelDeckV2 [--state-root ABSOLUTE_PATH]"
        case .stateRootMustBeAbsolute:
            return "--state-root must be an absolute path"
        case .missingResource(let name):
            return "V2 app resource is missing: \(name)"
        case .invalidRuntimeConfiguration:
            return "V2 runtime configuration is invalid"
        case .unsafeStatePath(let path):
            return "V2 state path must not be a symbolic link: \(path)"
        case .protectedStatePath(let path):
            return "V2 state path overlaps protected live state or a source checkout: \(path)"
        }
    }
}

struct V2RuntimePaths: Equatable, Sendable {
    let stateRoot: URL
    let applicationState: URL
    let applicationArtifacts: URL
    let sockets: URL
    let emptyLegacyAgents: URL
    let extensionState: URL
    let extensionArtifacts: URL
    let logs: URL
    let temporaryFiles: URL

    var engineRendezvous: URL {
        applicationState.appendingPathComponent("engine/rendezvous.json")
    }

    var operatorCredential: URL {
        applicationState.appendingPathComponent("engine/operator_credential")
    }

    var engineStandardOutput: URL {
        logs.appendingPathComponent("engine.stdout.log")
    }

    var engineStandardError: URL {
        logs.appendingPathComponent("engine.stderr.log")
    }

    var directories: [URL] {
        [
            stateRoot,
            applicationState,
            applicationArtifacts,
            sockets,
            emptyLegacyAgents,
            extensionState,
            extensionArtifacts,
            logs,
            temporaryFiles,
        ]
    }
}

struct V2RuntimeConfiguration: Equatable, Sendable {
    let pythonExecutable: URL
    let resourceRoot: URL
    let paths: V2RuntimePaths

    init(stateRoot: URL, pythonExecutable: URL, resourceRoot: URL) {
        let root = stateRoot.standardizedFileURL
        self.pythonExecutable = pythonExecutable.standardizedFileURL
        self.resourceRoot = resourceRoot.standardizedFileURL.resolvingSymlinksInPath()
        self.paths = V2RuntimePaths(
            stateRoot: root,
            applicationState: root.appendingPathComponent("application-state", isDirectory: true),
            applicationArtifacts: root.appendingPathComponent("application-artifacts", isDirectory: true),
            sockets: root.appendingPathComponent("sockets", isDirectory: true),
            emptyLegacyAgents: root.appendingPathComponent("legacy-agents-empty", isDirectory: true),
            extensionState: root.appendingPathComponent("extension-state", isDirectory: true),
            extensionArtifacts: root.appendingPathComponent("extension-artifacts", isDirectory: true),
            logs: root.appendingPathComponent("logs", isDirectory: true),
            temporaryFiles: root.appendingPathComponent("temporary", isDirectory: true)
        )
    }

    static func stateRoot(
        arguments: [String],
        homeDirectory: URL = FileManager.default.homeDirectoryForCurrentUser
    ) throws -> URL {
        if arguments.count == 1 {
            return homeDirectory
                .appendingPathComponent("Library/Application Support", isDirectory: true)
                .appendingPathComponent("Model Deck V2", isDirectory: true)
        }
        guard arguments.count == 3, arguments[1] == "--state-root" else {
            throw V2RuntimeConfigurationError.usage
        }
        let stateRoot = URL(fileURLWithPath: arguments[2], isDirectory: true)
        guard arguments[2].hasPrefix("/") else {
            throw V2RuntimeConfigurationError.stateRootMustBeAbsolute
        }
        return stateRoot
    }

    static func load(arguments: [String], bundle: Bundle = .main) throws -> V2RuntimeConfiguration {
        guard let resources = bundle.resourceURL else {
            throw V2RuntimeConfigurationError.missingResource("Contents/Resources")
        }
        let runtimeURL = resources.appendingPathComponent("config/runtime.plist")
        guard
            let runtime = NSDictionary(contentsOf: runtimeURL),
            let pythonPath = runtime["python_executable"] as? String,
            pythonPath.hasPrefix("/"),
            FileManager.default.isExecutableFile(atPath: pythonPath)
        else {
            throw V2RuntimeConfigurationError.invalidRuntimeConfiguration
        }
        return V2RuntimeConfiguration(
            stateRoot: try stateRoot(arguments: arguments),
            pythonExecutable: URL(fileURLWithPath: pythonPath),
            resourceRoot: resources
        )
    }

    func validateStateRootSafety(
        fileManager: FileManager = .default,
        homeDirectory: URL = FileManager.default.homeDirectoryForCurrentUser
    ) throws {
        try rejectSymlinkAliases(fileManager: fileManager)

        let home = homeDirectory.standardizedFileURL.resolvingSymlinksInPath()
        let applicationSupport = home.appendingPathComponent("Library/Application Support")
        let protectedPaths = [
            home.appendingPathComponent(".codex"),
            applicationSupport.appendingPathComponent("Model Deck"),
            applicationSupport.appendingPathComponent("Codex OpenRouter"),
            URL(fileURLWithPath: "/Applications/Model Deck.app"),
            URL(fileURLWithPath: "/Applications/OpenRouter Settings.app"),
        ]

        let candidate = paths.stateRoot.standardizedFileURL.resolvingSymlinksInPath()
        if protectedPaths.contains(where: { pathsOverlap(candidate, $0) }) {
            throw V2RuntimeConfigurationError.protectedStatePath(paths.stateRoot.path)
        }

        var ancestor = paths.stateRoot.standardizedFileURL
        while ancestor.path != "/" {
            if isModelDeckSourceRoot(ancestor, fileManager: fileManager)
                || (ancestor.pathExtension == "app" && ancestor.lastPathComponent == "Model Deck.app") {
                throw V2RuntimeConfigurationError.protectedStatePath(paths.stateRoot.path)
            }
            ancestor.deleteLastPathComponent()
        }
    }

    func prepareDirectories(fileManager: FileManager = .default) throws {
        for directory in paths.directories {
            var isDirectory: ObjCBool = false
            if fileManager.fileExists(atPath: directory.path, isDirectory: &isDirectory) {
                let values = try directory.resourceValues(forKeys: [.isSymbolicLinkKey])
                guard isDirectory.boolValue, values.isSymbolicLink != true else {
                    throw V2RuntimeConfigurationError.unsafeStatePath(directory.path)
                }
            } else {
                try fileManager.createDirectory(
                    at: directory,
                    withIntermediateDirectories: false,
                    attributes: [.posixPermissions: 0o700]
                )
            }
        }
    }

    private func rejectSymlinkAliases(fileManager: FileManager) throws {
        var candidate = paths.stateRoot.standardizedFileURL
        while candidate.path != "/" {
            if fileManager.fileExists(atPath: candidate.path) {
                let values = try candidate.resourceValues(forKeys: [.isSymbolicLinkKey])
                if values.isSymbolicLink == true && !Self.isPermittedTemporaryAlias(candidate) {
                    throw V2RuntimeConfigurationError.unsafeStatePath(candidate.path)
                }
            }
            candidate.deleteLastPathComponent()
        }
    }

    private static func isPermittedTemporaryAlias(_ url: URL) -> Bool {
        let path = url.standardizedFileURL.path
        let expectedDestination: String
        switch path {
        case "/tmp":
            expectedDestination = "private/tmp"
        case "/var":
            expectedDestination = "private/var"
        default:
            return false
        }
        return (try? FileManager.default.destinationOfSymbolicLink(atPath: path))
            == expectedDestination
    }

    private func pathsOverlap(_ left: URL, _ right: URL) -> Bool {
        let leftPath = left.standardizedFileURL.resolvingSymlinksInPath().path
        let rightPath = right.standardizedFileURL.resolvingSymlinksInPath().path
        return leftPath == rightPath
            || leftPath.hasPrefix(rightPath + "/")
            || rightPath.hasPrefix(leftPath + "/")
    }

    private func isModelDeckSourceRoot(_ url: URL, fileManager: FileManager) -> Bool {
        fileManager.fileExists(atPath: url.appendingPathComponent("local_router.py").path)
            && fileManager.fileExists(atPath: url.appendingPathComponent("build.sh").path)
    }

    var engineArguments: [String] {
        [
            "-B",
            "-m", "model_deck.cli.main",
            "engine", "serve",
            "--state-root", paths.applicationState.path,
            "--artifact-root", paths.applicationArtifacts.path,
            "--socket-root", paths.sockets.path,
            "--legacy-agents-dir", paths.emptyLegacyAgents.path,
            "--enable-application-state",
            "--enable-extensions",
            "--extension-state-root", paths.extensionState.path,
            "--extension-artifact-root", paths.extensionArtifacts.path,
        ]
    }

    var engineEnvironment: [String: String] {
        [
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": resourceRoot.appendingPathComponent("python/src").path,
            "TMPDIR": paths.temporaryFiles.path,
        ]
    }
}

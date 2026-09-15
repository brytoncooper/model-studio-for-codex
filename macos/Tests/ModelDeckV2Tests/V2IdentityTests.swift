import XCTest
@testable import ModelDeckV2

final class V2IdentityTests: XCTestCase {
    private let stateRoot = URL(fileURLWithPath: "/tmp/model-deck-v2-state")
    private let python = URL(fileURLWithPath: "/opt/model-deck-v2-python")
    private let resources = URL(fileURLWithPath: "/Applications/Model Deck V2.app/Contents/Resources")

    func testRuntimeUsesSeparateStateRoots() {
        let configuration = makeConfiguration()

        let canonicalRoot = stateRoot
        XCTAssertEqual(configuration.paths.stateRoot.path, canonicalRoot.path)
        XCTAssertEqual(configuration.paths.applicationState.path, canonicalRoot.appendingPathComponent("application-state").path)
        XCTAssertEqual(configuration.paths.applicationArtifacts.path, canonicalRoot.appendingPathComponent("application-artifacts").path)
        XCTAssertEqual(configuration.paths.sockets.path, canonicalRoot.appendingPathComponent("sockets").path)
        XCTAssertEqual(
            configuration.paths.managedCodexAgents.path,
            canonicalRoot.appendingPathComponent("codex-harness/codex-home/agents").path
        )
        XCTAssertEqual(configuration.paths.extensionState.path, canonicalRoot.appendingPathComponent("extension-state").path)
        XCTAssertEqual(configuration.paths.extensionArtifacts.path, canonicalRoot.appendingPathComponent("extension-artifacts").path)
        XCTAssertEqual(Set(configuration.paths.directories.map(\.path)).count, configuration.paths.directories.count)
    }

    func testEngineArgumentsUseOnlyV2Paths() {
        let configuration = makeConfiguration()
        let arguments = configuration.engineArguments

        XCTAssertEqual(Array(arguments.prefix(5)), ["-B", "-m", "model_deck.cli.main", "engine", "serve"])
        XCTAssertTrue(arguments.contains("--enable-application-state"))
        XCTAssertTrue(arguments.contains("--enable-extensions"))
        let canonicalRoot = stateRoot
        XCTAssertTrue(arguments.contains(canonicalRoot.appendingPathComponent("application-state").path))
        XCTAssertTrue(arguments.contains(canonicalRoot.appendingPathComponent("extension-state").path))
        XCTAssertTrue(arguments.contains(configuration.paths.managedCodexAgents.path))
        XCTAssertFalse(arguments.contains("--rendezvous"))
    }

    func testEngineArgumentsOmitBridgeWhenProviderIsNotConfigured() {
        let arguments = makeConfiguration().engineArguments
        XCTAssertFalse(arguments.contains("--provider-config"))
        XCTAssertFalse(arguments.contains("--enable-codex-bridge"))
        XCTAssertFalse(arguments.contains("--enable-codex-projection"))
    }

    func testRuntimePathsKeepProviderProfileInsideV2State() {
        let configuration = makeConfiguration()
        XCTAssertEqual(
            configuration.paths.providerProfile.path,
            stateRoot.appendingPathComponent("application-state/setup/openrouter-provider.json").path
        )
    }

    func testEngineArgumentsEnableProjectionWhenProviderIsConfigured() {
        let configuration = V2RuntimeConfiguration(
            stateRoot: stateRoot,
            pythonExecutable: python,
            resourceRoot: resources,
            providerConfig: URL(fileURLWithPath: "/tmp/provider.json")
        )
        XCTAssertTrue(configuration.engineArguments.contains("--enable-codex-projection"))
    }

    func testEngineEnvironmentIsExplicitAndSanitized() {
        let environment = makeConfiguration().engineEnvironment

        XCTAssertEqual(environment["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")
        XCTAssertEqual(environment["PYTHONPATH"], resources.appendingPathComponent("python/src").path)
        XCTAssertEqual(environment["TMPDIR"], stateRoot.appendingPathComponent("temporary").path)
        XCTAssertNil(environment["HOME"])
        XCTAssertNil(environment["OPENAI_API_KEY"])
        XCTAssertEqual(
            Set(environment.keys),
            ["PATH", "LANG", "LC_ALL", "PYTHONDONTWRITEBYTECODE", "PYTHONPATH", "TMPDIR"]
        )
    }

    func testStateRootAcceptsOnlyAnOptionalAbsoluteOverride() throws {
        let home = URL(fileURLWithPath: "/Users/example")
        XCTAssertEqual(
            try V2RuntimeConfiguration.stateRoot(arguments: ["ModelDeckV2"], homeDirectory: home).path,
            "/Users/example/Library/Application Support/Model Deck V2"
        )
        XCTAssertEqual(
            try V2RuntimeConfiguration.stateRoot(
                arguments: ["ModelDeckV2", "--state-root", "/tmp/acceptance"],
                homeDirectory: home
            ).path,
            "/tmp/acceptance"
        )
        XCTAssertThrowsError(
            try V2RuntimeConfiguration.stateRoot(
                arguments: ["ModelDeckV2", "--state-root", "relative"],
                homeDirectory: home
            )
        )
        XCTAssertThrowsError(
            try V2RuntimeConfiguration.stateRoot(
                arguments: ["ModelDeckV2", "--unexpected"],
                homeDirectory: home
            )
        )
    }

    func testSafetyRejectsProtectedLiveStateBeforeCreatingDirectories() {
        let home = URL(fileURLWithPath: "/Users/example")
        let protectedRoots = [
            home,
            home.appendingPathComponent(".codex/v2"),
            home.appendingPathComponent("Library/Application Support/Model Deck/v2"),
        ]

        for protectedRoot in protectedRoots {
            let configuration = V2RuntimeConfiguration(
                stateRoot: protectedRoot,
                pythonExecutable: python,
                resourceRoot: resources
            )
            XCTAssertThrowsError(
                try configuration.validateStateRootSafety(homeDirectory: home)
            )
            XCTAssertFalse(
                FileManager.default.fileExists(
                    atPath: protectedRoot.appendingPathComponent("application-state").path
                )
            )
        }
    }

    func testSafetyRejectsSourceCheckoutAndSymlinkedParentBeforeCreatingDirectories() throws {
        let temporaryRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: temporaryRoot, withIntermediateDirectories: false)
        addTeardownBlock {
            try? FileManager.default.removeItem(at: temporaryRoot)
        }

        let sourceRoot = temporaryRoot.appendingPathComponent("Model Deck", isDirectory: true)
        try FileManager.default.createDirectory(at: sourceRoot, withIntermediateDirectories: false)
        XCTAssertTrue(FileManager.default.createFile(atPath: sourceRoot.appendingPathComponent("local_router.py").path, contents: Data()))
        XCTAssertTrue(FileManager.default.createFile(atPath: sourceRoot.appendingPathComponent("build.sh").path, contents: Data()))
        let sourceState = sourceRoot.appendingPathComponent("v2-state", isDirectory: true)
        let sourceConfiguration = V2RuntimeConfiguration(
            stateRoot: sourceState,
            pythonExecutable: python,
            resourceRoot: resources
        )
        XCTAssertThrowsError(
            try sourceConfiguration.validateStateRootSafety(homeDirectory: URL(fileURLWithPath: "/Users/example"))
        )
        XCTAssertFalse(FileManager.default.fileExists(atPath: sourceState.path))

        let realParent = temporaryRoot.appendingPathComponent("real", isDirectory: true)
        let linkedParent = temporaryRoot.appendingPathComponent("linked", isDirectory: true)
        try FileManager.default.createDirectory(at: realParent, withIntermediateDirectories: false)
        try FileManager.default.createSymbolicLink(at: linkedParent, withDestinationURL: realParent)
        let linkedState = linkedParent.appendingPathComponent("v2-state", isDirectory: true)
        let linkedConfiguration = V2RuntimeConfiguration(
            stateRoot: linkedState,
            pythonExecutable: python,
            resourceRoot: resources
        )
        XCTAssertThrowsError(
            try linkedConfiguration.validateStateRootSafety(homeDirectory: URL(fileURLWithPath: "/Users/example"))
        )
        XCTAssertFalse(FileManager.default.fileExists(atPath: realParent.appendingPathComponent("v2-state").path))
    }

    func testPrepareDirectoriesCreatesManagedCodexParentChain() throws {
        let temporaryRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        addTeardownBlock {
            try? FileManager.default.removeItem(at: temporaryRoot)
        }
        let configuration = V2RuntimeConfiguration(
            stateRoot: temporaryRoot.appendingPathComponent("state", isDirectory: true),
            pythonExecutable: python,
            resourceRoot: resources
        )

        try configuration.validateStateRootSafety(
            homeDirectory: URL(fileURLWithPath: "/Users/example")
        )
        try configuration.prepareDirectories()

        var isDirectory = ObjCBool(false)
        XCTAssertTrue(
            FileManager.default.fileExists(
                atPath: configuration.paths.managedCodexAgents.path,
                isDirectory: &isDirectory
            )
        )
        XCTAssertTrue(isDirectory.boolValue)
    }

    private func makeConfiguration() -> V2RuntimeConfiguration {
        V2RuntimeConfiguration(
            stateRoot: stateRoot,
            pythonExecutable: python,
            resourceRoot: resources
        )
    }
}

import Foundation
import XCTest
@testable import ModelDeckV2

private final class RecordingCodexConnectorRunner: V2CodexConnectorRunning {
    private(set) var executable: URL?
    private(set) var arguments: [String] = []
    let outcome: String

    init(outcome: String) {
        self.outcome = outcome
    }

    func run(executable: URL, arguments: [String]) throws -> (status: Int32, output: Data) {
        self.executable = executable
        self.arguments = arguments
        return (0, Data(outcome.utf8))
    }
}

final class V2CodexDesktopConnectionServiceTests: XCTestCase {
    func testConnectInvokesPackagedConnectorWithOnlyIsolatedPaths() throws {
        let runner = RecordingCodexConnectorRunner(
            outcome: #"{"status":"connected","reason":"launch requested"}"#
        )
        let connector = URL(fileURLWithPath: "/tmp/Model Deck V2.app/Contents/Resources/CodexDesktopBridge")
        let service = V2CodexDesktopConnectionService(
            connectorURL: connector,
            engineRendezvousURL: URL(fileURLWithPath: "/tmp/v2/application-state/engine/rendezvous.json"),
            engineCredentialURL: URL(fileURLWithPath: "/tmp/v2/application-state/engine/operator_credential"),
            bridgeDescriptorURL: URL(fileURLWithPath: "/tmp/v2/application-state/engine/codex-bridge.json"),
            applicationsDirectoryURL: URL(fileURLWithPath: "/tmp/Applications"),
            runner: runner
        )

        let outcome = try service.connect()

        XCTAssertEqual(outcome.status, .connected)
        XCTAssertEqual(runner.executable, connector)
        XCTAssertEqual(Array(runner.arguments.prefix(2)), ["--model-deck-connect", "connect"])
        XCTAssertTrue(runner.arguments.contains("/tmp/v2/application-state/engine/rendezvous.json"))
        XCTAssertTrue(runner.arguments.contains("/tmp/v2/application-state/engine/operator_credential"))
        XCTAssertFalse(runner.arguments.joined().contains("secret"))
    }
}

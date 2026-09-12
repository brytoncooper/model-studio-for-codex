import XCTest
@testable import ModelDeckClient

final class ModelCatalogBootstrapTests: XCTestCase {
    func testDefaultSelectsLegacyAdapter() {
        let service = ModelCatalogBootstrap.makeService(environment: [:]) { _, completion in
            completion(["ok": true, "models": []])
        }
        XCTAssertTrue(service is LegacyModelCatalogService)
    }

    func testInvalidRendezvousEnvDoesNotFallBackToLegacy() throws {
        let temp = FileManager.default.temporaryDirectory
        let badFile = temp.appendingPathComponent("rendezvous-bad-\(UUID().uuidString).json")
        try "{\"transport\":\"unix\"}".write(to: badFile, atomically: true, encoding: .utf8)
        let service = ModelCatalogBootstrap.makeService(environment: [
            ModelCatalogBootstrap.rendezvousEnvironmentKey: badFile.path,
        ]) { _, completion in
            completion(["ok": true, "models": []])
        }
        XCTAssertFalse(service is LegacyModelCatalogService)
        XCTAssertTrue(service is UnavailableModelCatalogService)
    }

    func testConfiguredEngineBootstrapSelectsEngineService() throws {
        let temp = FileManager.default.temporaryDirectory
        let rendezvous = temp.appendingPathComponent("rendezvous-\(UUID().uuidString).json")
        let fixture = """
        {
          "transport": "unix",
          "socket_path": "/tmp/model-deck-engine.sock",
          "engine_instance_id": "550e8400-e29b-41d4-a716-446655440000",
          "instance_nonce": "rendezvous-nonce-7f3a",
          "api_profile": { "major": 1, "minor": 0 }
        }
        """
        try fixture.write(to: rendezvous, atomically: true, encoding: .utf8)
        let service = ModelCatalogBootstrap.makeService(
            environment: [ModelCatalogBootstrap.rendezvousEnvironmentKey: rendezvous.path],
            legacyRequestHandler: { _, completion in completion(["ok": true, "models": []]) },
            engineTransportFactory: { _ in FakeEngineTransport() }
        )
        XCTAssertTrue(service is EngineModelCatalogService)
    }
}

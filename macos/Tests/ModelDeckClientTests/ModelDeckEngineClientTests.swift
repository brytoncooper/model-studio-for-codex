import XCTest
import ModelDeckContracts
@testable import ModelDeckClient

private final class FixedCredentialProvider: EngineInstanceCredentialProviding {
    let value: String
    private(set) var readCount = 0

    init(value: String) {
        self.value = value
    }

    func readCredential() throws -> String {
        readCount += 1
        return value
    }
}

private final class TrackingCredentialProvider: EngineInstanceCredentialProviding {
    private(set) var readCount = 0
    func readCredential() throws -> String {
        readCount += 1
        return "operator-secret"
    }
}

final class ModelDeckEngineClientTests: XCTestCase {
    private let apiProfile = EngineAPIProfile(major: 1, minor: 0)
    private let connectionID = "550e8400-e29b-41d4-a716-446655440002"

    func testRendezvousMinimalFixtureShape() throws {
        let mapping = """
        {
          "transport": "unix",
          "socket_path": "/tmp/model-deck-engine.sock",
          "engine_instance_id": "550e8400-e29b-41d4-a716-446655440000",
          "instance_nonce": "rendezvous-nonce-7f3a",
          "api_profile": { "major": 1, "minor": 0 }
        }
        """.data(using: .utf8)!
        let fixtureURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("rendezvous-minimal-\(UUID().uuidString).json")
        try mapping.write(to: fixtureURL)
        defer { try? FileManager.default.removeItem(at: fixtureURL) }
        let descriptor = try EngineRendezvousDescriptor.load(from: fixtureURL)
        XCTAssertEqual(descriptor.transport, "unix")
        XCTAssertEqual(descriptor.socketPath, "/tmp/model-deck-engine.sock")
        XCTAssertEqual(descriptor.engineInstanceID, "550e8400-e29b-41d4-a716-446655440000")
        XCTAssertEqual(descriptor.instanceNonce, "rendezvous-nonce-7f3a")
        XCTAssertEqual(descriptor.apiProfile, apiProfile)
    }

    func testRendezvousRejectsExtraField() {
        let mapping: [String: Any] = [
            "transport": "unix",
            "socket_path": "/tmp/model-deck-engine.sock",
            "engine_instance_id": "550e8400-e29b-41d4-a716-446655440000",
            "instance_nonce": "rendezvous-nonce-7f3a",
            "api_profile": ["major": 1, "minor": 0],
            "credential": "secret",
        ]
        XCTAssertThrowsError(try EngineRendezvousDescriptor.parse(mapping))
    }

    func testRendezvousRejectsRelativeSocketPath() {
        let mapping: [String: Any] = [
            "transport": "unix",
            "socket_path": "tmp/model-deck-engine.sock",
            "engine_instance_id": "550e8400-e29b-41d4-a716-446655440000",
            "instance_nonce": "rendezvous-nonce-7f3a",
            "api_profile": ["major": 1, "minor": 0],
        ]
        XCTAssertThrowsError(try EngineRendezvousDescriptor.parse(mapping)) { error in
            XCTAssertEqual((error as? EngineClientError)?.description, "socket_path must be absolute")
        }
    }

    func testRendezvousRejectsBooleanMajorInAPIProfile() throws {
        let json = """
        {
          "transport": "unix",
          "socket_path": "/tmp/model-deck-engine.sock",
          "engine_instance_id": "550e8400-e29b-41d4-a716-446655440000",
          "instance_nonce": "rendezvous-nonce-7f3a",
          "api_profile": { "major": true, "minor": 0 }
        }
        """.data(using: .utf8)!
        let mapping = try JSONSerialization.jsonObject(with: json) as! [String: Any]
        XCTAssertThrowsError(try EngineRendezvousDescriptor.parse(mapping))
    }

    func testRendezvousRejectsFractionalMajorInAPIProfile() throws {
        let json = """
        {
          "transport": "unix",
          "socket_path": "/tmp/model-deck-engine.sock",
          "engine_instance_id": "550e8400-e29b-41d4-a716-446655440000",
          "instance_nonce": "rendezvous-nonce-7f3a",
          "api_profile": { "major": 1.9, "minor": 0 }
        }
        """.data(using: .utf8)!
        let mapping = try JSONSerialization.jsonObject(with: json) as! [String: Any]
        XCTAssertThrowsError(try EngineRendezvousDescriptor.parse(mapping))
    }

    func testRendezvousRejectsNegativeMinorInAPIProfile() {
        let mapping: [String: Any] = [
            "transport": "unix",
            "socket_path": "/tmp/model-deck-engine.sock",
            "engine_instance_id": "550e8400-e29b-41d4-a716-446655440000",
            "instance_nonce": "rendezvous-nonce-7f3a",
            "api_profile": ["major": 1, "minor": -1],
        ]
        XCTAssertThrowsError(try EngineRendezvousDescriptor.parse(mapping))
    }


    func testFirstHelloMismatchNeverReadsCredential() throws {
        let hello1 = try ContractJSON.loadFixture(named: "hello_result_unauthenticated.json")
        let transport = FakeEngineTransport(responses: [wrap(id: "md-1", result: try jsonObject(hello1))])
        let credentials = TrackingCredentialProvider()
        let descriptor = standardDescriptor(
            engineInstanceID: "00000000-0000-0000-0000-000000000001"
        )
        let client = ModelDeckEngineClient(transport: transport, credentialProvider: credentials)
        XCTAssertThrowsError(try client.connectAndAuthenticate(descriptor: descriptor)) { error in
            XCTAssertEqual(error as? EngineClientError, .rendezvousMismatch)
        }
        XCTAssertEqual(credentials.readCount, 0)
    }


    func testFirstHelloMalformedApiProfileMapsToNegotiationFailed() throws {
        let hello1: [String: Any] = [
            "authenticated": false,
            "api_profile": ["major": true, "minor": 0],
            "engine_instance_id": "550e8400-e29b-41d4-a716-446655440000",
            "instance_nonce": "rendezvous-nonce-7f3a",
            "capabilities": ["features": ["tools": "supported"]],
        ]
        let transport = FakeEngineTransport(responses: [wrap(id: "md-1", result: hello1)])
        let credentials = TrackingCredentialProvider()
        let client = ModelDeckEngineClient(transport: transport, credentialProvider: credentials)
        XCTAssertThrowsError(try client.connectAndAuthenticate(descriptor: standardDescriptor())) { error in
            guard case EngineClientError.negotiationFailed = error else {
                return XCTFail("expected negotiationFailed, got \(error)")
            }
        }
        XCTAssertEqual(credentials.readCount, 0)
    }

    func testFirstHelloApiProfileMismatchNeverReadsCredential() throws {
        let hello1: [String: Any] = [
            "authenticated": false,
            "api_profile": ["major": 2, "minor": 0],
            "engine_instance_id": "550e8400-e29b-41d4-a716-446655440000",
            "instance_nonce": "rendezvous-nonce-7f3a",
            "capabilities": ["features": ["tools": "supported"]],
        ]
        let transport = FakeEngineTransport(responses: [wrap(id: "md-1", result: hello1)])
        let credentials = TrackingCredentialProvider()
        let client = ModelDeckEngineClient(transport: transport, credentialProvider: credentials)
        XCTAssertThrowsError(try client.connectAndAuthenticate(descriptor: standardDescriptor())) { error in
            guard case EngineClientError.negotiationFailed = error else {
                return XCTFail("expected negotiationFailed, got \(error)")
            }
        }
        XCTAssertEqual(credentials.readCount, 0)
    }

    func testTwoStepHelloAndModelsList() throws {
        let hello1 = try ContractJSON.loadFixture(named: "hello_result_unauthenticated.json")
        let listResult: [String: Any] = [
            "collection": "catalog",
            "items": [[
                "kind": "catalog",
                "provider_model_id": "openrouter/foo/bar",
                "connection_id": connectionID,
                "display_name": "Foo",
            ]],
            "cache_only": true,
        ]
        let transport = FakeEngineTransport(responses: [
            wrap(id: "md-1", result: try jsonObject(hello1)),
            wrap(id: "md-2", result: authenticatedHello2()),
            wrap(id: "md-3", result: listResult),
        ])
        let client = ModelDeckEngineClient(
            transport: transport,
            credentialProvider: FixedCredentialProvider(value: "operator-secret")
        )
        try client.connectAndAuthenticate(descriptor: standardDescriptor())
        let list = try client.listCatalogModelsResult(connectionID: connectionID)
        XCTAssertEqual(list.items.count, 1)
        XCTAssertEqual(list.items[0].providerModelID, "openrouter/foo/bar")
        XCTAssertTrue(list.cacheOnly)
        let sent = transport.recordedFrames()
        XCTAssertEqual(sent.count, 3)
    }

    func testModelsListRejectsMissingCacheOnly() throws {
        let hello1 = try ContractJSON.loadFixture(named: "hello_result_unauthenticated.json")
        let transport = FakeEngineTransport(responses: [
            wrap(id: "md-1", result: try jsonObject(hello1)),
            wrap(id: "md-2", result: authenticatedHello2()),
            wrap(id: "md-3", result: [
                "collection": "catalog",
                "items": [],
            ]),
        ])
        let client = ModelDeckEngineClient(
            transport: transport,
            credentialProvider: FixedCredentialProvider(value: "x")
        )
        try client.connectAndAuthenticate(descriptor: standardDescriptor())
        XCTAssertThrowsError(try client.listCatalogModels(connectionID: connectionID))
    }

    func testModelsListRejectsWrongCollection() throws {
        let hello1 = try ContractJSON.loadFixture(named: "hello_result_unauthenticated.json")
        let transport = FakeEngineTransport(responses: [
            wrap(id: "md-1", result: try jsonObject(hello1)),
            wrap(id: "md-2", result: authenticatedHello2()),
            wrap(id: "md-3", result: [
                "collection": "registered",
                "items": [],
                "cache_only": true,
            ]),
        ])
        let client = ModelDeckEngineClient(
            transport: transport,
            credentialProvider: FixedCredentialProvider(value: "x")
        )
        try client.connectAndAuthenticate(descriptor: standardDescriptor())
        XCTAssertThrowsError(try client.listCatalogModels(connectionID: connectionID))
    }

    func testModelsListRejectsCacheOnlyFalse() throws {
        let hello1 = try ContractJSON.loadFixture(named: "hello_result_unauthenticated.json")
        let transport = FakeEngineTransport(responses: [
            wrap(id: "md-1", result: try jsonObject(hello1)),
            wrap(id: "md-2", result: authenticatedHello2()),
            wrap(id: "md-3", result: [
                "collection": "catalog",
                "items": [],
                "cache_only": false,
            ]),
        ])
        let client = ModelDeckEngineClient(
            transport: transport,
            credentialProvider: FixedCredentialProvider(value: "x")
        )
        try client.connectAndAuthenticate(descriptor: standardDescriptor())
        XCTAssertThrowsError(try client.listCatalogModels(connectionID: connectionID))
    }

    private func standardDescriptor(
        engineInstanceID: String = "550e8400-e29b-41d4-a716-446655440000"
    ) -> EngineRendezvousDescriptor {
        EngineRendezvousDescriptor(
            transport: "unix",
            socketPath: "/tmp/model-deck-engine.sock",
            engineInstanceID: engineInstanceID,
            instanceNonce: "rendezvous-nonce-7f3a",
            apiProfile: apiProfile
        )
    }

    private func authenticatedHello2() -> [String: Any] {
        [
            "authenticated": true,
            "api_profile": ["major": 1, "minor": 0],
            "engine_instance_id": "550e8400-e29b-41d4-a716-446655440000",
            "instance_nonce": "rendezvous-nonce-7f3a",
            "capabilities": ["features": ["tools": "supported"]],
        ]
    }

    private func jsonObject(_ data: Data) throws -> Any {
        try JSONSerialization.jsonObject(with: data)
    }

    private func wrap(id: String, result: Any) -> Data {
        let object: [String: Any] = ["jsonrpc": "2.0", "id": id, "result": result]
        guard let data = try? JSONSerialization.data(withJSONObject: object) else {
            XCTFail("failed to encode json rpc response")
            return Data()
        }
        return data
    }
}

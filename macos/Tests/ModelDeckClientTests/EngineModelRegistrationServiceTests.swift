import XCTest
import ModelDeckContracts
@testable import ModelDeckClient

private final class ModelCredentialProvider: EngineInstanceCredentialProviding {
    func readCredential() throws -> String { "operator-secret" }
}

final class EngineModelRegistrationServiceTests: XCTestCase {
    private let connectionID = "11111111-1111-4111-8111-111111111111"
    private let registrationID = "33333333-3333-4333-8333-333333333333"

    func testListRegisteredSendsFrozenRPCAndDecodesTypedItems() async throws {
        let transport = FakeEngineTransport(responses: [
            frame(id: "md-1", result: hello(authenticated: false)),
            frame(id: "md-2", result: hello(authenticated: true)),
            frame(id: "md-3", result: [
                "collection": "registered",
                "items": [
                    [
                        "kind": "registered",
                        "registration_id": registrationID,
                        "provider_model_id": "gpt-5",
                        "connection_id": connectionID,
                        "display_name": "GPT-5",
                        "revision": 2,
                    ],
                    [
                        "kind": "registered",
                        "registration_id": "44444444-4444-4444-8444-444444444444",
                        "provider_model_id": "claude-opus-4.5",
                        "connection_id": connectionID,
                        "display_name": "Claude Opus",
                        "revision": 1,
                    ],
                ],
            ]),
        ])
        let service = EngineModelRegistrationService(
            rendezvous: descriptor(),
            transport: transport,
            credentialProvider: ModelCredentialProvider()
        )
        try await service.connect()

        let page = try await service.listRegisteredModels()

        XCTAssertEqual(page.collection, "registered")
        XCTAssertEqual(page.items.count, 2)
        let first = page.items[0]
        XCTAssertEqual(first.registrationID, registrationID)
        XCTAssertEqual(first.providerModelID, "gpt-5")
        XCTAssertEqual(first.connectionID, connectionID)
        XCTAssertEqual(first.displayName, "GPT-5")
        XCTAssertEqual(first.revision, 2)
        let second = page.items[1]
        XCTAssertEqual(second.providerModelID, "claude-opus-4.5")

        let requests = transport.recordedFrames().map(requestObject)
        XCTAssertEqual(requests.map { $0["method"] as? String }, [
            "engine.v1.hello", "engine.v1.hello", "engine.v1.models.list",
        ])
        let listParams = try XCTUnwrap(requests[2]["params"] as? [String: Any])
        XCTAssertEqual(listParams["collection"] as? String, "registered",
                       "default collection must be explicit registered, never catalog")
    }

    func testRegisterModelPassesExpectedRevisionAndIdempotencyKey() async throws {
        let transport = FakeEngineTransport(responses: [
            frame(id: "md-1", result: hello(authenticated: false)),
            frame(id: "md-2", result: hello(authenticated: true)),
            frame(id: "md-3", result: [
                "model": [
                    "registration_id": registrationID,
                    "provider_model_id": "gpt-5",
                    "connection_id": connectionID,
                    "display_name": "GPT-5 Custom",
                    "revision": 1,
                ],
            ]),
        ])
        let service = EngineModelRegistrationService(
            rendezvous: descriptor(),
            transport: transport,
            credentialProvider: ModelCredentialProvider()
        )
        try await service.connect()

        let model = try await service.registerModel(
            connectionID: connectionID,
            providerModelID: "gpt-5",
            displayName: "GPT-5 Custom",
            expectedRevision: 0,
            idempotencyKey: "11111111-1111-4111-8111-bbbbbbbbbbbb"
        )

        XCTAssertEqual(model.registrationID, registrationID)
        XCTAssertEqual(model.providerModelID, "gpt-5")
        XCTAssertEqual(model.connectionID, connectionID)
        XCTAssertEqual(model.displayName, "GPT-5 Custom")
        XCTAssertEqual(model.revision, 1)

        let requests = transport.recordedFrames().map(requestObject)
        let params = try XCTUnwrap(requests[2]["params"] as? [String: Any])
        XCTAssertEqual(params["connection_id"] as? String, connectionID)
        XCTAssertEqual(params["provider_model_id"] as? String, "gpt-5")
        XCTAssertEqual(params["display_name"] as? String, "GPT-5 Custom")
        XCTAssertEqual(params["expected_revision"] as? Int, 0)
        XCTAssertEqual(params["idempotency_key"] as? String, "11111111-1111-4111-8111-bbbbbbbbbbbb")
    }

    func testRenameModelPassesRegistrationIDAndRevision() async throws {
        let transport = FakeEngineTransport(responses: [
            frame(id: "md-1", result: hello(authenticated: false)),
            frame(id: "md-2", result: hello(authenticated: true)),
            frame(id: "md-3", result: [
                "model": [
                    "registration_id": registrationID,
                    "provider_model_id": "gpt-5",
                    "connection_id": connectionID,
                    "display_name": "GPT-5 Renamed",
                    "revision": 2,
                ],
            ]),
        ])
        let service = EngineModelRegistrationService(
            rendezvous: descriptor(),
            transport: transport,
            credentialProvider: ModelCredentialProvider()
        )
        try await service.connect()

        let model = try await service.renameModel(
            registrationID: registrationID,
            displayName: "GPT-5 Renamed",
            expectedRevision: 1,
            idempotencyKey: "idem-rename-1"
        )

        XCTAssertEqual(model.displayName, "GPT-5 Renamed")
        XCTAssertEqual(model.revision, 2)
        XCTAssertEqual(model.providerModelID, "gpt-5",
                       "rename must not change provider_model_id")

        let requests = transport.recordedFrames().map(requestObject)
        let params = try XCTUnwrap(requests[2]["params"] as? [String: Any])
        XCTAssertEqual(params["registration_id"] as? String, registrationID)
        XCTAssertEqual(params["display_name"] as? String, "GPT-5 Renamed")
        XCTAssertEqual(params["expected_revision"] as? Int, 1)
        XCTAssertEqual(params["idempotency_key"] as? String, "idem-rename-1")
        XCTAssertNil(params["provider_model_id"], "rename must not carry provider_model_id")
        XCTAssertNil(params["connection_id"], "rename must not carry connection_id")
    }

    func testRemoveModelPassesRegistrationIDAndRevision() async throws {
        let transport = FakeEngineTransport(responses: [
            frame(id: "md-1", result: hello(authenticated: false)),
            frame(id: "md-2", result: hello(authenticated: true)),
            frame(id: "md-3", result: ["removed": true]),
        ])
        let service = EngineModelRegistrationService(
            rendezvous: descriptor(),
            transport: transport,
            credentialProvider: ModelCredentialProvider()
        )
        try await service.connect()

        let removed = try await service.removeModel(
            registrationID: registrationID,
            expectedRevision: 1,
            idempotencyKey: "idem-remove-1"
        )

        XCTAssertTrue(removed)

        let requests = transport.recordedFrames().map(requestObject)
        let params = try XCTUnwrap(requests[2]["params"] as? [String: Any])
        XCTAssertEqual(params["registration_id"] as? String, registrationID)
        XCTAssertEqual(params["expected_revision"] as? Int, 1)
        XCTAssertEqual(params["idempotency_key"] as? String, "idem-remove-1")
    }

    func testNewIdempotencyKeyHelperReturnsUUIDLowercase() {
        let key = EngineModelRegistrationService.newIdempotencyKey()
        XCTAssertNotNil(UUID(uuidString: key))
    }

    private func descriptor() -> EngineRendezvousDescriptor {
        EngineRendezvousDescriptor(
            transport: "unix", socketPath: "/tmp/model-deck-engine.sock",
            engineInstanceID: "550e8400-e29b-41d4-a716-446655440000",
            instanceNonce: "rendezvous-nonce-7f3a", apiProfile: EngineAPIProfile(major: 1, minor: 0)
        )
    }

    private func hello(authenticated: Bool) -> [String: Any] {
        [
            "authenticated": authenticated,
            "api_profile": ["major": 1, "minor": 0],
            "engine_instance_id": "550e8400-e29b-41d4-a716-446655440000",
            "instance_nonce": "rendezvous-nonce-7f3a",
            "capabilities": ["features": ["tools": "supported"]],
        ]
    }

    private func frame(id: String, result: Any) -> Data {
        try! JSONSerialization.data(withJSONObject: ["jsonrpc": "2.0", "id": id, "result": result])
    }

    private func requestObject(_ framed: Data) -> [String: Any] {
        var data = framed
        if data.last == 0x0A { data = data.dropLast() }
        return (try? JSONSerialization.jsonObject(with: Data(data))) as? [String: Any] ?? [:]
    }
}

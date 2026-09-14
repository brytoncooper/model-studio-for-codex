import XCTest
import ModelDeckContracts
@testable import ModelDeckClient

private final class ConnectionCredentialProvider: EngineInstanceCredentialProviding {
    func readCredential() throws -> String { "operator-secret" }
}

final class EngineConnectionServiceTests: XCTestCase {
    private let connectionID = "11111111-1111-4111-8111-111111111111"

    func testListConnectionsSendsFrozenRPCAndDecodesTypedRefs() async throws {
        let transport = FakeEngineTransport(responses: [
            frame(id: "md-1", result: hello(authenticated: false)),
            frame(id: "md-2", result: hello(authenticated: true)),
            frame(id: "md-3", result: [
                "connections": [
                    [
                        "connection_id": "11111111-1111-4111-8111-111111111111",
                        "provider_id": "codex.openai",
                        "endpoint_config_ref": "ref:v2.endpoint.sha20-aaaaaaaaaaaaaaaaaaaa",
                        "credential_ref": "ref:v2.credential.sha20-bbbbbbbbbbbbbbbbbbbb",
                        "revision": 4,
                    ],
                    [
                        "connection_id": "22222222-2222-4222-8222-222222222222",
                        "provider_id": "openrouter.api",
                        "revision": 0,
                    ],
                ],
            ]),
        ])
        let service = EngineConnectionService(
            rendezvous: descriptor(),
            transport: transport,
            credentialProvider: ConnectionCredentialProvider()
        )
        try await service.connect()

        let refs = try await service.listConnections()

        XCTAssertEqual(refs.count, 2)
        XCTAssertEqual(refs[0].connectionID, "11111111-1111-4111-8111-111111111111")
        XCTAssertEqual(refs[0].providerID, "codex.openai")
        XCTAssertEqual(refs[0].revision, 4)
        XCTAssertEqual(refs[0].endpointConfigRef, "ref:v2.endpoint.sha20-aaaaaaaaaaaaaaaaaaaa")
        XCTAssertEqual(refs[0].credentialRef, "ref:v2.credential.sha20-bbbbbbbbbbbbbbbbbbbb")
        XCTAssertEqual(refs[1].connectionID, "22222222-2222-4222-8222-222222222222")
        XCTAssertEqual(refs[1].providerID, "openrouter.api")
        XCTAssertEqual(refs[1].revision, 0)
        XCTAssertNil(refs[1].endpointConfigRef)
        XCTAssertNil(refs[1].credentialRef)

        let requests = transport.recordedFrames().map(requestObject)
        XCTAssertEqual(requests.map { $0["method"] as? String }, [
            "engine.v1.hello", "engine.v1.hello", "engine.v1.connections.list",
        ])
        let listParams = try XCTUnwrap(requests[2]["params"] as? [String: Any])
        XCTAssertTrue(listParams.isEmpty, "connections.list must send empty params object")
    }

    func testSaveConnectionPassesExpectedRevisionAndIdempotencyKey() async throws {
        let transport = FakeEngineTransport(responses: [
            frame(id: "md-1", result: hello(authenticated: false)),
            frame(id: "md-2", result: hello(authenticated: true)),
            frame(id: "md-3", result: [
                "connection": [
                    "connection_id": "11111111-1111-4111-8111-111111111111",
                    "provider_id": "codex.openai",
                    "revision": 5,
                ],
            ]),
        ])
        let service = EngineConnectionService(
            rendezvous: descriptor(),
            transport: transport,
            credentialProvider: ConnectionCredentialProvider()
        )
        try await service.connect()

        let saved = try await service.saveConnection(
            input: ConnectionSaveInput(
                connectionID: connectionID,
                providerID: "codex.openai",
                endpointConfigRef: "ref:v2.endpoint.sha20-cccccccccccccccccccc",
                credentialRef: "ref:v2.credential.sha20-dddddddddddddddddddd"
            ),
            expectedRevision: 4,
            idempotencyKey: "11111111-1111-4111-8111-aaaaaaaaaaaa"
        )

        XCTAssertEqual(saved.connectionID, connectionID)
        XCTAssertEqual(saved.providerID, "codex.openai")
        XCTAssertEqual(saved.revision, 5)

        let requests = transport.recordedFrames().map(requestObject)
        XCTAssertEqual(requests[2]["method"] as? String, "engine.v1.connections.save")
        let params = try XCTUnwrap(requests[2]["params"] as? [String: Any])
        XCTAssertEqual(params["expected_revision"] as? Int, 4)
        XCTAssertEqual(params["idempotency_key"] as? String, "11111111-1111-4111-8111-aaaaaaaaaaaa")
        let inner = try XCTUnwrap(params["connection"] as? [String: Any])
        XCTAssertEqual(inner["connection_id"] as? String, connectionID)
        XCTAssertEqual(inner["provider_id"] as? String, "codex.openai")
        XCTAssertEqual(inner["endpoint_config_ref"] as? String, "ref:v2.endpoint.sha20-cccccccccccccccccccc")
        XCTAssertEqual(inner["credential_ref"] as? String, "ref:v2.credential.sha20-dddddddddddddddddddd")
        XCTAssertNil(inner["revision"], "CAS revision must not appear inside connection payload")
    }

    func testSaveConnectionMinimalPayloadOmitsOptionalRefs() async throws {
        let transport = FakeEngineTransport(responses: [
            frame(id: "md-1", result: hello(authenticated: false)),
            frame(id: "md-2", result: hello(authenticated: true)),
            frame(id: "md-3", result: [
                "connection": [
                    "connection_id": connectionID,
                    "provider_id": "cursor.com",
                    "revision": 0,
                ],
            ]),
        ])
        let service = EngineConnectionService(
            rendezvous: descriptor(),
            transport: transport,
            credentialProvider: ConnectionCredentialProvider()
        )
        try await service.connect()

        _ = try await service.saveConnection(
            input: ConnectionSaveInput(
                connectionID: connectionID,
                providerID: "cursor.com",
                endpointConfigRef: nil,
                credentialRef: nil
            ),
            expectedRevision: 0,
            idempotencyKey: "idem-create-1"
        )

        let requests = transport.recordedFrames().map(requestObject)
        let params = try XCTUnwrap(requests[2]["params"] as? [String: Any])
        let inner = try XCTUnwrap(params["connection"] as? [String: Any])
        XCTAssertNil(inner["endpoint_config_ref"])
        XCTAssertNil(inner["credential_ref"])
        XCTAssertEqual(params["expected_revision"] as? Int, 0)
        XCTAssertEqual(params["idempotency_key"] as? String, "idem-create-1")
    }

    func testNewIdempotencyKeyHelperReturnsUUIDLowercase() {
        let key = EngineConnectionService.newIdempotencyKey()
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

import XCTest
import ModelDeckContracts
@testable import ModelDeckClient

private final class HostProjectionCredentialProvider: EngineInstanceCredentialProviding {
    func readCredential() throws -> String { "operator-secret" }
}

final class EngineHostProjectionStatusServiceTests: XCTestCase {
    private let hostID = "codex.openai"

    func testStatusSendsFrozenRPCAndDecodesReady() async throws {
        let transport = FakeEngineTransport(responses: [
            frame(id: "md-1", result: hello(authenticated: false)),
            frame(id: "md-2", result: hello(authenticated: true)),
            frame(id: "md-3", result: ["status": "ready"]),
        ])
        let service = EngineHostProjectionStatusService(
            rendezvous: descriptor(),
            transport: transport,
            credentialProvider: HostProjectionCredentialProvider()
        )
        try await service.connect()

        let status = try await service.status(hostID: hostID)

        XCTAssertEqual(status, .ready)
        XCTAssertEqual(status.rawValue, "ready")

        let requests = transport.recordedFrames().map(requestObject)
        XCTAssertEqual(requests.map { $0["method"] as? String }, [
            "engine.v1.hello",
            "engine.v1.hello",
            "engine.v1.hosts.projection_status",
        ])
        let params = try XCTUnwrap(requests[2]["params"] as? [String: Any])
        XCTAssertEqual(params["host_id"] as? String, hostID)
        XCTAssertNil(params["status"], "status must only appear in the result, never the params")
        XCTAssertEqual(params.count, 1, "params must contain only host_id")
    }

    func testStatusDecodesPendingAndFailed() async throws {
        for expected in [HostProjectionStatus.pending, .failed] {
            let transport = FakeEngineTransport(responses: [
                frame(id: "md-1", result: hello(authenticated: false)),
                frame(id: "md-2", result: hello(authenticated: true)),
                frame(id: "md-3", result: ["status": expected.rawValue]),
            ])
            let service = EngineHostProjectionStatusService(
                rendezvous: descriptor(),
                transport: transport,
                credentialProvider: HostProjectionCredentialProvider()
            )
            try await service.connect()

            let status = try await service.status(hostID: hostID)
            XCTAssertEqual(status, expected)
            XCTAssertEqual(status.rawValue, expected.rawValue)
        }
    }

    func testStatusPropagatesDifferentHostID() async throws {
        let transport = FakeEngineTransport(responses: [
            frame(id: "md-1", result: hello(authenticated: false)),
            frame(id: "md-2", result: hello(authenticated: true)),
            frame(id: "md-3", result: ["status": "pending"]),
        ])
        let service = EngineHostProjectionStatusService(
            rendezvous: descriptor(),
            transport: transport,
            credentialProvider: HostProjectionCredentialProvider()
        )
        try await service.connect()

        _ = try await service.status(hostID: "com.modeldeck.host.cursor")

        let requests = transport.recordedFrames().map(requestObject)
        let params = try XCTUnwrap(requests[2]["params"] as? [String: Any])
        XCTAssertEqual(params["host_id"] as? String, "com.modeldeck.host.cursor")
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

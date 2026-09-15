import XCTest
import ModelDeckContracts
@testable import ModelDeckClient

private final class HostServiceCredentialProvider: EngineInstanceCredentialProviding {
    func readCredential() throws -> String { "operator-secret" }
}

final class EngineHostServiceTests: XCTestCase {
    private let hostID = "codex.openai"

    func testListHostsSendsFrozenRPCWithEmptyParamsAndDecodesTypedHosts() async throws {
        let transport = FakeEngineTransport(responses: [
            frame(id: "md-1", result: hello(authenticated: false)),
            frame(id: "md-2", result: hello(authenticated: true)),
            frame(id: "md-3", result: [
                "hosts": [
                    ["host_id": "codex.openai", "api_profile": "codex.app-server.v1"],
                    ["host_id": "cursor.com", "api_profile": "cursor.cloud.v1"],
                ],
            ]),
        ])
        let service = EngineHostService(
            rendezvous: descriptor(),
            transport: transport,
            credentialProvider: HostServiceCredentialProvider()
        )
        try await service.connect()

        let hosts = try await service.listHosts()

        XCTAssertEqual(hosts.count, 2)
        XCTAssertEqual(hosts[0].hostID, "codex.openai")
        XCTAssertEqual(hosts[0].apiProfile, "codex.app-server.v1")
        XCTAssertEqual(hosts[1].hostID, "cursor.com")
        XCTAssertEqual(hosts[1].apiProfile, "cursor.cloud.v1")

        let requests = transport.recordedFrames().map(requestObject)
        XCTAssertEqual(requests.map { $0["method"] as? String }, [
            "engine.v1.hello",
            "engine.v1.hello",
            "engine.v1.hosts.list",
        ])
        let params = try XCTUnwrap(requests[2]["params"] as? [String: Any])
        XCTAssertTrue(params.isEmpty, "hosts.list must send empty params object")
    }

    func testListHostsDecodesEmptyList() async throws {
        let transport = FakeEngineTransport(responses: [
            frame(id: "md-1", result: hello(authenticated: false)),
            frame(id: "md-2", result: hello(authenticated: true)),
            frame(id: "md-3", result: ["hosts": []]),
        ])
        let service = EngineHostService(
            rendezvous: descriptor(),
            transport: transport,
            credentialProvider: HostServiceCredentialProvider()
        )
        try await service.connect()

        let hosts = try await service.listHosts()
        XCTAssertTrue(hosts.isEmpty)
    }

    func testPrepareHostSendsFrozenRPCWithHostIDAndDecodesPrepared() async throws {
        let transport = FakeEngineTransport(responses: [
            frame(id: "md-1", result: hello(authenticated: false)),
            frame(id: "md-2", result: hello(authenticated: true)),
            frame(id: "md-3", result: ["prepared": true]),
        ])
        let service = EngineHostService(
            rendezvous: descriptor(),
            transport: transport,
            credentialProvider: HostServiceCredentialProvider()
        )
        try await service.connect()

        let prepared = try await service.prepareHost(hostID: hostID)

        XCTAssertTrue(prepared)

        let requests = transport.recordedFrames().map(requestObject)
        XCTAssertEqual(requests.map { $0["method"] as? String }, [
            "engine.v1.hello",
            "engine.v1.hello",
            "engine.v1.hosts.prepare",
        ])
        let params = try XCTUnwrap(requests[2]["params"] as? [String: Any])
        XCTAssertEqual(params["host_id"] as? String, hostID)
        XCTAssertNil(params["prepared"], "prepared must only appear in the result, never the params")
        XCTAssertEqual(params.count, 1, "params must contain only host_id")
    }

    func testPrepareHostPropagatesDifferentHostID() async throws {
        let transport = FakeEngineTransport(responses: [
            frame(id: "md-1", result: hello(authenticated: false)),
            frame(id: "md-2", result: hello(authenticated: true)),
            frame(id: "md-3", result: ["prepared": false]),
        ])
        let service = EngineHostService(
            rendezvous: descriptor(),
            transport: transport,
            credentialProvider: HostServiceCredentialProvider()
        )
        try await service.connect()

        let prepared = try await service.prepareHost(hostID: "cursor.com")
        XCTAssertFalse(prepared)

        let requests = transport.recordedFrames().map(requestObject)
        let params = try XCTUnwrap(requests[2]["params"] as? [String: Any])
        XCTAssertEqual(params["host_id"] as? String, "cursor.com")
    }

    func testPrepareHostDecodesFalse() async throws {
        let transport = FakeEngineTransport(responses: [
            frame(id: "md-1", result: hello(authenticated: false)),
            frame(id: "md-2", result: hello(authenticated: true)),
            frame(id: "md-3", result: ["prepared": false]),
        ])
        let service = EngineHostService(
            rendezvous: descriptor(),
            transport: transport,
            credentialProvider: HostServiceCredentialProvider()
        )
        try await service.connect()

        let prepared = try await service.prepareHost(hostID: hostID)
        XCTAssertFalse(prepared)
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

import XCTest
import ModelDeckContracts
@testable import ModelDeckClient

private struct FixedPanelCredential: EngineInstanceCredentialProviding {
    func readCredential() throws -> String { "operator-secret" }
}

final class EngineExtensionPanelServiceTests: XCTestCase {
    func testDiscoversFetchesListsAndInvokesUsingFrozenRPCs() throws {
        let transport = FakeEngineTransport(responses: [
            response(id: "md-1", result: hello(authenticated: false)),
            response(id: "md-2", result: hello(authenticated: true)),
            response(id: "md-3", result: ["panels": [["panel_id": "org.example.panel", "title": "Example"]]]),
            response(id: "md-4", result: ["panel": readyPanel()]),
            response(id: "md-5", result: ["operations": [[
                "operation_id": "org.example.save",
                "input_schema_id": "schema:save-input",
                "output_schema_id": "schema:save-output",
                "effect": "write",
                "required_grants": ["storage.write"],
            ]]]),
            response(id: "md-6", result: ["output": ["saved": true]]),
            response(id: "md-7", result: ["output": ["saved": true]]),
        ])
        let service = EngineExtensionPanelService(
            rendezvous: descriptor(),
            transport: transport,
            credentialProvider: FixedPanelCredential()
        )

        try service.connect()
        XCTAssertEqual(try service.listPanels(), [ExtensionPanelContribution(panelID: "org.example.panel", title: "Example")])
        XCTAssertEqual(try service.fetchPanel(panelID: "org.example.panel"), .object(readyPanelJSONValue()))
        XCTAssertEqual(try service.listOperations().first?.operationID, "org.example.save")
        XCTAssertEqual(
            try service.invokeOperation(operationID: "org.example.save", input: .object(["title": .string("Changed")])).output,
            .object(["saved": .bool(true)])
        )
        _ = try service.invokeOperation(operationID: "org.example.save", input: .object([:]))

        let requests = try transport.recordedFrames().map(requestObject)
        XCTAssertEqual(requests.map { $0["method"] as? String }, [
            "engine.v1.hello", "engine.v1.hello",
            "engine.v1.ui.contributions.list", "engine.v1.ui.panel.get",
            "engine.v1.operations.list", "engine.v1.operations.invoke", "engine.v1.operations.invoke",
        ])
        let invokeParams = try XCTUnwrap(requests[5]["params"] as? [String: Any])
        let secondInvokeParams = try XCTUnwrap(requests[6]["params"] as? [String: Any])
        XCTAssertEqual(invokeParams["operation"] as? String, "org.example.save")
        XCTAssertEqual((invokeParams["input"] as? [String: Any])?["title"] as? String, "Changed")
        let firstKey = try XCTUnwrap(invokeParams["idempotency_key"] as? String)
        let secondKey = try XCTUnwrap(secondInvokeParams["idempotency_key"] as? String)
        XCTAssertNotNil(UUID(uuidString: firstKey))
        XCTAssertNotEqual(firstKey, secondKey)
    }

    private func descriptor() -> EngineRendezvousDescriptor {
        EngineRendezvousDescriptor(
            transport: "unix", socketPath: "/tmp/isolated-engine.sock",
            engineInstanceID: "550e8400-e29b-41d4-a716-446655440000",
            instanceNonce: "isolated-nonce", apiProfile: EngineAPIProfile(major: 1, minor: 0)
        )
    }

    private func hello(authenticated: Bool) -> [String: Any] {
        [
            "authenticated": authenticated,
            "api_profile": ["major": 1, "minor": 0],
            "engine_instance_id": "550e8400-e29b-41d4-a716-446655440000",
            "instance_nonce": "isolated-nonce",
            "capabilities": ["features": ["tools": "supported"]],
        ]
    }

    private func readyPanel() -> [String: Any] {
        ["panel_id": "org.example.panel", "revision": 1, "state": "ready", "title": "Example", "root": ["kind": "text", "id": "title", "value": "Hello"]]
    }

    private func readyPanelJSONValue() -> [String: JSONValue] {
        [
            "panel_id": .string("org.example.panel"), "revision": .number(1),
            "state": .string("ready"), "title": .string("Example"),
            "root": .object(["kind": .string("text"), "id": .string("title"), "value": .string("Hello")]),
        ]
    }

    private func response(id: String, result: Any) -> Data {
        try! JSONSerialization.data(withJSONObject: ["jsonrpc": "2.0", "id": id, "result": result])
    }

    private func requestObject(_ framed: Data) throws -> [String: Any] {
        let payload = framed.last == 0x0A ? framed.dropLast() : framed[framed.startIndex..<framed.endIndex]
        return try XCTUnwrap(JSONSerialization.jsonObject(with: Data(payload)) as? [String: Any])
    }
}

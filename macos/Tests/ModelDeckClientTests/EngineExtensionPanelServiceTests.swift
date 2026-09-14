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
            response(id: "md-6", result: [
                "output": ["saved": true],
                "job_id": "11111111-1111-4111-8111-111111111111",
                "panel": readyPanel(),
            ]),
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
        let result = try service.invokeOperation(
            operationID: "org.example.save",
            input: .object(["title": .string("Changed")])
        )
        XCTAssertEqual(
            result.output,
            .object(["saved": .bool(true)])
        )
        XCTAssertEqual(result.panel, .object(readyPanelJSONValue()))
        XCTAssertEqual(result.jobID, "11111111-1111-4111-8111-111111111111")
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

    func testInstallsAndChangesExtensionStateThroughFrozenRPCs() throws {
        let transport = FakeEngineTransport(responses: [
            response(id: "md-1", result: hello(authenticated: false)),
            response(id: "md-2", result: hello(authenticated: true)),
            response(id: "md-3", result: ["extensions": [[
                "extension_id": "org.example.notebook",
                "status": "installed",
            ]]]),
            response(id: "md-4", result: [
                "extension_id": "org.example.notebook",
                "status": "installed",
                "version": "1.0.0",
                "revision": 1,
            ]),
            response(id: "md-5", result: [
                "extension_id": "org.example.notebook",
                "version": "1.0.0",
            ]),
            response(id: "md-6", result: ["extension_id": "org.example.notebook", "version": "2.0.0"]),
            response(id: "md-7", result: ["enabled": true]),
            response(id: "md-8", result: ["enabled": false]),
        ])
        let service = EngineExtensionPanelService(
            rendezvous: descriptor(),
            transport: transport,
            credentialProvider: FixedPanelCredential()
        )

        try service.connect()
        XCTAssertEqual(try service.listInstalledExtensions().first?.extensionID, "org.example.notebook")
        let detail = try service.extensionDetail(extensionID: "org.example.notebook")
        XCTAssertEqual(detail.revision, 1)
        XCTAssertEqual(try service.installExtension(archivePath: "/tmp/notebook.zip"), "org.example.notebook")
        XCTAssertEqual(try service.updateExtension(extensionID: "org.example.notebook", archivePath: "/tmp/notebook-2.zip", expectedRevision: 1), "2.0.0")
        try service.setExtensionEnabled(extensionID: "org.example.notebook", revision: 1, enabled: true)
        try service.setExtensionEnabled(extensionID: "org.example.notebook", revision: 2, enabled: false)

        let requests = try transport.recordedFrames().map(requestObject)
        XCTAssertEqual(requests.map { $0["method"] as? String }, [
            "engine.v1.hello", "engine.v1.hello",
            "engine.v1.extensions.list", "engine.v1.extensions.get",
            "engine.v1.extensions.install", "engine.v1.extensions.update",
            "engine.v1.extensions.enable",
            "engine.v1.extensions.disable",
        ])
        let installParams = try XCTUnwrap(requests[4]["params"] as? [String: Any])
        XCTAssertEqual(installParams["archive_path"] as? String, "/tmp/notebook.zip")
        XCTAssertEqual(installParams["expected_revision"] as? Int, 0)
        let updateParams = try XCTUnwrap(requests[5]["params"] as? [String: Any])
        XCTAssertEqual(updateParams["extension_id"] as? String, "org.example.notebook")
        XCTAssertEqual(updateParams["archive_path"] as? String, "/tmp/notebook-2.zip")
        XCTAssertEqual(updateParams["expected_revision"] as? Int, 1)
        XCTAssertNotNil(UUID(uuidString: updateParams["idempotency_key"] as? String ?? ""))
        let enableParams = try XCTUnwrap(requests[6]["params"] as? [String: Any])
        let disableParams = try XCTUnwrap(requests[7]["params"] as? [String: Any])
        XCTAssertEqual(enableParams["expected_revision"] as? Int, 1)
        XCTAssertEqual(disableParams["expected_revision"] as? Int, 2)
    }


    func testGetJobReturnsTypedSnapshotViaFrozenRPCs() throws {
        let transport = FakeEngineTransport(responses: [
            response(id: "md-1", result: hello(authenticated: false)),
            response(id: "md-2", result: hello(authenticated: true)),
            response(id: "md-3", result: [
                "job_id": "11111111-1111-4111-8111-111111111111",
                "state": "running",
                "progress": 0.42,
                "output": NSNull(),
            ]),
            response(id: "md-4", result: [
                "job_id": "11111111-1111-4111-8111-111111111111",
                "state": "completed",
                "output": [
                    "media_type": "text/markdown",
                    "suggested_filename": "session-notebook.md",
                    "content": "# hi",
                ],
            ]),
        ])
        let service = EngineExtensionPanelService(
            rendezvous: descriptor(),
            transport: transport,
            credentialProvider: FixedPanelCredential()
        )
        try service.connect()
        let running = try service.getJob(jobID: "11111111-1111-4111-8111-111111111111")
        XCTAssertEqual(running.state, .running)
        XCTAssertEqual(running.progress, 0.42)
        XCTAssertEqual(running.output, .null)
        XCTAssertTrue(running.outputPresent)

        let completed = try service.getJob(jobID: "11111111-1111-4111-8111-111111111111")
        XCTAssertEqual(completed.state, .completed)
        XCTAssertEqual(completed.progress, nil)
        XCTAssertEqual(
            completed.output,
            .object([
                "media_type": .string("text/markdown"),
                "suggested_filename": .string("session-notebook.md"),
                "content": .string("# hi"),
            ])
        )
        XCTAssertTrue(completed.outputPresent)

        let requests = try transport.recordedFrames().map(requestObject)
        XCTAssertEqual(requests.map { $0["method"] as? String }, [
            "engine.v1.hello", "engine.v1.hello",
            "engine.v1.jobs.get", "engine.v1.jobs.get",
        ])
        let firstGet = try XCTUnwrap(requests[2]["params"] as? [String: Any])
        XCTAssertEqual(firstGet["job_id"] as? String, "11111111-1111-4111-8111-111111111111")
    }

    func testGetJobDistinguishesAbsentOutputFromExplicitNull() throws {
        let transport = FakeEngineTransport(responses: [
            response(id: "md-1", result: hello(authenticated: false)),
            response(id: "md-2", result: hello(authenticated: true)),
            response(id: "md-3", result: [
                "job_id": "11111111-1111-4111-8111-111111111111",
                "state": "running",
                "progress": 0.1,
            ]),
        ])
        let service = EngineExtensionPanelService(
            rendezvous: descriptor(),
            transport: transport,
            credentialProvider: FixedPanelCredential()
        )
        try service.connect()
        let snapshot = try service.getJob(jobID: "11111111-1111-4111-8111-111111111111")
        XCTAssertEqual(snapshot.state, .running)
        XCTAssertNil(snapshot.output)
        XCTAssertFalse(snapshot.outputPresent, "absent output must not be confused with explicit JSON null")
    }

    func testCancelJobReturnsAcceptedViaFrozenRPCs() throws {
        let transport = FakeEngineTransport(responses: [
            response(id: "md-1", result: hello(authenticated: false)),
            response(id: "md-2", result: hello(authenticated: true)),
            response(id: "md-3", result: ["accepted": true]),
            response(id: "md-4", result: ["accepted": false]),
        ])
        let service = EngineExtensionPanelService(
            rendezvous: descriptor(),
            transport: transport,
            credentialProvider: FixedPanelCredential()
        )
        try service.connect()
        XCTAssertTrue(try service.cancelJob(
            jobID: "11111111-1111-4111-8111-111111111111",
            idempotencyKey: "cancel-key-1"
        ))
        XCTAssertFalse(try service.cancelJob(
            jobID: "11111111-1111-4111-8111-111111111111",
            idempotencyKey: "cancel-key-2"
        ))

        let requests = try transport.recordedFrames().map(requestObject)
        XCTAssertEqual(requests.map { $0["method"] as? String }, [
            "engine.v1.hello", "engine.v1.hello",
            "engine.v1.jobs.cancel", "engine.v1.jobs.cancel",
        ])
        let firstCancel = try XCTUnwrap(requests[2]["params"] as? [String: Any])
        let secondCancel = try XCTUnwrap(requests[3]["params"] as? [String: Any])
        XCTAssertEqual(firstCancel["job_id"] as? String, "11111111-1111-4111-8111-111111111111")
        XCTAssertEqual(firstCancel["idempotency_key"] as? String, "cancel-key-1")
        XCTAssertEqual(secondCancel["idempotency_key"] as? String, "cancel-key-2")
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

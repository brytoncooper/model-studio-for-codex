import XCTest
import ModelDeckContracts
@testable import ModelDeckClient

private final class FixedSettingsCredential: EngineInstanceCredentialProviding {
    func readCredential() throws -> String { "operator-secret" }
}

final class EngineHostSettingsServiceTests: XCTestCase {
    private let sha = "sha256:" + String(repeating: "ab", count: 32)
    private let apiProfile = EngineAPIProfile(major: 1, minor: 0)

    private func descriptor() -> EngineRendezvousDescriptor {
        EngineRendezvousDescriptor(
            transport: "unix",
            socketPath: "/tmp/model-deck-engine.sock",
            engineInstanceID: "550e8400-e29b-41d4-a716-446655440000",
            instanceNonce: "rendezvous-nonce-7f3a",
            apiProfile: apiProfile
        )
    }

    private func helloUnauthenticated() throws -> Any {
        try JSONSerialization.jsonObject(
            with: try ContractJSON.loadFixture(named: "hello_result_unauthenticated.json"))
    }

    private func helloAuthenticated() -> [String: Any] {
        [
            "authenticated": true,
            "api_profile": ["major": 1, "minor": 0],
            "engine_instance_id": "550e8400-e29b-41d4-a716-446655440000",
            "instance_nonce": "rendezvous-nonce-7f3a",
            "capabilities": ["features": ["tools": "supported"]],
        ]
    }

    private func helloAuthenticatedSecond() -> [String: Any] {
        [
            "authenticated": false,
            "api_profile": ["major": 1, "minor": 0],
            "engine_instance_id": "550e8400-e29b-41d4-a716-446655440000",
            "instance_nonce": "rendezvous-nonce-7f3a",
            "capabilities": ["features": ["tools": "supported"]],
        ]
    }

    private func makeService(transports: [FakeEngineTransport]) -> (EngineHostSettingsService, [FakeEngineTransport]) {
        var vended: [FakeEngineTransport] = []
        let lock = NSLock()
        var index = 0
        let service = EngineHostSettingsService(
            rendezvous: descriptor(),
            makeTransport: {
                lock.lock()
                defer { lock.unlock() }
                let t = transports[index]
                index += 1
                vended.append(t)
                return t
            },
            credentialProvider: FixedSettingsCredential()
        )
        return (service, vended)
    }

    func testReadViaTwoHelloSequenceSendsSettingsMethod() async throws {
        let paramsData = try ContractJSON.loadFixture(named: "hosts_settings_read_params.json", valid: true)
        let params = try JSONDecoder().decode(HostSettingsReadParams.self, from: paramsData)
        let snapshot: [String: Any] = [
            "host_id": "codex.cli",
            "document_id": "config.toml",
            "document_revision": "absent",
            "exists": false,
            "target": [
                "display_name": "Codex CLI",
                "display_path": "/tmp/config.toml",
                "scope": "user",
                "writable": true,
            ],
            "schema_profile": [
                "schema_id": "codex-cli",
                "schema_revision": "1",
                "host_version": "0.1",
                "support_level": "supported",
            ],
            "precedence": [],
            "raw_toml": "",
            "structured": ["sections": []],
            "context_revision": "cr-1",
        ]
        let transport = FakeEngineTransport(responses: [
            wrap(id: "md-1", result: try helloUnauthenticated()),
            wrap(id: "md-2", result: helloAuthenticated()),
            wrap(id: "md-3", result: ["snapshot": snapshot]),
        ])
        let lock = NSLock()
        var vended: [FakeEngineTransport] = []
        let service = EngineHostSettingsService(
            rendezvous: descriptor(),
            makeTransport: { lock.lock(); defer { lock.unlock() }; vended.append(transport); return transport },
            credentialProvider: FixedSettingsCredential()
        )
        let result = try await service.read(params: params)
        XCTAssertEqual(result.snapshot.hostID, "codex.cli")
        XCTAssertEqual(result.snapshot.contextRevision, "cr-1")
        let frames = transport.recordedFrames()
        XCTAssertEqual(frames.count, 3)
        XCTAssertEqual(try recordedMethod(frames[0]), "engine.v1.hello")
        XCTAssertEqual(try recordedMethod(frames[1]), "engine.v1.hello")
        XCTAssertEqual(try recordedMethod(frames[2]), EngineHostSettingsService.readMethod)
        XCTAssertEqual(vended.count, 1)
    }

    func testAuthenticationFailureSendsNoSettings() async throws {
        let paramsData = try ContractJSON.loadFixture(named: "hosts_settings_read_params.json", valid: true)
        let params = try JSONDecoder().decode(HostSettingsReadParams.self, from: paramsData)
        let transport = FakeEngineTransport(responses: [
            wrap(id: "md-1", result: try helloUnauthenticated()),
            wrap(id: "md-2", result: helloAuthenticatedSecond()),
        ])
        let service = EngineHostSettingsService(
            rendezvous: descriptor(),
            makeTransport: { transport },
            credentialProvider: FixedSettingsCredential()
        )
        do {
            _ = try await service.read(params: params)
            XCTFail("expected authentication failure")
        } catch let error as EngineClientError {
            guard case .negotiationFailed = error else {
                return XCTFail("expected negotiationFailed, got \(error)")
            }
        }
        let frames = transport.recordedFrames()
        XCTAssertEqual(frames.count, 2)
        XCTAssertEqual(try recordedMethod(frames[0]), "engine.v1.hello")
        XCTAssertEqual(try recordedMethod(frames[1]), "engine.v1.hello")
    }

    func testConcurrentReadsAreIsolated() async throws {
        let paramsData = try ContractJSON.loadFixture(named: "hosts_settings_read_params.json", valid: true)
        let params = try JSONDecoder().decode(HostSettingsReadParams.self, from: paramsData)
        func snapshotData(_ revision: String) -> [String: Any] {
            [
                "host_id": "codex.cli",
                "document_id": "config.toml",
                "document_revision": "absent",
                "exists": false,
                "target": [
                    "display_name": "Codex CLI",
                    "display_path": "/tmp/config.toml",
                    "scope": "user",
                    "writable": true,
                ],
                "schema_profile": [
                    "schema_id": "codex-cli",
                    "schema_revision": "1",
                    "host_version": "0.1",
                    "support_level": "supported",
                ],
                "precedence": [],
                "raw_toml": "",
                "structured": ["sections": []],
                "context_revision": revision,
            ]
        }
        let t1 = FakeEngineTransport(responses: [
            wrap(id: "md-1", result: try helloUnauthenticated()),
            wrap(id: "md-2", result: helloAuthenticated()),
            wrap(id: "md-3", result: ["snapshot": snapshotData("cr-1")]),
        ])
        let t2 = FakeEngineTransport(responses: [
            wrap(id: "md-1", result: try helloUnauthenticated()),
            wrap(id: "md-2", result: helloAuthenticated()),
            wrap(id: "md-3", result: ["snapshot": snapshotData("cr-2")]),
        ])
        let lock = NSLock()
        var queue = [t1, t2]
        let service = EngineHostSettingsService(
            rendezvous: descriptor(),
            makeTransport: {
                lock.lock()
                defer { lock.unlock() }
                return queue.removeFirst()
            },
            credentialProvider: FixedSettingsCredential()
        )
        async let first = service.read(params: params)
        async let second = service.read(params: params)
        let (r1, r2) = try await (first, second)
        XCTAssertNotEqual(r1.snapshot.contextRevision, r2.snapshot.contextRevision)
        for transport in [t1, t2] {
            let frames = transport.recordedFrames()
            XCTAssertEqual(frames.count, 3)
            XCTAssertEqual(try recordedMethod(frames[2]), EngineHostSettingsService.readMethod)
        }
    }

    func testPreviewAndSavePropagateExactServerTokens() async throws {
        let previewData = try ContractJSON.loadFixture(named: "hosts_settings_preview_result_valid.json", valid: true)
        let previewFixture = try JSONDecoder().decode(HostSettingsPreviewResult.self, from: previewData)
        let previewToken = try XCTUnwrap(previewFixture.preview)
        let paramsData = try ContractJSON.loadFixture(named: "hosts_settings_validate_params_structured.json", valid: true)
        let validateParams = try JSONDecoder().decode(HostSettingsValidateParams.self, from: paramsData)
        let previewParams = HostSettingsPreviewParams(
            hostID: validateParams.hostID,
            documentID: validateParams.documentID,
            expectedContentHash: validateParams.expectedContentHash,
            draft: validateParams.draft,
            contextRevision: validateParams.contextRevision
        )
        let previewObject = try XCTUnwrap(try JSONSerialization.jsonObject(with: previewData) as? [String: Any])
        let previewTransport = FakeEngineTransport(responses: [
            wrap(id: "md-1", result: try helloUnauthenticated()),
            wrap(id: "md-2", result: helloAuthenticated()),
            wrap(id: "md-3", result: previewObject),
        ])
        let previewService = EngineHostSettingsService(
            rendezvous: descriptor(),
            makeTransport: { previewTransport },
            credentialProvider: FixedSettingsCredential()
        )
        let previewResult = try await previewService.preview(params: previewParams)
        XCTAssertEqual(previewResult.preview?.previewID, previewToken.previewID)

        let saveObject = try XCTUnwrap(try ContractJSON.loadFixtureJSONObject(named: "hosts_settings_save_result_changed.json", valid: true) as? [String: Any])
        let saveTransport = FakeEngineTransport(responses: [
            wrap(id: "md-1", result: try helloUnauthenticated()),
            wrap(id: "md-2", result: helloAuthenticated()),
            wrap(id: "md-3", result: saveObject),
        ])
        let saveParams = HostSettingsSaveParams(
            hostID: validateParams.hostID,
            documentID: validateParams.documentID,
            expectedContentHash: validateParams.expectedContentHash,
            contextRevision: previewResult.contextRevision,
            previewID: previewToken.previewID,
            candidateContentHash: previewToken.candidateContentHash,
            candidateRawTOML: previewFixture.candidateRawTOML ?? "",
            idempotencyKey: "test-key-1"
        )
        let saveService = EngineHostSettingsService(
            rendezvous: descriptor(),
            makeTransport: { saveTransport },
            credentialProvider: FixedSettingsCredential()
        )
        let saveResult = try await saveService.save(params: saveParams)
        XCTAssertTrue(saveResult.saved)
        let frames = saveTransport.recordedFrames()
        XCTAssertEqual(frames.count, 3)
        XCTAssertEqual(try recordedMethod(frames[2]), EngineHostSettingsService.saveMethod)
        let sentParams = try XCTUnwrap(try recordedParams(frames[2]) as? [String: Any])
        XCTAssertEqual(sentParams["preview_id"] as? String, previewToken.previewID)
        XCTAssertEqual(sentParams["candidate_content_hash"] as? String, previewToken.candidateContentHash)
        XCTAssertEqual(sentParams["context_revision"] as? String, previewResult.contextRevision)
        XCTAssertEqual(sentParams["idempotency_key"] as? String, "test-key-1")
    }

    func testSaveRejectsSchemaViolationWithoutRetry() async throws {
        let saveParams = HostSettingsSaveParams(
            hostID: "codex.cli",
            documentID: "config.toml",
            expectedContentHash: .absent,
            contextRevision: "cr-1",
            previewID: "preview-1",
            candidateContentHash: sha,
            candidateRawTOML: "",
            idempotencyKey: "test-key-1"
        )
        let transport = FakeEngineTransport(responses: [
            wrap(id: "md-1", result: try helloUnauthenticated()),
            wrap(id: "md-2", result: helloAuthenticated()),
            wrap(id: "md-3", result: ["saved": true]),
        ])
        let service = EngineHostSettingsService(
            rendezvous: descriptor(),
            makeTransport: { transport },
            credentialProvider: FixedSettingsCredential()
        )
        await XCTAssertThrowsErrorAsync(try await service.save(params: saveParams))
        XCTAssertEqual(transport.recordedFrames().count, 3)
    }

    func testMalformedResponseSurfacedWithoutRetry() async throws {
        let paramsData = try ContractJSON.loadFixture(named: "hosts_settings_read_params.json", valid: true)
        let params = try JSONDecoder().decode(HostSettingsReadParams.self, from: paramsData)
        let transport = FakeEngineTransport(responses: [
            wrap(id: "md-1", result: try helloUnauthenticated()),
            wrap(id: "md-2", result: helloAuthenticated()),
            Data("not json".utf8),
        ])
        let service = EngineHostSettingsService(
            rendezvous: descriptor(),
            makeTransport: { transport },
            credentialProvider: FixedSettingsCredential()
        )
        await XCTAssertThrowsErrorAsync(try await service.read(params: params))
        XCTAssertEqual(transport.recordedFrames().count, 3)
    }

    func testEngineErrorSurfacedWithoutRetry() async throws {
        let paramsData = try ContractJSON.loadFixture(named: "hosts_settings_read_params.json", valid: true)
        let params = try JSONDecoder().decode(HostSettingsReadParams.self, from: paramsData)
        let object: [String: Any] = ["jsonrpc": "2.0", "id": "md-3", "error": ["code": -32000, "message": "stale context"]]
        let transport = FakeEngineTransport(responses: [
            wrap(id: "md-1", result: try helloUnauthenticated()),
            wrap(id: "md-2", result: helloAuthenticated()),
            try JSONSerialization.data(withJSONObject: object),
        ])
        let service = EngineHostSettingsService(
            rendezvous: descriptor(),
            makeTransport: { transport },
            credentialProvider: FixedSettingsCredential()
        )
        do {
            _ = try await service.read(params: params)
            XCTFail("expected engine error")
        } catch let error as EngineClientError {
            XCTAssertEqual(error, .unavailable("engine hosts.settings request failed (\(EngineHostSettingsService.readMethod))"))
        }
        XCTAssertEqual(transport.recordedFrames().count, 3)
    }

    func testInvokeValidatedRequiresAuthentication() throws {
        let transport = FakeEngineTransport(responses: [])
        let client = ModelDeckEngineClient(transport: transport, credentialProvider: FixedSettingsCredential())
        let params = HostSettingsReadParams(hostID: "codex.cli")
        do {
            let _: HostSettingsReadResult = try client.invokeValidated(
                method: EngineHostSettingsService.readMethod,
                params: params,
                paramsSchemaRef: "contracts/engine.v1/methods/hosts.settings.read.params.schema.json",
                resultSchemaRef: "contracts/engine.v1/methods/hosts.settings.read.result.schema.json"
            )
            XCTFail("expected negotiationFailed")
        } catch {
            guard let clientError = error as? EngineClientError, case .negotiationFailed = clientError else {
                return XCTFail("expected negotiationFailed")
            }
        }
        XCTAssertEqual(transport.recordedFrames().count, 0)
    }

    func testCancelDuringBlockedFactorySendsNoHello() async throws {
        let paramsData = try ContractJSON.loadFixture(named: "hosts_settings_read_params.json", valid: true)
        let params = try JSONDecoder().decode(HostSettingsReadParams.self, from: paramsData)
        let enteredFactory = DispatchSemaphore(value: 0)
        let releaseFactory = DispatchSemaphore(value: 0)
        let transport = FakeEngineTransport(responses: [
            wrap(id: "md-1", result: try helloUnauthenticated()),
            wrap(id: "md-2", result: helloAuthenticated()),
            wrap(id: "md-3", result: ["snapshot": ["host_id": "codex.cli"]]),
        ])
        let service = EngineHostSettingsService(
            rendezvous: descriptor(),
            makeTransport: { enteredFactory.signal(); releaseFactory.wait(); return transport },
            credentialProvider: FixedSettingsCredential()
        )
        let task = Task { try await service.read(params: params) }
        XCTAssertEqual(enteredFactory.wait(timeout: .now() + 5), .success, "factory was never entered")
        service.cancel()
        releaseFactory.signal()
        do {
            _ = try await task.value
            XCTFail("expected cancellation")
        } catch let error as EngineClientError {
            XCTAssertEqual(error, .requestCancelled)
        }
        XCTAssertEqual(transport.recordedFrames().count, 0)
    }

    func testRemoteErrorMessageIsSanitized() async throws {
        let paramsData = try ContractJSON.loadFixture(named: "hosts_settings_read_params.json", valid: true)
        let params = try JSONDecoder().decode(HostSettingsReadParams.self, from: paramsData)
        let object: [String: Any] = ["jsonrpc": "2.0", "id": "md-3", "error": ["code": -32000, "message": "stale context secret-token-abc"]]
        let transport = FakeEngineTransport(responses: [
            wrap(id: "md-1", result: try helloUnauthenticated()),
            wrap(id: "md-2", result: helloAuthenticated()),
            try JSONSerialization.data(withJSONObject: object),
        ])
        let service = EngineHostSettingsService(
            rendezvous: descriptor(),
            makeTransport: { transport },
            credentialProvider: FixedSettingsCredential()
        )
        do {
            _ = try await service.read(params: params)
            XCTFail("expected engine error")
        } catch let error as EngineClientError {
            XCTAssertEqual(error, .unavailable("engine hosts.settings request failed (\(EngineHostSettingsService.readMethod))"))
            XCTAssertFalse(String(describing: error).contains("secret-token-abc"))
        }
        XCTAssertEqual(transport.recordedFrames().count, 3)
    }

    private func wrap(id: String, result: Any) -> Data {
        let object: [String: Any] = ["jsonrpc": "2.0", "id": id, "result": result]
        guard let data = try? JSONSerialization.data(withJSONObject: object) else {
            XCTFail("failed to encode json rpc response")
            return Data()
        }
        return data
    }

    private func recordedMethod(_ frame: Data) throws -> String {
        var data = frame
        if data.last == 0x0A { data = data.dropLast() }
        let object = try XCTUnwrap(try JSONSerialization.jsonObject(with: Data(data)) as? [String: Any])
        return try XCTUnwrap(object["method"] as? String)
    }

    private func recordedParams(_ frame: Data) throws -> Any {
        var data = frame
        if data.last == 0x0A { data = data.dropLast() }
        let object = try XCTUnwrap(try JSONSerialization.jsonObject(with: Data(data)) as? [String: Any])
        return try XCTUnwrap(object["params"])
    }

    private func XCTAssertThrowsErrorAsync(_ expression: @autoclosure () async throws -> some Any) async {
        do {
            _ = try await expression()
            XCTFail("expected error")
        } catch {
            return
        }
    }
}

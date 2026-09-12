import XCTest
import ModelDeckContracts
@testable import ModelDeckClient

final class HostSettingsTypesTests: XCTestCase {
    private func roundTrip<T: Codable & Equatable>(_ value: T) throws {
        let data = try JSONEncoder().encode(value)
        XCTAssertEqual(try JSONDecoder().decode(T.self, from: data), value)
    }

    func testReadParamsFixture() throws {
        let data = try ContractJSON.loadFixture(named: "hosts_settings_read_params.json", valid: true)
        let params = try ContractJSON.decodeValidated(
            HostSettingsReadParams.self,
            from: data,
            schemaRef: "contracts/engine.v1/methods/hosts.settings.read.params.schema.json"
        )
        XCTAssertEqual(params.hostID, "com.openai.codex")
        try roundTrip(params)
    }

    func testValidateStructuredParamsFixture() throws {
        let data = try ContractJSON.loadFixture(named: "hosts_settings_validate_params_structured.json", valid: true)
        let params = try ContractJSON.decodeValidated(
            HostSettingsValidateParams.self,
            from: data,
            schemaRef: "contracts/engine.v1/methods/hosts.settings.validate.params.schema.json"
        )
        XCTAssertEqual(params.expectedContentHash, .absent)
        XCTAssertEqual(params.contextRevision, "ctx-001")
        guard case .structured(let changes) = params.draft, changes.count == 2 else {
            return XCTFail("expected two structured changes")
        }
        guard case .set(let field, _, let value) = changes[0] else {
            return XCTFail("expected set change")
        }
        XCTAssertEqual(field, "model")
        XCTAssertEqual(value, .string("gpt-6"))
        guard case .unset(let unsetField, _) = changes[1] else {
            return XCTFail("expected unset change")
        }
        XCTAssertEqual(unsetField, "api_key")
        try roundTrip(params)
    }

    func testPreviewValidFixture() throws {
        let data = try ContractJSON.loadFixture(named: "hosts_settings_preview_result_valid.json", valid: true)
        let result = try ContractJSON.decodeValidated(
            HostSettingsPreviewResult.self,
            from: data,
            schemaRef: "contracts/engine.v1/methods/hosts.settings.preview.result.schema.json"
        )
        XCTAssertTrue(result.valid)
        XCTAssertEqual(result.validationLevel, .schema)
        let preview = try XCTUnwrap(result.preview)
        XCTAssertEqual(preview.previewID, "preview-001")
        XCTAssertEqual(preview.baseContentHash, .absent)
        XCTAssertTrue(preview.changed)
        XCTAssertFalse(preview.diffTruncated)
        XCTAssertEqual(preview.changedFieldIDs, ["model"])
        let structured = try XCTUnwrap(result.candidateStructured)
        XCTAssertEqual(structured.sections.count, 1)
        XCTAssertEqual(structured.sections[0].fields.count, 2)
        guard case .public(let model) = structured.sections[0].fields[0] else {
            return XCTFail("expected public descriptor")
        }
        XCTAssertEqual(model.value, .string("gpt-6"))
        guard case .secret(let secret) = structured.sections[0].fields[1] else {
            return XCTFail("expected secret descriptor")
        }
        XCTAssertTrue(secret.configured)
        try roundTrip(result)
    }

    func testPreviewInvalidFixtureHasNullPreview() throws {
        let data = try ContractJSON.loadFixture(named: "hosts_settings_preview_invalid.json", valid: true)
        let result = try ContractJSON.decodeValidated(
            HostSettingsPreviewResult.self,
            from: data,
            schemaRef: "contracts/engine.v1/methods/hosts.settings.preview.result.schema.json"
        )
        XCTAssertFalse(result.valid)
        XCTAssertNil(result.preview)
    }

    func testPreviewValidWithNullPreviewRejected() throws {
        let data = try ContractJSON.loadFixture(named: "hosts_settings_preview_valid_null.json", valid: false)
        XCTAssertThrowsError(try JSONDecoder().decode(HostSettingsPreviewResult.self, from: data))
    }

    func testSaveParamsAndResultFixtures() throws {
        let paramsData = try ContractJSON.loadFixture(named: "hosts_settings_save_params.json", valid: true)
        let params = try ContractJSON.decodeValidated(
            HostSettingsSaveParams.self,
            from: paramsData,
            schemaRef: "contracts/engine.v1/methods/hosts.settings.save.params.schema.json"
        )
        XCTAssertEqual(params.previewID, "preview-001")
        XCTAssertEqual(params.idempotencyKey, "save-001")
        try roundTrip(params)

        let resultData = try ContractJSON.loadFixture(named: "hosts_settings_save_result_changed.json", valid: true)
        let result = try ContractJSON.decodeValidated(
            HostSettingsSaveResult.self,
            from: resultData,
            schemaRef: "contracts/engine.v1/methods/hosts.settings.save.result.schema.json"
        )
        XCTAssertTrue(result.saved)
        XCTAssertTrue(result.changed)
        XCTAssertNil(result.backup)
        XCTAssertEqual(result.previousContentHash, .absent)
        XCTAssertEqual(result.applicationEffects, [.futureSession])
        try roundTrip(result)
    }

    func testSecretDescriptorCarriesNoValueKeys() throws {
        let data = try ContractJSON.loadFixture(named: "hosts_settings_secret_entry_configured.json", valid: true)
        let descriptor = try JSONDecoder().decode(HostFieldDescriptor.self, from: data)
        guard case .secret(let secret) = descriptor else {
            return XCTFail("expected secret descriptor")
        }
        XCTAssertEqual(secret.fieldID, "api_key")
        XCTAssertTrue(secret.configured)
        let encoded = try JSONEncoder().encode(descriptor)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: encoded) as? [String: Any])
        XCTAssertNotNil(object["configured"])
        for forbidden in ["value", "default", "effective", "entries"] {
            XCTAssertNil(object[forbidden], "secret descriptor encoded \(forbidden)")
        }
    }

    func testSecretWithValueRejectedBySchema() throws {
        try ContractJSON.validateFixture(
            named: "hosts_settings_secret_with_value.json",
            schemaRef: "contracts/common/host_settings.schema.json#/definitions/field_descriptor",
            valid: false
        )
    }

    func testUnsetWithValueRejectedBySchema() throws {
        try ContractJSON.validateFixture(
            named: "hosts_settings_change_unset_with_value.json",
            schemaRef: "contracts/common/host_settings.schema.json#/definitions/structured_change",
            valid: false
        )
    }

    func testValidateMissingContextRejectedBySchema() throws {
        try ContractJSON.validateFixture(
            named: "hosts_settings_validate_missing_context.json",
            schemaRef: "contracts/engine.v1/methods/hosts.settings.validate.params.schema.json",
            valid: false
        )
    }

    func testRawDraftRoundTrip() throws {
        let params = HostSettingsValidateParams(
            hostID: "com.openai.codex",
            documentID: "ref:codex-user-config",
            expectedContentHash: .absent,
            draft: .raw("model = \"gpt-6\"\n"),
            contextRevision: "ctx-001"
        )
        try roundTrip(params)
    }

    func testStructuredDraftRoundTrip() throws {
        let draft = HostDraft.structured([
            .set(fieldID: "model", entryID: nil, value: .string("gpt-6")),
            .unset(fieldID: "api_key", entryID: "primary"),
        ])
        try roundTrip(draft)
    }

    func testContextAndPreviewRoundTrip() throws {
        let result = HostSettingsPreviewResult(
            valid: true,
            validationLevel: .schema,
            candidateContentHash: "sha256:0000000000000000000000000000000000000000000000000000000000000000",
            candidateRawTOML: "model = \"gpt-6\"\n",
            candidateStructured: HostStructuredDocument(sections: []),
            diagnostics: [HostDiagnostic(severity: .info, code: "ok", message: "clean")],
            contextRevision: "ctx-001",
            preview: HostSettingsPreview(
                previewID: "preview-001",
                baseContentHash: .absent,
                candidateContentHash: "sha256:0000000000000000000000000000000000000000000000000000000000000000",
                changed: true,
                diff: "+model\n",
                diffTruncated: false,
                changedFieldIDs: ["model"],
                applicationEffects: [.futureSession],
                protectedProjectionChanges: false
            )
        )
        try roundTrip(result)
    }

    func testInvalidPreviewWirePreservesNullPreview() throws {
        let data = try ContractJSON.loadFixture(named: "hosts_settings_preview_invalid.json", valid: true)
        let result = try ContractJSON.decodeValidated(
            HostSettingsPreviewResult.self,
            from: data,
            schemaRef: "contracts/engine.v1/methods/hosts.settings.preview.result.schema.json"
        )
        XCTAssertNil(result.preview)
        let encoded = try JSONEncoder().encode(result)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: encoded) as? [String: Any])
        XCTAssertTrue(object.keys.contains("preview"))
        XCTAssertTrue(object["preview"] is NSNull)
        try SchemaValidator.validate(
            instance: object,
            schemaRef: "contracts/engine.v1/methods/hosts.settings.preview.result.schema.json"
        )
    }

    func testSaveResultWirePreservesNullBackup() throws {
        let data = try ContractJSON.loadFixture(named: "hosts_settings_save_result_changed.json", valid: true)
        let result = try ContractJSON.decodeValidated(
            HostSettingsSaveResult.self,
            from: data,
            schemaRef: "contracts/engine.v1/methods/hosts.settings.save.result.schema.json"
        )
        XCTAssertNil(result.backup)
        let encoded = try JSONEncoder().encode(result)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: encoded) as? [String: Any])
        XCTAssertTrue(object.keys.contains("backup"))
        XCTAssertTrue(object["backup"] is NSNull)
        try SchemaValidator.validate(
            instance: object,
            schemaRef: "contracts/engine.v1/methods/hosts.settings.save.result.schema.json"
        )
    }

    func testPreviewMissingKeyRejected() throws {
        let data = try ContractJSON.loadFixture(named: "hosts_settings_preview_invalid.json", valid: true)
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        object.removeValue(forKey: "preview")
        let stripped = try JSONSerialization.data(withJSONObject: object)
        XCTAssertThrowsError(try JSONDecoder().decode(HostSettingsPreviewResult.self, from: stripped))
    }

    func testSaveMissingBackupKeyRejected() throws {
        let data = try ContractJSON.loadFixture(named: "hosts_settings_save_result_changed.json", valid: true)
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        object.removeValue(forKey: "backup")
        let stripped = try JSONSerialization.data(withJSONObject: object)
        XCTAssertThrowsError(try JSONDecoder().decode(HostSettingsSaveResult.self, from: stripped))
    }

    func testUnsetChangeOmitsValueKey() throws {
        let change = HostStructuredChange.unset(fieldID: "api_key", entryID: nil)
        let data = try JSONEncoder().encode(change)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(object["operation"] as? String, "unset")
        XCTAssertNil(object["value"])
    }
}

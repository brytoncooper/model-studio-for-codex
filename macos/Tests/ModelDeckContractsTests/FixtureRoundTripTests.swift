import Foundation
import XCTest
@testable import ModelDeckContracts

final class FixtureRoundTripTests: XCTestCase {
    func testSettingsPreviewOneOfEnforcesSiblingCandidateHash() throws {
        let schemaRef = "contracts/engine.v1/methods/hosts.settings.preview.result.schema.json"
        let valid = try ContractJSON.loadFixtureJSONObject(named: "hosts_settings_preview_result_valid.json", valid: true)
        let invalid = try ContractJSON.loadFixtureJSONObject(named: "hosts_settings_preview_absent_candidate.json", valid: false)
        XCTAssertNoThrow(try SchemaValidator.validate(instance: valid, schemaRef: schemaRef))
        XCTAssertThrowsError(try SchemaValidator.validate(instance: invalid, schemaRef: schemaRef)) { error in
            XCTAssertTrue(error is SchemaValidationError)
        }
    }

    func testSettingsFieldAllOfEnforcesSiblingConfiguredType() throws {
        let schemaRef = "contracts/common/host_settings.schema.json#/definitions/field_descriptor"
        let valid = try ContractJSON.loadFixtureJSONObject(named: "hosts_settings_secret_entry_configured.json", valid: true)
        var invalid = try XCTUnwrap(valid as? [String: Any])
        invalid["configured"] = "true"
        XCTAssertNoThrow(try SchemaValidator.validate(instance: valid, schemaRef: schemaRef))
        XCTAssertThrowsError(try SchemaValidator.validate(instance: invalid, schemaRef: schemaRef)) { error in
            XCTAssertTrue(error is SchemaValidationError)
        }
    }

    func testReferenceEnforcesSiblingStringLength() throws {
        let schema: [String: Any] = [
            "$ref": "#/definitions/opaque_ref",
            "maxLength": 8,
        ]
        let base = "contracts/common/types.schema.json"
        XCTAssertNoThrow(try SchemaValidator.validate(instance: "ref:ok", schema: schema, baseDocumentPath: base))
        XCTAssertThrowsError(try SchemaValidator.validate(instance: "ref:too-long", schema: schema, baseDocumentPath: base)) { error in
            XCTAssertTrue(error is SchemaValidationError)
        }
    }

    func testValidManifestFixturesValidate() throws {
        let manifest = try ContractJSON.loadManifest()
        let valid = manifest["valid"] as! [[String: Any]]
        for entry in valid {
            let name = entry["file"] as! String
            let schemaRef = entry["schema"] as! String
            let instance = try ContractJSON.loadFixtureJSONObject(named: name, valid: true)
            XCTAssertNoThrow(try SchemaValidator.validate(instance: instance, schemaRef: schemaRef), name)
        }
    }

    func testInvalidManifestFixturesReject() throws {
        let manifest = try ContractJSON.loadManifest()
        let invalid = manifest["invalid"] as! [[String: Any]]
        for entry in invalid {
            let name = entry["file"] as! String
            let schemaRef = entry["schema"] as! String
            let instance = try ContractJSON.loadFixtureJSONObject(named: name, valid: false)
            XCTAssertThrowsError(try SchemaValidator.validate(instance: instance, schemaRef: schemaRef), name) { error in
                XCTAssertTrue(error is SchemaValidationError, name)
            }
        }
    }

    func testManifestValidFixturesCanonicalRoundTrip() throws {
        let manifest = try ContractJSON.loadManifest()
        let valid = manifest["valid"] as! [[String: Any]]
        for entry in valid {
            let name = entry["file"] as! String
            let data = try ContractJSON.loadFixture(named: name, valid: true)
            let object = try JSONSerialization.jsonObject(with: data)
            let canonical = try CanonicalJSON.canonicalData(from: object)
            let reparsed = try JSONSerialization.jsonObject(with: canonical)
            let canonical2 = try CanonicalJSON.canonicalData(from: reparsed)
            XCTAssertEqual(canonical, canonical2, name)
        }
    }

    func testJsonRpcRequestPreservesParams() throws {
        let data = try ContractJSON.loadFixture(named: "jsonrpc_request.json", valid: true)
        let decoded = try JsonRpcRequest.parse(data: data)
        XCTAssertEqual(decoded.jsonrpc, "2.0")
        XCTAssertEqual(decoded.method, "engine.v1.models.list")
        XCTAssertEqual(decoded.params?["collection"], .string("registered"))
        let reencoded = try ContractJSON.encode(decoded)
        let again = try JsonRpcRequest.parse(data: reencoded)
        XCTAssertEqual(decoded, again)
    }

    func testCapabilityUnknownFixturePreservesUnknown() throws {
        let data = try ContractJSON.loadFixture(named: "capability_features_unknown.json", valid: true)
        let decoded = try CapabilityFeaturesFixture.parse(data: data)
        XCTAssertEqual(decoded.capabilities.features["tools"], "unknown")
    }

    func testToolCallFixturePreservesArguments() throws {
        let data = try ContractJSON.loadFixture(named: "tool_call.json", valid: true)
        let decoded = try ToolCallFixture.parse(data: data)
        XCTAssertEqual(decoded.arguments, .object(["q": .string("contracts")]))
    }

    func testTerminalEventFixturePreservesTerminalResult() throws {
        let data = try ContractJSON.loadFixture(named: "run_event_completed.json", valid: true)
        let decoded = try RunEventCompletedFixture.parse(data: data)
        XCTAssertEqual(decoded.terminalResult.outcome, "completed")
    }

    func testOneOfRequiresExactlyOneBranch() throws {
        let schema: [String: Any] = [
            "oneOf": [
                ["type": "string"],
                ["type": "integer"],
            ],
        ]
        XCTAssertNoThrow(try SchemaValidator.validate(instance: "ok", schema: schema, baseDocumentPath: "contracts/common/types.schema.json"))
        XCTAssertThrowsError(try SchemaValidator.validate(instance: true, schema: schema, baseDocumentPath: "contracts/common/types.schema.json"))
    }

    func testAllOfRequiresEveryBranch() throws {
        let schema: [String: Any] = [
            "allOf": [
                ["type": "string", "minLength": 2],
                ["type": "string", "maxLength": 4],
            ],
        ]
        XCTAssertNoThrow(try SchemaValidator.validate(instance: "ok", schema: schema, baseDocumentPath: "contracts/common/types.schema.json"))
        XCTAssertThrowsError(try SchemaValidator.validate(instance: "toolong", schema: schema, baseDocumentPath: "contracts/common/types.schema.json"))
    }

    func testNotRejectsMatchingInstance() throws {
        let schema: [String: Any] = [
            "not": ["type": "string"],
        ]
        XCTAssertNoThrow(try SchemaValidator.validate(instance: 1, schema: schema, baseDocumentPath: "contracts/common/types.schema.json"))
        XCTAssertThrowsError(try SchemaValidator.validate(instance: "nope", schema: schema, baseDocumentPath: "contracts/common/types.schema.json"))
    }

    func testTypeRejectsBooleanAsNumber() throws {
        let schema: [String: Any] = ["type": "number"]
        XCTAssertNoThrow(try SchemaValidator.validate(instance: 1.5, schema: schema, baseDocumentPath: "contracts/common/types.schema.json"))
        XCTAssertThrowsError(try SchemaValidator.validate(instance: true, schema: schema, baseDocumentPath: "contracts/common/types.schema.json"))
    }

    func testCapabilityTriStateRejectsDenied() throws {
        let schema = try SchemaValidator.schemaNode(
            schemaRef: "contracts/common/types.schema.json#/definitions/capability_tri_state"
        )
        XCTAssertThrowsError(try SchemaValidator.validate(instance: "denied", schema: schema, baseDocumentPath: "contracts/common/types.schema.json"))
    }

    func testValidateFixtureHelperAcceptsRejectedInvalidFixture() throws {
        let manifest = try ContractJSON.loadManifest()
        let invalid = manifest["invalid"] as! [[String: Any]]
        guard let entry = invalid.first else {
            XCTFail("manifest has no invalid fixtures")
            return
        }
        let name = entry["file"] as! String
        let schemaRef = entry["schema"] as! String
        XCTAssertNoThrow(try ContractJSON.validateFixture(named: name, schemaRef: schemaRef, valid: false))
    }

    func testBundledSchemaLoadRejectsPathTraversal() {
        XCTAssertThrowsError(
            try SchemaValidator.loadSchemaDocument(relative: "contracts/../../outside.schema.json")
        ) { error in
            XCTAssertTrue(error is SchemaValidationError)
        }
    }

    func testBundledSchemaLoadRejectsRemoteURI() {
        XCTAssertThrowsError(
            try SchemaValidator.loadSchemaDocument(relative: "https://example.com/schema.schema.json")
        ) { error in
            XCTAssertTrue(error is SchemaValidationError)
        }
    }

    func testDecodedZeroAndOneRemainNumbers() throws {
        let values = try JSONSerialization.jsonObject(with: Data("[0,1,true,false]".utf8)) as! [Any]
        let base = "contracts/common/types.schema.json"
        for value in values.prefix(2) {
            XCTAssertNoThrow(try SchemaValidator.validate(instance: value, schema: ["type": "integer"], baseDocumentPath: base))
            XCTAssertThrowsError(try SchemaValidator.validate(instance: value, schema: ["type": "boolean"], baseDocumentPath: base))
        }
        for value in values.suffix(2) {
            XCTAssertNoThrow(try SchemaValidator.validate(instance: value, schema: ["type": "boolean"], baseDocumentPath: base))
            XCTAssertThrowsError(try SchemaValidator.validate(instance: value, schema: ["type": "integer"], baseDocumentPath: base))
        }
    }

    func testUnknownSchemaKeywordFailsClosed() throws {
        let schema: [String: Any] = [
            "type": "string",
            "$comment": "not allowed",
        ]
        XCTAssertThrowsError(try SchemaValidator.validate(instance: "x", schema: schema, baseDocumentPath: "contracts/common/types.schema.json"))
    }
}

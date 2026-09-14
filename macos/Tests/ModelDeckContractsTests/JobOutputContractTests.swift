import Foundation
import XCTest
@testable import ModelDeckContracts

/// Verifies the generic bounded job-output contract shared between
/// `engine.v1.jobs.get.result` and `plugin.v1.broker/jobs.complete.params`.
///
/// Both schemas bind `output` to the shared `json_value` definition
/// (bounded depth/items/keys/length). The shape is intentionally generic:
/// application-owned JSON, no filesystem path, no Notebook-specific
/// schema. The Notebook plugin uses `{media_type, suggested_filename,
/// content}` as one valid payload under this contract.
final class JobOutputContractTests: XCTestCase {
    private let jobsGetResultSchemaRef =
        "contracts/engine.v1/methods/jobs.get.result.schema.json"
    private let jobsCompleteParamsSchemaRef =
        "contracts/plugin.v1/broker/jobs.complete.params.schema.json"

    func testMinimalDocumentValidatesAgainstJobsGetResult() throws {
        let document: [String: Any] = [
            "job_id": "550e8400-e29b-41d4-a716-446655440099",
            "state": "completed",
            "output": [
                "media_type": "text/markdown",
                "suggested_filename": "session-2026-09-13.md",
                "content": "# Session notes\n\nFirst observation.\n",
            ],
        ]
        XCTAssertNoThrow(
            try SchemaValidator.validate(instance: document, schemaRef: jobsGetResultSchemaRef)
        )
    }

    func testMinimalDocumentValidatesAgainstJobsCompleteParams() throws {
        let document: [String: Any] = [
            "job_id": "550e8400-e29b-41d4-a716-446655440099",
            "output": [
                "media_type": "text/markdown",
                "suggested_filename": "session-2026-09-13.md",
                "content": "# Session notes\n\nFirst observation.\n",
            ],
        ]
        XCTAssertNoThrow(
            try SchemaValidator.validate(instance: document, schemaRef: jobsCompleteParamsSchemaRef)
        )
    }

    func testCanonicalValidFixtureValidatesBothSchemas() throws {
        let fixture = try ContractJSON.loadFixtureJSONObject(
            named: "plugin_job_output_minimal.json",
            valid: true
        )
        // jobs.complete.params accepts the fixture as-is.
        XCTAssertNoThrow(
            try SchemaValidator.validate(instance: fixture, schemaRef: jobsCompleteParamsSchemaRef)
        )
        // jobs.get.result requires `state`. The fixture carries job_id
        // and output; pair it with state so the schema accepts it.
        let fixtureDict = fixture as? [String: Any] ?? [:]
        let asResult: [String: Any] = [
            "state": "completed",
            "job_id": fixtureDict["job_id"] as Any,
            "output": fixtureDict["output"] as Any,
        ]
        XCTAssertNoThrow(
            try SchemaValidator.validate(instance: asResult, schemaRef: jobsGetResultSchemaRef)
        )
    }

    func testCanonicalOverNestedFixtureRejectsBothSchemas() throws {
        let fixture = try ContractJSON.loadFixtureJSONObject(
            named: "plugin_job_output_over_nested.json",
            valid: false
        )
        XCTAssertThrowsError(
            try SchemaValidator.validate(instance: fixture, schemaRef: jobsGetResultSchemaRef)
        ) { error in
            XCTAssertTrue(error is SchemaValidationError)
        }
        XCTAssertThrowsError(
            try SchemaValidator.validate(instance: fixture, schemaRef: jobsCompleteParamsSchemaRef)
        ) { error in
            XCTAssertTrue(error is SchemaValidationError)
        }
    }

    func testOutputOptionalOnJobsGetResult() throws {
        // A completed job without `output` is still valid; the field
        // is optional. jobs.get.result only requires job_id + state.
        let document: [String: Any] = [
            "job_id": "550e8400-e29b-41d4-a716-446655440099",
            "state": "completed",
        ]
        XCTAssertNoThrow(
            try SchemaValidator.validate(instance: document, schemaRef: jobsGetResultSchemaRef)
        )
    }
}

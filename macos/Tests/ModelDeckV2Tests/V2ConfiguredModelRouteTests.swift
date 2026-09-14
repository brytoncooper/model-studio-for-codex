import Foundation
import XCTest
@testable import ModelDeckV2

final class V2ConfiguredModelRouteTests: XCTestCase {
    func testLoadsOnlyNonSecretConnectionAndModelReferences() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        addTeardownBlock { try? FileManager.default.removeItem(at: directory) }

        let profile = directory.appendingPathComponent("provider.json")
        try Data(
            """
            {
              "provider_id": "com.modeldeck.provider.fixture",
              "connection_id": "550e8400-e29b-41d4-a716-446655440000",
              "provider_model_id": "fixture/model",
              "display_name": "Fixture Model",
              "endpoint_config_ref": "ref:fixture.endpoint",
              "credential_ref": "ref:fixture.credential"
            }
            """.utf8
        ).write(to: profile)

        let route = try V2ConfiguredModelRoute.load(from: profile)

        XCTAssertEqual(route.connectionID, "550e8400-e29b-41d4-a716-446655440000")
        XCTAssertEqual(route.providerID, "com.modeldeck.provider.fixture")
        XCTAssertEqual(route.providerModelID, "fixture/model")
        XCTAssertEqual(route.displayName, "Fixture Model")
        XCTAssertEqual(route.endpointConfigRef, "ref:fixture.endpoint")
        XCTAssertEqual(route.credentialRef, "ref:fixture.credential")
    }

    func testRejectsLiteralSecretFields() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        addTeardownBlock { try? FileManager.default.removeItem(at: directory) }

        let profile = directory.appendingPathComponent("provider.json")
        try Data(
            """
            {
              "provider_id": "com.modeldeck.provider.fixture",
              "connection_id": "550e8400-e29b-41d4-a716-446655440000",
              "provider_model_id": "fixture/model",
              "display_name": "Fixture Model",
              "endpoint_config_ref": "ref:fixture.endpoint",
              "credential_ref": "ref:fixture.credential",
              "api_key": "must-not-be-read"
            }
            """.utf8
        ).write(to: profile)

        XCTAssertThrowsError(try V2ConfiguredModelRoute.load(from: profile))
    }
}

import Foundation
import XCTest
@testable import ModelDeckV2

private final class RecordingProviderCredentialSaver: V2ProviderCredentialSaving {
    private(set) var savedCredential: String?
    private(set) var savedAccountID: UUID?
    private(set) var deletedAccountID: UUID?

    func saveCredential(_ credential: String, accountID: UUID) throws {
        savedCredential = credential
        savedAccountID = accountID
    }

    func deleteCredential(accountID: UUID) throws {
        deletedAccountID = accountID
    }
}

final class V2ProviderSetupTests: XCTestCase {
    func testSavesCredentialOutsideNonSecretProviderProfile() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        addTeardownBlock { try? FileManager.default.removeItem(at: directory) }
        let profileURL = directory.appendingPathComponent("provider.json")
        let helperURL = URL(fileURLWithPath: "/Applications/Model Deck V2.app/Contents/Helpers/OpenRouterCredentialHelper")
        let saver = RecordingProviderCredentialSaver()
        let setup = V2ProviderSetupService(
            profileURL: profileURL,
            credentialHelperURL: helperURL,
            credentialSaver: saver
        )

        let result = try setup.saveOpenRouterConnection(
            apiKey: "secret-test-key",
            providerModelID: "deepseek/deepseek-v4.1-flash",
            displayName: "DeepSeek Flash"
        )

        XCTAssertEqual(saver.savedCredential, "secret-test-key")
        XCTAssertEqual(saver.savedAccountID, result.credentialAccountID)
        let profileData = try Data(contentsOf: profileURL)
        let profileText = try XCTUnwrap(String(data: profileData, encoding: .utf8))
        XCTAssertFalse(profileText.contains("secret-test-key"))
        let profile = try JSONSerialization.jsonObject(with: profileData) as? [String: Any]
        XCTAssertEqual(profile?["provider_id"] as? String, "com.modeldeck.openrouter")
        XCTAssertEqual(profile?["provider_model_id"] as? String, "deepseek/deepseek-v4.1-flash")
        let command = profile?["credential_command"] as? [String: Any]
        XCTAssertEqual(command?["executable"] as? String, helperURL.path)
        XCTAssertEqual(command?["args"] as? [String], ["--token", result.credentialAccountID.uuidString.lowercased()])
        let attributes = try FileManager.default.attributesOfItem(atPath: profileURL.path)
        XCTAssertEqual((attributes[.posixPermissions] as? NSNumber)?.intValue, 0o600)
    }

    func testRejectsMissingKeyOrModelBeforeCallingCredentialSaver() throws {
        let saver = RecordingProviderCredentialSaver()
        let setup = V2ProviderSetupService(
            profileURL: URL(fileURLWithPath: "/tmp/provider.json"),
            credentialHelperURL: URL(fileURLWithPath: "/tmp/helper"),
            credentialSaver: saver
        )

        XCTAssertThrowsError(
            try setup.saveOpenRouterConnection(apiKey: "", providerModelID: "model", displayName: "")
        )
        XCTAssertThrowsError(
            try setup.saveOpenRouterConnection(apiKey: "key", providerModelID: "", displayName: "")
        )
        XCTAssertNil(saver.savedCredential)
    }

    func testRepeatedSaveReusesCredentialAccountAndConnection() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        addTeardownBlock { try? FileManager.default.removeItem(at: directory) }
        let saver = RecordingProviderCredentialSaver()
        let setup = V2ProviderSetupService(
            profileURL: directory.appendingPathComponent("provider.json"),
            credentialHelperURL: URL(fileURLWithPath: "/tmp/helper"),
            credentialSaver: saver
        )
        let first = try setup.saveOpenRouterConnection(
            apiKey: "first-key",
            providerModelID: "provider/first",
            displayName: "First"
        )
        let second = try setup.saveOpenRouterConnection(
            apiKey: "second-key",
            providerModelID: "provider/second",
            displayName: "Second"
        )

        XCTAssertEqual(second.credentialAccountID, first.credentialAccountID)
        XCTAssertEqual(second.connectionID, first.connectionID)
        XCTAssertNil(saver.deletedAccountID)
    }

    func testProfileWriteFailureDeletesNewCredential() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        addTeardownBlock { try? FileManager.default.removeItem(at: directory) }
        let blockingFile = directory.appendingPathComponent("not-a-directory")
        XCTAssertTrue(FileManager.default.createFile(atPath: blockingFile.path, contents: Data()))
        let saver = RecordingProviderCredentialSaver()
        let setup = V2ProviderSetupService(
            profileURL: blockingFile.appendingPathComponent("provider.json"),
            credentialHelperURL: URL(fileURLWithPath: "/tmp/helper"),
            credentialSaver: saver
        )

        XCTAssertThrowsError(
            try setup.saveOpenRouterConnection(
                apiKey: "disposable-key",
                providerModelID: "provider/model",
                displayName: "Model"
            )
        )
        XCTAssertEqual(saver.deletedAccountID, saver.savedAccountID)
    }

    func testCredentialHelperUnavailableIsActionable() throws {
        let helper = V2CredentialHelperClient(
            executableURL: URL(fileURLWithPath: "/missing/model-deck-helper")
        )

        XCTAssertThrowsError(
            try helper.saveCredential("key", accountID: UUID())
        ) { error in
            XCTAssertEqual(
                String(describing: error),
                String(describing: V2ProviderSetupError.credentialHelperUnavailable)
            )
        }
    }
}

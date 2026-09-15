import CryptoKit
import Foundation

enum V2ProviderSetupError: Error, CustomStringConvertible {
    case missingAPIKey
    case missingModel
    case credentialHelperUnavailable
    case credentialSaveFailed
    case profileWriteFailed

    var description: String {
        switch self {
        case .missingAPIKey:
            return "Enter an OpenRouter API key."
        case .missingModel:
            return "Enter an OpenRouter model ID."
        case .credentialHelperUnavailable:
            return "The V2 credential helper is unavailable. Rebuild the isolated V2 app."
        case .credentialSaveFailed:
            return "The API key could not be saved in Keychain. Approve the Keychain prompt and try again."
        case .profileWriteFailed:
            return "The non-secret provider connection could not be saved. Check the V2 state folder and try again."
        }
    }
}

protocol V2ProviderCredentialSaving {
    func saveCredential(_ credential: String, accountID: UUID) throws
    func deleteCredential(accountID: UUID) throws
}

struct V2ProviderSetupResult: Equatable, Sendable {
    let profileURL: URL
    let credentialAccountID: UUID
    let connectionID: UUID
}

struct V2CredentialHelperClient: V2ProviderCredentialSaving {
    let executableURL: URL

    func saveCredential(_ credential: String, accountID: UUID) throws {
        guard FileManager.default.isExecutableFile(atPath: executableURL.path) else {
            throw V2ProviderSetupError.credentialHelperUnavailable
        }
        let process = Process()
        let input = Pipe()
        process.executableURL = executableURL
        process.arguments = ["--save", accountID.uuidString.lowercased()]
        process.standardInput = input
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
            input.fileHandleForWriting.write(Data(credential.utf8))
            try input.fileHandleForWriting.close()
            process.waitUntilExit()
        } catch {
            throw V2ProviderSetupError.credentialSaveFailed
        }
        guard process.terminationStatus == 0 else {
            throw V2ProviderSetupError.credentialSaveFailed
        }
    }

    func deleteCredential(accountID: UUID) throws {
        guard FileManager.default.isExecutableFile(atPath: executableURL.path) else {
            throw V2ProviderSetupError.credentialHelperUnavailable
        }
        let process = Process()
        process.executableURL = executableURL
        process.arguments = ["--delete", accountID.uuidString.lowercased()]
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            throw V2ProviderSetupError.credentialSaveFailed
        }
        guard process.terminationStatus == 0 else {
            throw V2ProviderSetupError.credentialSaveFailed
        }
    }
}

struct V2ProviderSetupService {
    private let profileURL: URL
    private let credentialHelperURL: URL
    private let credentialSaver: V2ProviderCredentialSaving

    init(
        profileURL: URL,
        credentialHelperURL: URL,
        credentialSaver: V2ProviderCredentialSaving? = nil
    ) {
        self.profileURL = profileURL
        self.credentialHelperURL = credentialHelperURL
        self.credentialSaver = credentialSaver
            ?? V2CredentialHelperClient(executableURL: credentialHelperURL)
    }

    func saveOpenRouterConnection(
        apiKey: String,
        providerModelID: String,
        displayName: String
    ) throws -> V2ProviderSetupResult {
        let trimmedKey = apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedModel = providerModelID.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedName = displayName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedKey.isEmpty else { throw V2ProviderSetupError.missingAPIKey }
        guard !trimmedModel.isEmpty else { throw V2ProviderSetupError.missingModel }

        let existingIdentity = loadExistingIdentity()
        let accountID = existingIdentity?.accountID ?? UUID()
        let connectionID = existingIdentity?.connectionID ?? UUID()
        try credentialSaver.saveCredential(trimmedKey, accountID: accountID)

        let identity = "https://openrouter.ai/api/v1\n\(trimmedModel)\n\(accountID.uuidString.lowercased())"
        let document: [String: Any] = [
            "schema_version": 1,
            "provider_id": "com.modeldeck.openrouter",
            "provider_name": "OpenRouter",
            "connection_id": connectionID.uuidString.lowercased(),
            "provider_model_id": trimmedModel,
            "display_name": trimmedName.isEmpty ? trimmedModel : trimmedName,
            "endpoint_config_ref": opaqueReference(kind: "endpoint", identity: identity),
            "credential_ref": opaqueReference(kind: "credential", identity: identity),
            "capability_snapshot_ref": "ref:v2.openrouter.serial-tools",
            "endpoint": [
                "base_url": "https://openrouter.ai/api/v1",
                "wire_mode": "auto",
                "vendor_id": "openrouter",
            ],
            "credential_command": [
                "executable": credentialHelperURL.path,
                "args": ["--token", accountID.uuidString.lowercased()],
                "timeout_ms": 30_000,
            ],
            "billing_description": "OpenRouter API usage consumes OpenRouter credits; it is not ChatGPT subscription usage.",
        ]
        do {
            try writePrivateJSON(document)
        } catch {
            if existingIdentity == nil {
                try? credentialSaver.deleteCredential(accountID: accountID)
            }
            throw V2ProviderSetupError.profileWriteFailed
        }
        return V2ProviderSetupResult(
            profileURL: profileURL,
            credentialAccountID: accountID,
            connectionID: connectionID
        )
    }

    private func loadExistingIdentity() -> (accountID: UUID, connectionID: UUID)? {
        guard let data = try? Data(contentsOf: profileURL),
              let document = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              document["provider_id"] as? String == "com.modeldeck.openrouter",
              let connectionValue = document["connection_id"] as? String,
              let connectionID = UUID(uuidString: connectionValue),
              let command = document["credential_command"] as? [String: Any],
              command["executable"] as? String == credentialHelperURL.path,
              let arguments = command["args"] as? [String],
              arguments.count == 2,
              arguments[0] == "--token",
              let accountID = UUID(uuidString: arguments[1]) else {
            return nil
        }
        return (accountID, connectionID)
    }

    private func opaqueReference(kind: String, identity: String) -> String {
        let digest = SHA256.hash(data: Data(identity.utf8))
            .map { String(format: "%02x", $0) }
            .joined()
        return "ref:v2.\(kind).\(digest.prefix(20))"
    }

    private func writePrivateJSON(_ document: [String: Any]) throws {
        let fileManager = FileManager.default
        let parent = profileURL.deletingLastPathComponent()
        try fileManager.createDirectory(
            at: parent,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        let temporaryURL = parent.appendingPathComponent(".\(profileURL.lastPathComponent).\(UUID().uuidString).tmp")
        let data = try JSONSerialization.data(
            withJSONObject: document,
            options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        ) + Data([0x0A])
        guard fileManager.createFile(
            atPath: temporaryURL.path,
            contents: data,
            attributes: [.posixPermissions: 0o600]
        ) else {
            throw V2ProviderSetupError.profileWriteFailed
        }
        do {
            let temporaryHandle = try FileHandle(forWritingTo: temporaryURL)
            try temporaryHandle.synchronize()
            try temporaryHandle.close()
            if fileManager.fileExists(atPath: profileURL.path) {
                _ = try fileManager.replaceItemAt(profileURL, withItemAt: temporaryURL)
            } else {
                try fileManager.moveItem(at: temporaryURL, to: profileURL)
            }
            try fileManager.setAttributes([.posixPermissions: 0o600], ofItemAtPath: profileURL.path)
        } catch {
            try? fileManager.removeItem(at: temporaryURL)
            throw error
        }
    }
}

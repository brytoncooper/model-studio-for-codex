import Foundation
import Security
import Darwin

enum CredentialError: Error { case unavailable }

enum Credentials {
    static func query(_ account: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: "com.cooper.codex-openrouter.keys",
         kSecAttrAccount as String: account]
    }

    static func save(_ value: Data, account: String) throws {
        guard !value.isEmpty, value.count <= 8192,
              String(data: value, encoding: .utf8) != nil else { throw CredentialError.unavailable }
        let status = SecItemUpdate(query(account) as CFDictionary,
                                   [kSecValueData as String: value] as CFDictionary)
        if status == errSecItemNotFound {
            var item = query(account)
            item[kSecValueData as String] = value
            item[kSecAttrLabel as String] = "Codex OpenRouter API key"
            item[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
            guard SecItemAdd(item as CFDictionary, nil) == errSecSuccess else { throw CredentialError.unavailable }
        } else if status != errSecSuccess {
            throw CredentialError.unavailable
        }
    }

    static func read(_ account: String) throws -> Data {
        var item = query(account)
        item[kSecReturnData as String] = true
        item[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        guard SecItemCopyMatching(item as CFDictionary, &result) == errSecSuccess,
              let value = result as? Data, !value.isEmpty, value.count <= 8192,
              String(data: value, encoding: .utf8) != nil else { throw CredentialError.unavailable }
        return value
    }

    static func selfTest() throws {
        let account = UUID().uuidString
        let value = Data(("disposable-self-test-" + UUID().uuidString).utf8)
        defer { SecItemDelete(query(account) as CFDictionary) }
        try save(value, account: account)
        guard try read(account) == value else { throw CredentialError.unavailable }
        let process = Process()
        let output = Pipe()
        let errors = Pipe()
        let finished = DispatchSemaphore(value: 0)
        process.executableURL = URL(fileURLWithPath: CommandLine.arguments[0])
        process.arguments = ["--token", account]
        process.standardOutput = output
        process.standardError = errors
        process.standardInput = FileHandle.nullDevice
        process.terminationHandler = { _ in finished.signal() }
        try process.run()
        if finished.wait(timeout: .now() + 5) == .timedOut {
            process.terminate()
            if finished.wait(timeout: .now() + 0.5) == .timedOut {
                Darwin.kill(process.processIdentifier, SIGKILL)
            }
            throw CredentialError.unavailable
        }
        guard process.terminationStatus == 0,
              output.fileHandleForReading.readDataToEndOfFile() == value + Data([10]) else {
            throw CredentialError.unavailable
        }
        guard SecItemDelete(query(account) as CFDictionary) == errSecSuccess else {
            throw CredentialError.unavailable
        }
    }
}

// No unattended invocation may display a Keychain authorization dialog.
SecKeychainSetUserInteractionAllowed(false)
let arguments = CommandLine.arguments
do {
    if arguments.count == 2 && arguments[1] == "--self-test-keychain" {
        try Credentials.selfTest()
        print("PASS: stable helper Keychain save/read and noninteractive child retrieval; disposable credential removed.")
    } else {
        guard arguments.count == 3, UUID(uuidString: arguments[2]) != nil else {
            throw CredentialError.unavailable
        }
        let command = arguments[1]
        let account = arguments[2]
        switch command {
        case "--save":
            // This command is requested only by an explicit foreground Save action.
            SecKeychainSetUserInteractionAllowed(true)
            try Credentials.save(FileHandle.standardInput.readDataToEndOfFile(), account: account)
        case "--authorize-token":
            // Explicit foreground access permits the user to authorize this stable helper.
            SecKeychainSetUserInteractionAllowed(true)
            FileHandle.standardOutput.write(try Credentials.read(account) + Data([10]))
        case "--token":
            FileHandle.standardOutput.write(try Credentials.read(account) + Data([10]))
        default:
            throw CredentialError.unavailable
        }
    }
    exit(0)
} catch {
    FileHandle.standardError.write(Data("OpenRouter credential access failed. Authorize the saved key from OpenRouter Settings.\n".utf8))
    exit(1)
}

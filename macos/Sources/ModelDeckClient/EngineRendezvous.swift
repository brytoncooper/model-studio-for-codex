import Foundation
import CoreFoundation

public struct EngineAPIProfile: Sendable, Equatable {
    public let major: Int
    public let minor: Int

    public init(major: Int, minor: Int) {
        self.major = major
        self.minor = minor
    }

    static func parse(_ value: Any) throws -> EngineAPIProfile {
        guard let dictionary = value as? [String: Any] else {
            throw EngineClientError.invalidRendezvous("api_profile required")
        }
        let major = try parseStrictInteger(dictionary["major"], field: "major")
        let minor = try parseStrictInteger(dictionary["minor"], field: "minor")
        return try validate(major: major, minor: minor)
    }

    private static func parseStrictInteger(_ value: Any?, field: String) throws -> Int {
        guard let value else {
            throw EngineClientError.invalidRendezvous("api_profile must include major and minor")
        }
        if let number = value as? NSNumber {
            if CFGetTypeID(number) == CFBooleanGetTypeID() {
                throw EngineClientError.invalidRendezvous("api_profile \(field) must be an integer")
            }
            let doubleValue = number.doubleValue
            if !doubleValue.isFinite {
                throw EngineClientError.invalidRendezvous("api_profile \(field) must be an integer")
            }
            if doubleValue.rounded(.towardZero) != doubleValue {
                throw EngineClientError.invalidRendezvous("api_profile \(field) must be an integer")
            }
            if doubleValue > Double(Int.max) || doubleValue < Double(Int.min) {
                throw EngineClientError.invalidRendezvous("api_profile \(field) must be an integer")
            }
            return Int(doubleValue)
        }
        if value is Bool {
            throw EngineClientError.invalidRendezvous("api_profile \(field) must be an integer")
        }
        if let intValue = value as? Int {
            return intValue
        }
        throw EngineClientError.invalidRendezvous("api_profile must include major and minor")
    }

    private static func validate(major: Int, minor: Int) throws -> EngineAPIProfile {
        if major < 1 {
            throw EngineClientError.invalidRendezvous("api_profile major must be at least 1")
        }
        if minor < 0 {
            throw EngineClientError.invalidRendezvous("api_profile minor must be non-negative")
        }
        let serverMajor = 1
        let serverMinor = 0
        if major != serverMajor {
            throw EngineClientError.invalidRendezvous("api_profile incompatible with engine 1.0")
        }
        if serverMinor < minor {
            throw EngineClientError.invalidRendezvous("api_profile incompatible with engine 1.0")
        }
        return EngineAPIProfile(major: major, minor: minor)
    }
}

public struct EngineRendezvousExpectation: Sendable, Equatable {
    public let engineInstanceID: String
    public let instanceNonce: String
    public let apiProfile: EngineAPIProfile

    public init(engineInstanceID: String, instanceNonce: String, apiProfile: EngineAPIProfile) {
        self.engineInstanceID = engineInstanceID
        self.instanceNonce = instanceNonce
        self.apiProfile = apiProfile
    }

    public func matches(engineInstanceID observedID: String, instanceNonce observedNonce: String) -> Bool {
        engineInstanceID == observedID && instanceNonce == observedNonce
    }
}

public struct EngineRendezvousDescriptor: Sendable, Equatable {
    private static let allowedKeys: Set<String> = [
        "transport",
        "socket_path",
        "engine_instance_id",
        "instance_nonce",
        "api_profile",
    ]

    public let transport: String
    public let socketPath: String
    public let engineInstanceID: String
    public let instanceNonce: String
    public let apiProfile: EngineAPIProfile

    public init(
        transport: String,
        socketPath: String,
        engineInstanceID: String,
        instanceNonce: String,
        apiProfile: EngineAPIProfile
    ) {
        self.transport = transport
        self.socketPath = socketPath
        self.engineInstanceID = engineInstanceID
        self.instanceNonce = instanceNonce
        self.apiProfile = apiProfile
    }

    public var expectation: EngineRendezvousExpectation {
        EngineRendezvousExpectation(
            engineInstanceID: engineInstanceID,
            instanceNonce: instanceNonce,
            apiProfile: apiProfile
        )
    }

    public static func load(from url: URL) throws -> EngineRendezvousDescriptor {
        let data = try Data(contentsOf: url)
        let object = try JSONSerialization.jsonObject(with: data)
        guard let dictionary = object as? [String: Any] else {
            throw EngineClientError.invalidRendezvous("rendezvous root must be an object")
        }
        return try parse(dictionary)
    }

    public static func parse(_ dictionary: [String: Any]) throws -> EngineRendezvousDescriptor {
        let extra = Set(dictionary.keys).subtracting(allowedKeys)
        if !extra.isEmpty {
            throw EngineClientError.invalidRendezvous("unexpected rendezvous fields: \(Array(extra).sorted())")
        }
        guard let transport = dictionary["transport"] as? String, transport == "unix" else {
            throw EngineClientError.invalidRendezvous("transport must be unix")
        }
        guard let socketPath = dictionary["socket_path"] as? String, !socketPath.isEmpty else {
            throw EngineClientError.invalidRendezvous("socket_path must be a non-empty string")
        }
        if !socketPath.hasPrefix("/") {
            throw EngineClientError.invalidRendezvous("socket_path must be absolute")
        }
        guard let engineInstanceID = dictionary["engine_instance_id"] as? String, !engineInstanceID.isEmpty else {
            throw EngineClientError.invalidRendezvous("engine_instance_id required")
        }
        guard let instanceNonce = dictionary["instance_nonce"] as? String, !instanceNonce.isEmpty else {
            throw EngineClientError.invalidRendezvous("instance_nonce required")
        }
        guard let apiRaw = dictionary["api_profile"] else {
            throw EngineClientError.invalidRendezvous("api_profile required")
        }
        let apiProfile = try EngineAPIProfile.parse(apiRaw)
        return EngineRendezvousDescriptor(
            transport: transport,
            socketPath: socketPath,
            engineInstanceID: engineInstanceID,
            instanceNonce: instanceNonce,
            apiProfile: apiProfile
        )
    }

    public static func defaultOperatorCredentialURL(rendezvousFile: URL) -> URL {
        rendezvousFile.deletingLastPathComponent().appendingPathComponent("operator_credential")
    }
}

public protocol EngineInstanceCredentialProviding: Sendable {
    func readCredential() throws -> String
}

public struct EngineFileCredentialProvider: EngineInstanceCredentialProviding {
    private let credentialURL: URL

    public init(credentialURL: URL) {
        self.credentialURL = credentialURL
    }

    public func readCredential() throws -> String {
        let data = try Data(contentsOf: credentialURL)
        let value = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else { throw EngineClientError.missingCredential }
        return value
    }
}

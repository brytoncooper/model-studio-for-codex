import Foundation

enum V2ConfiguredModelRouteError: Error {
    case invalidProfile
    case literalSecretField
}

struct V2ConfiguredModelRoute: Equatable, Sendable {
    let connectionID: String
    let providerID: String
    let providerModelID: String
    let displayName: String
    let endpointConfigRef: String
    let credentialRef: String

    static func load(from url: URL) throws -> V2ConfiguredModelRoute {
        guard !url.hasDirectoryPath,
              let document = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any]
        else {
            throw V2ConfiguredModelRouteError.invalidProfile
        }
        let forbiddenFields = [
            "api_key", "apikey", "api_token", "token", "secret",
            "authorization", "bearer", "password",
        ]
        guard document.keys.allSatisfy({ !forbiddenFields.contains($0.lowercased()) }) else {
            throw V2ConfiguredModelRouteError.literalSecretField
        }
        guard let connectionID = document["connection_id"] as? String,
              UUID(uuidString: connectionID)?.uuidString.lowercased() == connectionID.lowercased(),
              let providerID = document["provider_id"] as? String,
              !providerID.isEmpty,
              let providerModelID = document["provider_model_id"] as? String,
              !providerModelID.isEmpty,
              let displayName = document["display_name"] as? String,
              let endpointConfigRef = document["endpoint_config_ref"] as? String,
              !endpointConfigRef.isEmpty,
              let credentialRef = document["credential_ref"] as? String,
              !credentialRef.isEmpty
        else {
            throw V2ConfiguredModelRouteError.invalidProfile
        }
        return V2ConfiguredModelRoute(
            connectionID: connectionID,
            providerID: providerID,
            providerModelID: providerModelID,
            displayName: displayName,
            endpointConfigRef: endpointConfigRef,
            credentialRef: credentialRef
        )
    }
}

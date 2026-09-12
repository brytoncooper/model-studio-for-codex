import Foundation

public struct CatalogListItem: Equatable, Sendable {
    public let providerModelID: String
    public let displayName: String
    public let kind: String
    public let connectionID: String
    public let catalogRevision: String?

    public init(
        providerModelID: String,
        displayName: String,
        kind: String,
        connectionID: String,
        catalogRevision: String? = nil
    ) {
        self.providerModelID = providerModelID
        self.displayName = displayName
        self.kind = kind
        self.connectionID = connectionID
        self.catalogRevision = catalogRevision
    }

    public static func legacyCatalog(
        providerModelID: String,
        displayName: String,
        connectionID: String
    ) -> CatalogListItem {
        CatalogListItem(
            providerModelID: providerModelID,
            displayName: displayName,
            kind: "catalog",
            connectionID: connectionID
        )
    }


    public init(payload: [String: Any]) throws {
        guard let kind = payload["kind"] as? String else {
            throw EngineClientError.protocolError("model list item missing kind")
        }
        guard let providerModelID = payload["provider_model_id"] as? String, !providerModelID.isEmpty else {
            throw EngineClientError.protocolError("model list item missing provider_model_id")
        }
        let displayName = payload["display_name"] as? String ?? providerModelID
        let connectionID = payload["connection_id"] as? String ?? ""
        let catalogRevision = payload["catalog_revision"] as? String
        self.init(
            providerModelID: providerModelID,
            displayName: displayName,
            kind: kind,
            connectionID: connectionID,
            catalogRevision: catalogRevision
        )
    }

    static func parseEngineCatalogItem(
        _ payload: [String: Any],
        expectedConnectionID: String
    ) throws -> CatalogListItem {
        guard let kind = payload["kind"] as? String else {
            throw EngineClientError.protocolError("model list item missing kind")
        }
        guard kind == "catalog" else {
            throw EngineClientError.protocolError("expected catalog model list item")
        }
        guard let providerModelID = payload["provider_model_id"] as? String, !providerModelID.isEmpty else {
            throw EngineClientError.protocolError("model list item missing provider_model_id")
        }
        guard let connectionID = payload["connection_id"] as? String, !connectionID.isEmpty else {
            throw EngineClientError.protocolError("model list item missing connection_id")
        }
        guard connectionID == expectedConnectionID else {
            throw EngineClientError.protocolError("model list item connection_id mismatch")
        }
        let displayName = payload["display_name"] as? String ?? providerModelID
        let catalogRevision = payload["catalog_revision"] as? String
        return CatalogListItem(
            providerModelID: providerModelID,
            displayName: displayName,
            kind: kind,
            connectionID: connectionID,
            catalogRevision: catalogRevision
        )
    }
}

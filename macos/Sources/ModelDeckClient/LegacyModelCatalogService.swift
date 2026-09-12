import Foundation

public typealias LegacyModelCatalogRequestHandler = (
    _ request: [String: Any],
    _ completion: @escaping ([String: Any]) -> Void
) -> Void

public final class LegacyModelCatalogService: ModelCatalogServing {
    private let requestHandler: LegacyModelCatalogRequestHandler
    private let lock = NSLock()
    private var active = false

    public init(requestHandler: @escaping LegacyModelCatalogRequestHandler) {
        self.requestHandler = requestHandler
    }

    public func cancel() {
        lock.lock()
        active = false
        lock.unlock()
    }

    public func loadCatalog(
        connection: ModelCatalogConnectionRequest,
        completion: @escaping (Result<ModelCatalogPayload, ModelCatalogServiceError>) -> Void
    ) {
        lock.lock()
        active = true
        lock.unlock()
        var request: [String: Any] = [
            "action": connection.isCursor ? "cursor_models" : "endpoint_models",
            "account": connection.accountID,
            "base_url": connection.baseURL,
            "wire": connection.wire,
            "has_key": connection.hasKey,
            "executable": connection.executablePath,
        ]
        if connection.hasKey { request["account"] = connection.accountID }
        requestHandler(request) { result in
            self.lock.lock()
            let stillActive = self.active
            self.lock.unlock()
            guard stillActive else {
                completion(.failure(.cancelled))
                return
            }
            let suggested = result["source"] as? String == "suggested"
            let ok = result["ok"] as? Bool == true
            let rawModels = result["models"] as? [[String: Any]] ?? []
            let items: [CatalogListItem] = rawModels.compactMap { value in
                guard let id = value["id"] as? String else { return nil }
                let name = value["name"] as? String ?? value["display_name"] as? String ?? id
                return CatalogListItem.legacyCatalog(
                    providerModelID: id,
                    displayName: name,
                    connectionID: connection.accountID
                )
            }
            if ok {
                let note = result["note"] as? String ?? ""
                let message = suggested
                    ? "Suggested IDs · Availability and authentication are unverified. " + note
                    : "\(items.count) models from \(connection.accountName). " + note
                completion(.success(ModelCatalogPayload(
                    models: items,
                    suggested: suggested,
                    statusMessage: message.trimmingCharacters(in: .whitespaces),
                    unavailable: suggested
                )))
            } else {
                let message = result["error"] as? String
                    ?? result["message"] as? String
                    ?? "Could not load this connection's models. Retry or use an exact model ID."
                completion(.failure(.message(message)))
            }
        }
    }
}

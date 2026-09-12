import Foundation

public protocol ModelCatalogServing: AnyObject {
    func loadCatalog(
        connection: ModelCatalogConnectionRequest,
        completion: @escaping (Result<ModelCatalogPayload, ModelCatalogServiceError>) -> Void
    )
    func cancel()
}

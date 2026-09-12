import Foundation
import ModelDeckClient

public enum ModelCatalogPhase: Equatable, Sendable {
    case idle
    case loading
    case ready
    case empty
    case failure(String)
    case unavailable(String)
}

public struct ModelCatalogPresenterOutcome: Sendable {
    public let phase: ModelCatalogPhase
    public let applied: Bool
    public let statusMessage: String
    public let suggested: Bool
    public let models: [CatalogModel]?

    public init(
        phase: ModelCatalogPhase,
        applied: Bool,
        statusMessage: String,
        suggested: Bool,
        models: [CatalogModel]? = nil
    ) {
        self.phase = phase
        self.applied = applied
        self.statusMessage = statusMessage
        self.suggested = suggested
        self.models = models
    }
}

@MainActor
public final class ModelCatalogPresenter {
    public private(set) var phase: ModelCatalogPhase = .idle
    private var loadToken = UUID()
    private var activeService: ModelCatalogServing?

    public init() {}

    public func cancel() {
        loadToken = UUID()
        activeService?.cancel()
        activeService = nil
        phase = .idle
    }

    public func load(
        connection: ModelCatalogConnectionRequest,
        service: ModelCatalogServing,
        completion: @escaping (ModelCatalogPresenterOutcome) -> Void
    ) {
        cancel()
        let token = UUID()
        loadToken = token
        activeService = service
        phase = .loading
        service.loadCatalog(connection: connection) { result in
            Task { @MainActor in
                guard self.loadToken == token else {
                    completion(ModelCatalogPresenterOutcome(
                        phase: self.phase,
                        applied: false,
                        statusMessage: "",
                        suggested: false
                    ))
                    return
                }
                self.activeService = nil
                switch result {
                case .success(let payload):
                    let models = payload.models.map { $0.asPresentationModel(suggested: payload.suggested) }
                    let phase: ModelCatalogPhase
                    if payload.unavailable {
                        phase = .unavailable(payload.statusMessage)
                    } else if models.isEmpty {
                        phase = .empty
                    } else {
                        phase = .ready
                    }
                    self.phase = phase
                    completion(ModelCatalogPresenterOutcome(
                        phase: phase,
                        applied: true,
                        statusMessage: payload.statusMessage,
                        suggested: payload.suggested,
                        models: models
                    ))
                case .failure(let error):
                    if error == .cancelled {
                        self.phase = .idle
                        completion(ModelCatalogPresenterOutcome(
                            phase: .idle,
                            applied: false,
                            statusMessage: "",
                            suggested: false
                        ))
                        return
                    }
                    let message = error.description
                    self.phase = .failure(message)
                    completion(ModelCatalogPresenterOutcome(
                        phase: .failure(message),
                        applied: true,
                        statusMessage: message,
                        suggested: false
                    ))
                }
            }
        }
    }
}

private extension CatalogListItem {
    func asPresentationModel(suggested: Bool) -> CatalogModel {
        CatalogModel(id: providerModelID, name: displayName, suggested: suggested)
    }
}

import Foundation

public enum ModelCatalogBootstrap {
    public static let rendezvousEnvironmentKey = "MODEL_DECK_ENGINE_RENDEZVOUS"
    public static let credentialEnvironmentKey = "MODEL_DECK_ENGINE_CREDENTIAL"

    public static func makeService(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        legacyRequestHandler: @escaping LegacyModelCatalogRequestHandler,
        engineTransportFactory: ((EngineRendezvousDescriptor) throws -> EngineTransport)? = nil
    ) -> ModelCatalogServing {
        guard let rendezvousPath = environment[rendezvousEnvironmentKey], !rendezvousPath.isEmpty else {
            return LegacyModelCatalogService(requestHandler: legacyRequestHandler)
        }
        do {
            let rendezvousURL = URL(fileURLWithPath: rendezvousPath)
            let descriptor = try EngineRendezvousDescriptor.load(from: rendezvousURL)
            guard descriptor.transport == "unix" else {
                return UnavailableModelCatalogService(message: "Unsupported engine transport.")
            }
            guard let engineTransportFactory else {
                return UnavailableModelCatalogService(
                    message: "Engine rendezvous is configured but no transport factory was supplied."
                )
            }
            let credentialURL: URL
            if let override = environment[credentialEnvironmentKey], !override.isEmpty {
                credentialURL = URL(fileURLWithPath: override)
            } else {
                credentialURL = EngineRendezvousDescriptor.defaultOperatorCredentialURL(rendezvousFile: rendezvousURL)
            }
            let credentials = EngineFileCredentialProvider(credentialURL: credentialURL)
            return EngineModelCatalogService(
                rendezvous: descriptor,
                makeTransport: { try engineTransportFactory(descriptor) },
                credentialProvider: credentials
            )
        } catch {
            return UnavailableModelCatalogService(message: error.localizedDescription)
        }
    }
}

final class UnavailableModelCatalogService: ModelCatalogServing {
    private let message: String
    init(message: String) { self.message = message }
    func cancel() {}
    func loadCatalog(
        connection: ModelCatalogConnectionRequest,
        completion: @escaping (Result<ModelCatalogPayload, ModelCatalogServiceError>) -> Void
    ) {
        completion(.failure(.message(message)))
    }
}

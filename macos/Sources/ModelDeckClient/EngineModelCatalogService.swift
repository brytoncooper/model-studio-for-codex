import Foundation

public final class EngineModelCatalogService: ModelCatalogServing {
    private let makeTransport: () throws -> EngineTransport
    private let rendezvous: EngineRendezvousDescriptor
    private let credentialProvider: EngineInstanceCredentialProviding
    private let workerQueue = DispatchQueue(label: "modeldeck.engine.catalog", qos: .userInitiated)
    private let stateLock = NSLock()
    private var inFlightClient: ModelDeckEngineClient?
    private var activeRequestID: UUID?

    public init(
        rendezvous: EngineRendezvousDescriptor,
        makeTransport: @escaping () throws -> EngineTransport,
        credentialProvider: EngineInstanceCredentialProviding
    ) {
        self.rendezvous = rendezvous
        self.makeTransport = makeTransport
        self.credentialProvider = credentialProvider
    }

    public func cancel() {
        stateLock.lock()
        let client = inFlightClient
        activeRequestID = nil
        stateLock.unlock()
        client?.cancelInFlight()
    }

    public func loadCatalog(
        connection: ModelCatalogConnectionRequest,
        completion: @escaping (Result<ModelCatalogPayload, ModelCatalogServiceError>) -> Void
    ) {
        let requestID = UUID()
        stateLock.lock()
        activeRequestID = requestID
        stateLock.unlock()

        workerQueue.async {
            let finishLock = NSLock()
            var finished = false
            func finish(_ result: Result<ModelCatalogPayload, ModelCatalogServiceError>) {
                finishLock.lock()
                if finished {
                    finishLock.unlock()
                    return
                }
                finished = true
                finishLock.unlock()

                self.stateLock.lock()
                let isCurrent = self.activeRequestID == requestID
                if isCurrent {
                    self.activeRequestID = nil
                }
                self.stateLock.unlock()

                guard isCurrent else {
                    completion(.failure(.cancelled))
                    return
                }
                completion(result)
            }

            func isActive() -> Bool {
                self.stateLock.lock()
                let active = self.activeRequestID == requestID
                self.stateLock.unlock()
                return active
            }

            func clearInFlightIfOwned(_ client: ModelDeckEngineClient) {
                self.stateLock.lock()
                if self.inFlightClient === client {
                    self.inFlightClient = nil
                }
                self.stateLock.unlock()
            }

            func abandon(_ client: ModelDeckEngineClient) {
                client.cancelInFlight()
                clearInFlightIfOwned(client)
            }

            guard isActive() else {
                finish(.failure(.cancelled))
                return
            }

            var engine: ModelDeckEngineClient?
            do {
                guard isActive() else {
                    finish(.failure(.cancelled))
                    return
                }
                let transport = try self.makeTransport()
                let engineClient = ModelDeckEngineClient(
                    transport: transport,
                    credentialProvider: self.credentialProvider
                )
                engine = engineClient

                self.stateLock.lock()
                let adopted = self.activeRequestID == requestID
                if adopted {
                    self.inFlightClient = engineClient
                }
                self.stateLock.unlock()

                guard adopted else {
                    abandon(engineClient)
                    finish(.failure(.cancelled))
                    return
                }

                try engineClient.connectAndAuthenticate(descriptor: self.rendezvous)

                guard isActive() else {
                    abandon(engineClient)
                    finish(.failure(.cancelled))
                    return
                }

                let items = try engineClient.listCatalogModels(connectionID: connection.accountID)
                clearInFlightIfOwned(engineClient)

                guard isActive() else {
                    finish(.failure(.cancelled))
                    return
                }

                let message = items.isEmpty
                    ? "No cached catalog entries for \(connection.accountName)."
                    : "\(items.count) cached models from \(connection.accountName)."
                finish(.success(ModelCatalogPayload(
                    models: items,
                    suggested: false,
                    statusMessage: message,
                    unavailable: false
                )))
            } catch let error as EngineClientError {
                if let engine {
                    clearInFlightIfOwned(engine)
                }
                if isActive() {
                    finish(.failure(.message(error.description)))
                } else {
                    finish(.failure(.cancelled))
                }
            } catch {
                if let engine {
                    clearInFlightIfOwned(engine)
                }
                if isActive() {
                    finish(.failure(.message(error.localizedDescription)))
                } else {
                    finish(.failure(.cancelled))
                }
            }
        }
    }
}

import Foundation

/// Typed native client for `engine.v1.models.{list,register,rename,remove}`.
///
/// Wraps the frozen JSON-RPC surface exactly: no schema mutation, no implicit
/// `idempotency_key` synthesis outside the explicit helper, and
/// `expected_revision` passed through verbatim from the caller. Concurrent
/// calls serialize on a single shared `ModelDeckEngineClient`; the two-step
/// hello handshake runs once during ``connect()`` and each call is one
/// validated JSON-RPC round-trip.
///
/// The list surface always sends `collection: "registered"` — the catalog
/// branch is intentionally out of scope for V2 and is rejected by this
/// client. The caller is responsible for:
/// - generating a fresh `idempotency_key` per logical mutation (use
///   ``EngineModelRegistrationService/newIdempotencyKey()`` or supply their
///   own externally-coordinated key);
/// - tracking the current `revision` from ``listRegisteredModels()`` and
///   passing it as `expectedRevision` on the next mutation call;
/// - resolving the schema-validated `conflict` error from the engine by
///   re-listing and retrying — this client never auto-retries.
public final class EngineModelRegistrationService: @unchecked Sendable {
    /// Frozen RPC method for listing registered models.
    public static let listMethod = "engine.v1.models.list"
    /// Frozen RPC method for registering a new model against a connection.
    public static let registerMethod = "engine.v1.models.register"
    /// Frozen RPC method for renaming an existing registered model.
    public static let renameMethod = "engine.v1.models.rename"
    /// Frozen RPC method for removing a registered model.
    public static let removeMethod = "engine.v1.models.remove"

    private let rendezvous: EngineRendezvousDescriptor
    private let client: ModelDeckEngineClient
    private let workerQueue = DispatchQueue(label: "modeldeck.engine.models", qos: .userInitiated)
    private let lock = NSLock()

    /// Creates a service that authenticates via `rendezvous` and dials `transport`.
    public init(
        rendezvous: EngineRendezvousDescriptor,
        transport: EngineTransport,
        credentialProvider: EngineInstanceCredentialProviding
    ) {
        self.rendezvous = rendezvous
        self.client = ModelDeckEngineClient(transport: transport, credentialProvider: credentialProvider)
    }

    /// Runs the two-step hello authentication over `rendezvous`.
    public func connect() throws {
        try lock.withLock { try client.connectAndAuthenticate(descriptor: rendezvous) }
    }

    /// Calls `engine.v1.models.list` with `collection: "registered"` exactly
    /// once. `query`, `cursor`, and `limit` are forwarded when supplied; the
    /// caller passes the `next_cursor` returned in the previous page to page.
    public func listRegisteredModels(
        query: String? = nil,
        cursor: String? = nil,
        limit: Int? = nil
    ) async throws -> ModelListPage {
        let result: ModelListResult = try await perform(
            method: Self.listMethod,
            params: ModelListParams(
                collection: "registered",
                query: query,
                cursor: cursor,
                limit: limit
            ),
            paramsSchemaRef: "contracts/engine.v1/methods/models.list.params.schema.json",
            resultSchemaRef: "contracts/engine.v1/methods/models.list.result.schema.json"
        )
        return ModelListPage(
            collection: result.collection,
            items: result.items,
            nextCursor: result.nextCursor,
            cacheOnly: result.cacheOnly
        )
    }

    /// Calls `engine.v1.models.register` exactly once with the supplied
    /// `expectedRevision` and caller-supplied `idempotencyKey`. No retry on
    /// failure; the engine's schema-validated `conflict` error surfaces as
    /// ``EngineClientError/unavailable`` so the caller can re-list and decide.
    public func registerModel(
        connectionID: String,
        providerModelID: String,
        displayName: String,
        expectedRevision: Int,
        idempotencyKey: String
    ) async throws -> RegisteredModel {
        let result: ModelMutationResult = try await perform(
            method: Self.registerMethod,
            params: ModelRegisterParams(
                connectionID: connectionID,
                providerModelID: providerModelID,
                displayName: displayName,
                expectedRevision: expectedRevision,
                idempotencyKey: idempotencyKey
            ),
            paramsSchemaRef: "contracts/engine.v1/methods/models.register.params.schema.json",
            resultSchemaRef: "contracts/engine.v1/methods/models.register.result.schema.json"
        )
        return result.model
    }

    /// Calls `engine.v1.models.rename` exactly once with the supplied
    /// `expectedRevision` and caller-supplied `idempotencyKey`. Only
    /// `display_name` changes; `provider_model_id` and `connection_id` are
    /// intentionally absent from the params per the frozen schema.
    public func renameModel(
        registrationID: String,
        displayName: String,
        expectedRevision: Int,
        idempotencyKey: String
    ) async throws -> RegisteredModel {
        let result: ModelMutationResult = try await perform(
            method: Self.renameMethod,
            params: ModelRenameParams(
                registrationID: registrationID,
                displayName: displayName,
                expectedRevision: expectedRevision,
                idempotencyKey: idempotencyKey
            ),
            paramsSchemaRef: "contracts/engine.v1/methods/models.rename.params.schema.json",
            resultSchemaRef: "contracts/engine.v1/methods/models.rename.result.schema.json"
        )
        return result.model
    }

    /// Calls `engine.v1.models.remove` exactly once with the supplied
    /// `expectedRevision` and caller-supplied `idempotencyKey`. The boolean
    /// return mirrors the engine's schema-validated `removed` field.
    @discardableResult
    public func removeModel(
        registrationID: String,
        expectedRevision: Int,
        idempotencyKey: String
    ) async throws -> Bool {
        let result: ModelRemoveResult = try await perform(
            method: Self.removeMethod,
            params: ModelRemoveParams(
                registrationID: registrationID,
                expectedRevision: expectedRevision,
                idempotencyKey: idempotencyKey
            ),
            paramsSchemaRef: "contracts/engine.v1/methods/models.remove.params.schema.json",
            resultSchemaRef: "contracts/engine.v1/methods/models.remove.result.schema.json"
        )
        return result.removed
    }

    /// Generates a fresh lowercase UUIDv4 for use as an `idempotency_key`.
    public static func newIdempotencyKey() -> String {
        UUID().uuidString.lowercased()
    }

    private func perform<P: Encodable & Sendable, R: Decodable & Sendable>(
        method: String,
        params: P,
        paramsSchemaRef: String,
        resultSchemaRef: String
    ) async throws -> R {
        try await withCheckedThrowingContinuation { continuation in
            workerQueue.async {
                self.lock.withLock {
                    do {
                        let result: R = try self.client.invokeValidated(
                            method: method,
                            params: params,
                            paramsSchemaRef: paramsSchemaRef,
                            resultSchemaRef: resultSchemaRef
                        )
                        continuation.resume(returning: result)
                    } catch {
                        continuation.resume(throwing: error)
                    }
                }
            }
        }
    }
}

// MARK: - Wire types

/// A model registered against a `connection`.
///
/// Mirrors `vocabulary.schema.json#/definitions/registered_model` exactly:
/// snake_case wire keys, `revision` is the CAS counter the caller must echo
/// back on the next mutation. `capabilitySnapshotRef` is opaque server state
/// and never decoded by the client.
public struct RegisteredModel: Codable, Equatable, Sendable {
    public let registrationID: String
    public let providerModelID: String
    public let connectionID: String
    public let displayName: String
    public let capabilitySnapshotRef: String?
    public let revision: Int

    public init(
        registrationID: String,
        providerModelID: String,
        connectionID: String,
        displayName: String,
        capabilitySnapshotRef: String? = nil,
        revision: Int
    ) {
        self.registrationID = registrationID
        self.providerModelID = providerModelID
        self.connectionID = connectionID
        self.displayName = displayName
        self.capabilitySnapshotRef = capabilitySnapshotRef
        self.revision = revision
    }

    enum CodingKeys: String, CodingKey {
        case registrationID = "registration_id"
        case providerModelID = "provider_model_id"
        case connectionID = "connection_id"
        case displayName = "display_name"
        case capabilitySnapshotRef = "capability_snapshot_ref"
        case revision
    }
}

/// Page of registered models returned by `engine.v1.models.list`.
///
/// `collection` echoes the request collection name. `nextCursor` is the
/// opaque token for the next page (absent when the engine has no more items).
/// `cacheOnly` is `true` only when the engine supplied catalog items.
public struct ModelListPage: Equatable, Sendable {
    public let collection: String
    public let items: [RegisteredModel]
    public let nextCursor: String?
    public let cacheOnly: Bool?

    public init(
        collection: String,
        items: [RegisteredModel],
        nextCursor: String? = nil,
        cacheOnly: Bool? = nil
    ) {
        self.collection = collection
        self.items = items
        self.nextCursor = nextCursor
        self.cacheOnly = cacheOnly
    }
}

// MARK: - Internal request/response shapes

private struct ModelListParams: Encodable, Sendable {
    let collection: String
    let query: String?
    let cursor: String?
    let limit: Int?

    enum CodingKeys: String, CodingKey {
        case collection
        case query
        case cursor
        case limit
    }
}

private struct ModelListResult: Decodable, Sendable {
    let collection: String
    let items: [RegisteredModel]
    let nextCursor: String?
    let cacheOnly: Bool?

    enum CodingKeys: String, CodingKey {
        case collection
        case items
        case nextCursor = "next_cursor"
        case cacheOnly = "cache_only"
    }
}

private struct ModelRegisterParams: Encodable, Sendable {
    let connectionID: String
    let providerModelID: String
    let displayName: String
    let expectedRevision: Int
    let idempotencyKey: String

    enum CodingKeys: String, CodingKey {
        case connectionID = "connection_id"
        case providerModelID = "provider_model_id"
        case displayName = "display_name"
        case expectedRevision = "expected_revision"
        case idempotencyKey = "idempotency_key"
    }
}

private struct ModelRenameParams: Encodable, Sendable {
    let registrationID: String
    let displayName: String
    let expectedRevision: Int
    let idempotencyKey: String

    enum CodingKeys: String, CodingKey {
        case registrationID = "registration_id"
        case displayName = "display_name"
        case expectedRevision = "expected_revision"
        case idempotencyKey = "idempotency_key"
    }
}

private struct ModelRemoveParams: Encodable, Sendable {
    let registrationID: String
    let expectedRevision: Int
    let idempotencyKey: String

    enum CodingKeys: String, CodingKey {
        case registrationID = "registration_id"
        case expectedRevision = "expected_revision"
        case idempotencyKey = "idempotency_key"
    }
}

private struct ModelMutationResult: Decodable, Sendable {
    let model: RegisteredModel
}

private struct ModelRemoveResult: Decodable, Sendable {
    let removed: Bool
}

private extension NSLock {
    func withLock<T>(_ body: () throws -> T) rethrows -> T {
        lock()
        defer { unlock() }
        return try body()
    }
}

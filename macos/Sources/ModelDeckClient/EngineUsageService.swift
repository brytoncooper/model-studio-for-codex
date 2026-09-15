import Foundation
import ModelDeckContracts

/// Which kind of money a usage/price/benchmark value carries.
///
/// Mirrors `contracts/engine.v1/vocabulary.schema.json#/definitions/cost_kind`.
/// The three kinds are never summed together; a consumer must not merge them.
public enum EngineCostKind: String, Codable, Equatable, Sendable {
    case estimated
    case providerSettled = "provider_settled"
    case subscriptionAllowance = "subscription_allowance"
}

/// Source and age of a cached price, benchmark, or allowance value.
///
/// Mirrors `contracts/engine.v1/vocabulary.schema.json#/definitions/source_provenance`.
public struct EngineSourceProvenance: Codable, Equatable, Sendable {
    public let sourceID: String
    public let sourceURL: String?
    public let citation: String?
    /// Calendar date (YYYY-MM-DD) the source itself says the value is effective for.
    public let asOf: String?
    /// When this engine last successfully read the source. Always present.
    public let fetchedAt: String
    /// True when the cached value is past its freshness window or its most
    /// recent refresh failed. A stale-but-good value is still served.
    public let stale: Bool
    public let lastRefreshError: String?

    enum CodingKeys: String, CodingKey {
        case sourceID = "source_id"
        case sourceURL = "source_url"
        case citation
        case asOf = "as_of"
        case fetchedAt = "fetched_at"
        case stale
        case lastRefreshError = "last_refresh_error"
    }
}

public struct EngineUsageRecord: Codable, Equatable, Sendable {
    public let runID: String
    public let sessionID: String
    public let providerModelID: String?
    public let observedAt: String
    public let units: Double
    public let unitKind: String
    public let settledAmount: Double?
    public let currency: String?
    public let estimateAmount: Double?
    /// Absent means the engine did not record a kind for this row; a
    /// consumer must then treat subscription/cost as unknown rather than
    /// guessing from other fields (provider, route, etc.).
    public let costKind: EngineCostKind?
    public let provenance: EngineSourceProvenance?

    enum CodingKeys: String, CodingKey {
        case runID = "run_id"; case sessionID = "session_id"; case providerModelID = "provider_model_id"; case observedAt = "observed_at"; case units; case unitKind = "unit_kind"; case settledAmount = "settled_amount"; case currency; case estimateAmount = "estimate_amount"; case costKind = "cost_kind"; case provenance
    }
}

/// Price per single unit of a `usage_record` unit_kind, or `nil` when the
/// source publishes no price for it. `nil` means unknown and never zero.
public struct EngineUnitPrices: Codable, Equatable, Sendable {
    public let inputTokens: Double?
    public let outputTokens: Double?
    public let cachedTokens: Double?

    enum CodingKeys: String, CodingKey {
        case inputTokens = "input_tokens"; case outputTokens = "output_tokens"; case cachedTokens = "cached_tokens"
    }
}

/// Mirrors `contracts/engine.v1/vocabulary.schema.json#/definitions/price_record`.
public struct EnginePriceRecord: Codable, Equatable, Sendable {
    public let providerModelID: String
    public let registrationID: String?
    /// ISO 4217 code every value in `unitPrices` is denominated in. `nil`
    /// when the source states no currency, which makes the prices unusable
    /// for money math.
    public let currency: String?
    public let unitPrices: EngineUnitPrices
    public let provenance: EngineSourceProvenance

    enum CodingKeys: String, CodingKey {
        case providerModelID = "provider_model_id"; case registrationID = "registration_id"; case currency; case unitPrices = "unit_prices"; case provenance
    }
}

/// Result of `engine.v1.prices.query`. `cached` is always `true`: this is a
/// read path and never triggers a hidden network fetch.
public struct EnginePricesQueryResult: Decodable, Equatable, Sendable {
    public let records: [EnginePriceRecord]
    public let snapshot: EngineSourceProvenance
    public let cached: Bool
}

/// Mirrors `contracts/engine.v1/vocabulary.schema.json#/definitions/benchmark_record`.
public struct EngineBenchmarkRecord: Codable, Equatable, Sendable {
    /// The model identifier the benchmark source itself uses, verbatim.
    public let sourceModelRef: String
    /// `nil` when the engine has not mapped `sourceModelRef` to a provider
    /// model id yet; the row stays visible rather than being dropped.
    public let providerModelID: String?
    public let sourceID: String
    public let feed: String
    public let metric: String
    public let score: Double?
    public let provenance: EngineSourceProvenance

    enum CodingKeys: String, CodingKey {
        case sourceModelRef = "source_model_ref"; case providerModelID = "provider_model_id"; case sourceID = "source_id"; case feed; case metric; case score; case provenance
    }
}

/// Result of `engine.v1.benchmarks.query`.
public struct EngineBenchmarksQueryResult: Decodable, Equatable, Sendable {
    public let benchmarks: [EngineBenchmarkRecord]
    public let snapshot: EngineSourceProvenance?
    public let cached: Bool?
}

/// Result of `engine.v1.prices.refresh` / `engine.v1.benchmarks.refresh`.
///
/// The refresh is asynchronous: this only starts the job. Poll
/// `engine.v1.jobs.get` with `jobID` for completion; cached query results
/// are unchanged until it does.
public struct EngineRefreshJobResult: Decodable, Equatable, Sendable {
    public let jobID: String
    public let jobKind: String?
    public let explicitNetwork: Bool?

    enum CodingKeys: String, CodingKey {
        case jobID = "job_id"; case jobKind = "job_kind"; case explicitNetwork = "explicit_network"
    }
}

/// One `cost_kind` bucket's usage totals from `engine.v1.usage.summary`.
public struct EngineUsageTotal: Decodable, Equatable, Sendable {
    public let costKind: EngineCostKind
    /// Sum of monetary values in this group, or `nil` when any contributing
    /// record's amount is unknown or the group spans more than one currency.
    public let amount: Double?
    public let currency: String?
    public let units: Double?
    public let recordCount: Int

    enum CodingKeys: String, CodingKey {
        case costKind = "cost_kind"; case amount; case currency; case units; case recordCount = "record_count"
    }
}

/// Result of `engine.v1.usage.summary`: at most one total per `cost_kind`,
/// estimated/provider-settled/subscription-allowance kept separate.
public struct EngineUsageSummaryResult: Decodable, Equatable, Sendable {
    public let totals: [EngineUsageTotal]
}

public final class EngineUsageService: @unchecked Sendable {
    private let rendezvous: EngineRendezvousDescriptor
    private let client: ModelDeckEngineClient
    private let lock = NSLock()
    public init(rendezvous: EngineRendezvousDescriptor, transport: EngineTransport, credentialProvider: EngineInstanceCredentialProviding) {
        self.rendezvous = rendezvous; self.client = ModelDeckEngineClient(transport: transport, credentialProvider: credentialProvider)
    }
    public func connect() throws { try lock.withLock { try client.connectAndAuthenticate(descriptor: rendezvous) } }

    public func query() throws -> [EngineUsageRecord] {
        struct Result: Decodable { let records: [EngineUsageRecord] }
        return try lock.withLock {
            let result: Result = try client.invokeValidated(method: "engine.v1.usage.query", params: EmptyUsageParams(), paramsSchemaRef: "contracts/engine.v1/methods/usage.query.params.schema.json", resultSchemaRef: "contracts/engine.v1/methods/usage.query.result.schema.json")
            return result.records
        }
    }

    /// Calls `engine.v1.usage.summary`. Records with no recorded `cost_kind`
    /// are not represented in the totals; read them through ``query()``.
    public func summary(since: String? = nil, until: String? = nil) throws -> EngineUsageSummaryResult {
        try lock.withLock {
            try client.invokeValidated(method: "engine.v1.usage.summary", params: UsageWindowParams(since: since, until: until), paramsSchemaRef: "contracts/engine.v1/methods/usage.summary.params.schema.json", resultSchemaRef: "contracts/engine.v1/methods/usage.summary.result.schema.json")
        }
    }

    /// Calls `engine.v1.prices.query`. This is a read path: it never fetches
    /// over the network, so an empty or stale answer is resolved by calling
    /// ``refreshPrices(idempotencyKey:)``, not by retrying this call.
    public func queryPrices(
        providerModelID: String? = nil,
        registrationID: String? = nil,
        includeStale: Bool = true
    ) throws -> EnginePricesQueryResult {
        try lock.withLock {
            try client.invokeValidated(
                method: "engine.v1.prices.query",
                params: PricesQueryParams(providerModelID: providerModelID, registrationID: registrationID, includeStale: includeStale),
                paramsSchemaRef: "contracts/engine.v1/methods/prices.query.params.schema.json",
                resultSchemaRef: "contracts/engine.v1/methods/prices.query.result.schema.json"
            )
        }
    }

    /// Starts an explicit price refresh job and returns its id immediately;
    /// this is the only path that reaches the network for prices. Supply a
    /// fresh ``newIdempotencyKey()`` per logical refresh, or repeat a
    /// caller-tracked key to observe the same job again.
    public func refreshPrices(idempotencyKey: String) throws -> EngineRefreshJobResult {
        try lock.withLock {
            try client.invokeValidated(
                method: "engine.v1.prices.refresh",
                params: IdempotentRefreshParams(idempotencyKey: idempotencyKey),
                paramsSchemaRef: "contracts/engine.v1/methods/prices.refresh.params.schema.json",
                resultSchemaRef: "contracts/engine.v1/methods/prices.refresh.result.schema.json"
            )
        }
    }

    /// Calls `engine.v1.benchmarks.query`, optionally scoped to one model id.
    public func queryBenchmarks(modelID: String? = nil) throws -> EngineBenchmarksQueryResult {
        try lock.withLock {
            try client.invokeValidated(
                method: "engine.v1.benchmarks.query",
                params: BenchmarksQueryParams(modelID: modelID),
                paramsSchemaRef: "contracts/engine.v1/methods/benchmarks.query.params.schema.json",
                resultSchemaRef: "contracts/engine.v1/methods/benchmarks.query.result.schema.json"
            )
        }
    }

    /// Starts an explicit benchmark refresh job and returns its id immediately.
    public func refreshBenchmarks(idempotencyKey: String) throws -> EngineRefreshJobResult {
        try lock.withLock {
            try client.invokeValidated(
                method: "engine.v1.benchmarks.refresh",
                params: IdempotentRefreshParams(idempotencyKey: idempotencyKey),
                paramsSchemaRef: "contracts/engine.v1/methods/benchmarks.refresh.params.schema.json",
                resultSchemaRef: "contracts/engine.v1/methods/benchmarks.refresh.result.schema.json"
            )
        }
    }

    /// Reads the bounded public job snapshot for `jobID` via
    /// `engine.v1.jobs.get`, so a price/benchmark refresh job started by
    /// ``refreshPrices(idempotencyKey:)``/``refreshBenchmarks(idempotencyKey:)``
    /// can be observed by the same generic ``JobObservationViewController``
    /// used for extension/Notebook jobs. Same shape as
    /// `EngineExtensionPanelService.getJob(jobID:)`.
    public func getJob(jobID: String) throws -> JobSnapshot {
        let result: JobGetResult = try lock.withLock {
            try client.invokeValidated(
                method: "engine.v1.jobs.get",
                params: JobGetParams(jobID: jobID),
                paramsSchemaRef: "contracts/engine.v1/methods/jobs.get.params.schema.json",
                resultSchemaRef: "contracts/engine.v1/methods/jobs.get.result.schema.json"
            )
        }
        return JobSnapshot(
            jobID: result.jobID,
            state: JobState(rawValue: result.state) ?? .unknown,
            progress: result.progress,
            output: result.output,
            outputPresent: result.outputPresent
        )
    }

    /// Requests cancellation of `jobID` via `engine.v1.jobs.cancel`. See
    /// `EngineExtensionPanelService.cancelJob(jobID:idempotencyKey:)` for the
    /// accepted-vs-terminal semantics; identical here since jobs are engine-
    /// wide, not scoped to one service.
    @discardableResult
    public func cancelJob(jobID: String, idempotencyKey: String) throws -> Bool {
        let result: JobCancelResult = try lock.withLock {
            try client.invokeValidated(
                method: "engine.v1.jobs.cancel",
                params: JobCancelParams(jobID: jobID, idempotencyKey: idempotencyKey),
                paramsSchemaRef: "contracts/engine.v1/methods/jobs.cancel.params.schema.json",
                resultSchemaRef: "contracts/engine.v1/methods/jobs.cancel.result.schema.json"
            )
        }
        return result.accepted
    }

    /// Generates a fresh lowercase UUIDv4 for use as an `idempotency_key`,
    /// same convention as ``EngineModelRegistrationService/newIdempotencyKey()``.
    public static func newIdempotencyKey() -> String {
        UUID().uuidString.lowercased()
    }
}

/// Lets ``JobObservationViewController`` observe a price/benchmark refresh
/// job the same way it observes an extension operation's async job.
extension EngineUsageService: JobObservationService {}

private struct EmptyUsageParams: Encodable {}

private struct UsageWindowParams: Encodable {
    let since: String?
    let until: String?
}

private struct PricesQueryParams: Encodable {
    let providerModelID: String?
    let registrationID: String?
    let includeStale: Bool
    enum CodingKeys: String, CodingKey {
        case providerModelID = "provider_model_id"; case registrationID = "registration_id"; case includeStale = "include_stale"
    }
}

private struct BenchmarksQueryParams: Encodable {
    let modelID: String?
    enum CodingKeys: String, CodingKey { case modelID = "model_id" }
}

private struct IdempotentRefreshParams: Encodable {
    let idempotencyKey: String
    enum CodingKeys: String, CodingKey { case idempotencyKey = "idempotency_key" }
}

private struct JobGetParams: Encodable {
    let jobID: String
    enum CodingKeys: String, CodingKey { case jobID = "job_id" }
}

private struct JobGetResult: Decodable {
    let jobID: String
    let state: String
    let progress: Double?
    let output: JSONValue?
    let outputPresent: Bool

    enum CodingKeys: String, CodingKey {
        case jobID = "job_id"
        case state
        case progress
        case output
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        jobID = try container.decode(String.self, forKey: .jobID)
        state = try container.decode(String.self, forKey: .state)
        progress = try container.decodeIfPresent(Double.self, forKey: .progress)
        outputPresent = container.contains(.output)
        output = outputPresent
            ? try container.decode(JSONValue.self, forKey: .output)
            : nil
    }
}

private struct JobCancelParams: Encodable {
    let jobID: String
    let idempotencyKey: String
    enum CodingKeys: String, CodingKey {
        case jobID = "job_id"
        case idempotencyKey = "idempotency_key"
    }
}

private struct JobCancelResult: Decodable {
    let accepted: Bool
}

private extension NSLock { func withLock<T>(_ body: () throws -> T) rethrows -> T { lock(); defer { unlock() }; return try body() } }

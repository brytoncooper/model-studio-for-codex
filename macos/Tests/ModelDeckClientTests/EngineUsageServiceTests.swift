import XCTest
@testable import ModelDeckClient

final class EngineUsageServiceTests: XCTestCase {
    func testUsageRecordDecodesOptionalFieldsAsAbsent() throws {
        let data = #"{"run_id":"run","session_id":"session","observed_at":"2026-01-01T00:00:00Z","units":12,"unit_kind":"input_tokens"}"#.data(using: .utf8)!
        let record = try JSONDecoder().decode(EngineUsageRecord.self, from: data)
        XCTAssertEqual(record.units, 12)
        XCTAssertNil(record.providerModelID)
        XCTAssertNil(record.settledAmount)
        XCTAssertNil(record.estimateAmount)
        XCTAssertNil(record.currency)
        XCTAssertNil(record.costKind)
        XCTAssertNil(record.provenance)
    }

    // B16/C8e: usage_record gained optional cost_kind + provenance.
    func testUsageRecordDecodesCostKindAndProvenanceWhenPresent() throws {
        let data = """
        {"run_id":"run","session_id":"session","observed_at":"2026-01-01T00:00:00Z","units":12,"unit_kind":"input_tokens",
         "settled_amount":0.42,"currency":"USD","cost_kind":"provider_settled",
         "provenance":{"source_id":"com.modeldeck.openrouter","fetched_at":"2026-09-14T00:00:00Z","stale":false,"as_of":"2026-09-14"}}
        """.data(using: .utf8)!
        let record = try JSONDecoder().decode(EngineUsageRecord.self, from: data)
        XCTAssertEqual(record.costKind, .providerSettled)
        XCTAssertEqual(record.settledAmount, 0.42)
        XCTAssertEqual(record.provenance?.sourceID, "com.modeldeck.openrouter")
        XCTAssertEqual(record.provenance?.asOf, "2026-09-14")
        XCTAssertEqual(record.provenance?.stale, false)
    }

    // C8a's guard: an "estimated" record must never carry a numeric
    // settled_amount. This test only exercises decoding (schema validation
    // of that guard lives in the contracts golden fixtures); it documents
    // that the client's Codable model does not itself enforce the rule.
    func testUsageRecordDecodesEstimatedKind() throws {
        let data = #"{"run_id":"run","session_id":"session","observed_at":"2026-01-01T00:00:00Z","units":1,"unit_kind":"output_tokens","estimate_amount":0.01,"cost_kind":"estimated"}"#.data(using: .utf8)!
        let record = try JSONDecoder().decode(EngineUsageRecord.self, from: data)
        XCTAssertEqual(record.costKind, .estimated)
        XCTAssertEqual(record.estimateAmount, 0.01)
        XCTAssertNil(record.settledAmount)
    }

    func testPricesQueryResultDecodesUnknownUnitPriceAsNil() throws {
        let data = """
        {"records":[{"provider_model_id":"deepseek/deepseek-v4.1-flash","currency":"USD",
         "unit_prices":{"input_tokens":0.00000015,"output_tokens":0.0000006,"cached_tokens":null},
         "provenance":{"source_id":"com.modeldeck.openrouter","fetched_at":"2026-09-14T00:00:00Z","stale":false}}],
         "snapshot":{"source_id":"com.modeldeck.openrouter","fetched_at":"2026-09-14T00:00:00Z","stale":false},"cached":true}
        """.data(using: .utf8)!
        let result = try JSONDecoder().decode(EnginePricesQueryResult.self, from: data)
        XCTAssertTrue(result.cached)
        XCTAssertEqual(result.records.count, 1)
        XCTAssertEqual(result.records[0].unitPrices.inputTokens, 0.00000015)
        XCTAssertNil(result.records[0].unitPrices.cachedTokens)
        XCTAssertFalse(result.snapshot.stale)
    }

    func testBenchmarksQueryResultDecodesUnmappedProviderModelIDAsNil() throws {
        let data = """
        {"benchmarks":[{"source_model_ref":"deepseek-v4.1-flash","provider_model_id":null,
         "source_id":"com.modeldeck.artificial-analysis","feed":"artificial-analysis","metric":"coding_index","score":null,
         "provenance":{"source_id":"com.modeldeck.artificial-analysis","fetched_at":"2026-09-14T00:00:00Z","stale":true}}]}
        """.data(using: .utf8)!
        let result = try JSONDecoder().decode(EngineBenchmarksQueryResult.self, from: data)
        XCTAssertEqual(result.benchmarks.count, 1)
        XCTAssertNil(result.benchmarks[0].providerModelID)
        XCTAssertNil(result.benchmarks[0].score)
        XCTAssertTrue(result.benchmarks[0].provenance.stale)
    }

    func testRefreshJobResultDecodesJobIDOnly() throws {
        let data = #"{"job_id":"3fb0f1ac-1f7a-4f0e-9d2e-000000000001"}"#.data(using: .utf8)!
        let result = try JSONDecoder().decode(EngineRefreshJobResult.self, from: data)
        XCTAssertEqual(result.jobID, "3fb0f1ac-1f7a-4f0e-9d2e-000000000001")
        XCTAssertNil(result.jobKind)
        XCTAssertNil(result.explicitNetwork)
    }

    func testRefreshJobResultDecodesJobKindAndExplicitNetworkWhenPresent() throws {
        let data = #"{"job_id":"job-1","job_kind":"com.modeldeck.engine.prices.refresh","explicit_network":true}"#.data(using: .utf8)!
        let result = try JSONDecoder().decode(EngineRefreshJobResult.self, from: data)
        XCTAssertEqual(result.jobKind, "com.modeldeck.engine.prices.refresh")
        XCTAssertEqual(result.explicitNetwork, true)
    }

    func testUsageSummaryResultKeepsCostKindsSeparate() throws {
        let data = """
        {"totals":[
          {"cost_kind":"provider_settled","amount":1.5,"currency":"USD","units":1000,"record_count":3},
          {"cost_kind":"estimated","amount":null,"currency":null,"units":null,"record_count":1}
        ]}
        """.data(using: .utf8)!
        let result = try JSONDecoder().decode(EngineUsageSummaryResult.self, from: data)
        XCTAssertEqual(result.totals.count, 2)
        XCTAssertEqual(result.totals[0].costKind, .providerSettled)
        XCTAssertEqual(result.totals[0].amount, 1.5)
        XCTAssertEqual(result.totals[1].costKind, .estimated)
        XCTAssertNil(result.totals[1].amount)
        XCTAssertEqual(result.totals[1].recordCount, 1)
    }
}

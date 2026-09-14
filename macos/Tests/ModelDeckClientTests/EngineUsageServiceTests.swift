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
    }
}

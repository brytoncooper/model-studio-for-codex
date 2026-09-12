import XCTest
@testable import ModelDeckPresentation

final class UsageModelSelfTestTests: XCTestCase {
    func testLegacyUsageSelfTestScenario() {
        XCTAssertTrue(UsageModelSelfTest.run())
    }
}

import XCTest
@testable import ModelDeckPresentation

final class ModelBrowserStateTests: XCTestCase {
    func testStaleGenerationRejected() {
        var browser = ModelBrowserState()
        let first = browser.begin(route: "account-one")
        let fixture = [
            CatalogModel(id: "kimi-fast", name: "Kimi Coding Fast", suggested: false),
            CatalogModel(id: "kimi-think", name: "Kimi Reasoning", suggested: false),
        ]
        XCTAssertTrue(browser.receive(fixture, generation: first))
        let refresh = browser.begin(route: "account-one")
        XCTAssertFalse(browser.receive([], generation: first))
        let next = browser.begin(route: "account-two")
        XCTAssertTrue(browser.selected.isEmpty)
        XCTAssertFalse(browser.receive(fixture, generation: refresh))
        XCTAssertTrue(browser.receive([], generation: next))
    }

    func testSearchAndSelection() {
        var browser = ModelBrowserState()
        _ = browser.begin(route: "account-one")
        browser.entries = [
            CatalogModel(id: "a", name: "Café", suggested: false),
            CatalogModel(id: "b", name: "Other", suggested: false),
        ]
        browser.query = "cafe"
        XCTAssertEqual(browser.visible.map(\.id), ["a"])
        browser.toggle("a")
        browser.query = "other"
        XCTAssertTrue(browser.selected.contains("a"))
    }
}

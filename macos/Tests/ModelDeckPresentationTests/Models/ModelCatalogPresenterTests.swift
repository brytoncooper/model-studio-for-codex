import XCTest
import ModelDeckClient
@testable import ModelDeckPresentation

private func makeCatalogItem(id: String, name: String, connection: String = "a") throws -> CatalogListItem {
    let payload: [String: Any] = [
        "kind": "catalog",
        "provider_model_id": id,
        "display_name": name,
        "connection_id": connection,
    ]
    return try XCTUnwrap(CatalogListItem(payload: payload))
}

private final class StubCatalogService: ModelCatalogServing {
    var payload: ModelCatalogPayload?
    var error: ModelCatalogServiceError?
    func cancel() {}
    func loadCatalog(
        connection: ModelCatalogConnectionRequest,
        completion: @escaping (Result<ModelCatalogPayload, ModelCatalogServiceError>) -> Void
    ) {
        if let error {
            completion(.failure(error))
            return
        }
        completion(.success(payload ?? ModelCatalogPayload(models: [], suggested: false, statusMessage: "")))
    }
}

private final class PendingStubCatalogService: ModelCatalogServing {
    func cancel() {}
    func loadCatalog(
        connection: ModelCatalogConnectionRequest,
        completion: @escaping (Result<ModelCatalogPayload, ModelCatalogServiceError>) -> Void
    ) {}
}

private final class DelayedStubCatalogService: ModelCatalogServing {
    var delay: TimeInterval = 0.3
    var payload = ModelCatalogPayload(models: [], suggested: false, statusMessage: "late")
    private let lock = NSLock()
    private var cancelled = false

    func cancel() {
        lock.lock()
        cancelled = true
        lock.unlock()
    }

    func loadCatalog(
        connection: ModelCatalogConnectionRequest,
        completion: @escaping (Result<ModelCatalogPayload, ModelCatalogServiceError>) -> Void
    ) {
        lock.lock()
        cancelled = false
        let delay = delay
        let payload = payload
        lock.unlock()
        DispatchQueue.global(qos: .userInitiated).asyncAfter(deadline: .now() + delay) {
            self.lock.lock()
            let active = !self.cancelled
            self.lock.unlock()
            if active {
                completion(.success(payload))
            } else {
                completion(.failure(.cancelled))
            }
        }
    }
}

@MainActor
final class ModelCatalogPresenterTests: XCTestCase {
    private let connection = ModelCatalogConnectionRequest(
        accountID: "a",
        accountName: "A",
        baseURL: "https://example.invalid/v1",
        wire: "chat",
        hasKey: true,
        isCursor: false,
        executablePath: "/tmp/ModelDeck"
    )

    func testLoadingPhase() {
        let presenter = ModelCatalogPresenter()
        let service = PendingStubCatalogService()
        presenter.load(connection: connection, service: service) { _ in }
        XCTAssertEqual(presenter.phase, .loading)
    }

    func testFailurePhase() {
        let presenter = ModelCatalogPresenter()
        let service = StubCatalogService()
        service.error = .message("offline")
        let exp = expectation(description: "failure")
        presenter.load(connection: connection, service: service) { outcome in
            XCTAssertEqual(outcome.phase, .failure("offline"))
            XCTAssertTrue(outcome.applied)
            XCTAssertNil(outcome.models)
            exp.fulfill()
        }
        wait(for: [exp], timeout: 2)
    }

    func testReadyPhase() throws {
        let presenter = ModelCatalogPresenter()
        var browser = ModelBrowserState()
        let service = StubCatalogService()
        service.payload = ModelCatalogPayload(
            models: [try makeCatalogItem(id: "m1", name: "M1")],
            suggested: false,
            statusMessage: "1 cached models from A."
        )
        let generation = browser.begin(route: connection.routeKey)
        let exp = expectation(description: "ready")
        presenter.load(connection: connection, service: service) { outcome in
            XCTAssertEqual(outcome.phase, .ready)
            XCTAssertTrue(outcome.applied)
            XCTAssertEqual(outcome.models?.count, 1)
            if let models = outcome.models {
                XCTAssertTrue(browser.receive(models, generation: generation))
                XCTAssertEqual(browser.entries.count, 1)
            }
            exp.fulfill()
        }
        wait(for: [exp], timeout: 2)
    }

    func testEmptyPhase() {
        let presenter = ModelCatalogPresenter()
        let service = StubCatalogService()
        service.payload = ModelCatalogPayload(models: [], suggested: false, statusMessage: "none")
        let exp = expectation(description: "empty")
        presenter.load(connection: connection, service: service) { outcome in
            XCTAssertEqual(outcome.phase, .empty)
            XCTAssertTrue(outcome.applied)
            XCTAssertEqual(outcome.models?.count, 0)
            exp.fulfill()
        }
        wait(for: [exp], timeout: 2)
    }

    func testUnavailablePhase() throws {
        let presenter = ModelCatalogPresenter()
        let service = StubCatalogService()
        service.payload = ModelCatalogPayload(
            models: [try makeCatalogItem(id: "s1", name: "S1")],
            suggested: true,
            statusMessage: "Suggested IDs only",
            unavailable: true
        )
        let exp = expectation(description: "unavailable")
        presenter.load(connection: connection, service: service) { outcome in
            XCTAssertEqual(outcome.phase, .unavailable("Suggested IDs only"))
            XCTAssertTrue(outcome.applied)
            exp.fulfill()
        }
        wait(for: [exp], timeout: 2)
    }

    func testExplicitCancel() {
        let presenter = ModelCatalogPresenter()
        let service = DelayedStubCatalogService()
        let exp = expectation(description: "cancelled")
        presenter.load(connection: connection, service: service) { outcome in
            XCTAssertFalse(outcome.applied)
            exp.fulfill()
        }
        XCTAssertEqual(presenter.phase, .loading)
        presenter.cancel()
        XCTAssertEqual(presenter.phase, .idle)
        wait(for: [exp], timeout: 2)
    }

    func testStaleResponseSuperseded() throws {
        let presenter = ModelCatalogPresenter()
        var browser = ModelBrowserState()
        let generation = browser.begin(route: connection.routeKey)
        let first = StubCatalogService()
        first.payload = ModelCatalogPayload(models: [], suggested: false, statusMessage: "late")
        presenter.load(connection: connection, service: first) { _ in }
        presenter.cancel()
        let second = StubCatalogService()
        second.payload = ModelCatalogPayload(
            models: [try makeCatalogItem(id: "m2", name: "M2")],
            suggested: false,
            statusMessage: "ok"
        )
        let exp = expectation(description: "second")
        presenter.load(connection: connection, service: second) { outcome in
            XCTAssertTrue(outcome.applied)
            if let models = outcome.models {
                XCTAssertTrue(browser.receive(models, generation: generation))
                XCTAssertEqual(browser.entries.first?.id, "m2")
            }
            exp.fulfill()
        }
        wait(for: [exp], timeout: 2)
    }

    func testDelayedStaleCompletionRejected() throws {
        let presenter = ModelCatalogPresenter()
        var browser = ModelBrowserState()
        let generation = browser.begin(route: connection.routeKey)
        let slow = DelayedStubCatalogService()
        slow.payload = ModelCatalogPayload(
            models: [try makeCatalogItem(id: "m1", name: "M1")],
            suggested: false,
            statusMessage: "late"
        )
        let staleIgnored = expectation(description: "stale ignored")
        staleIgnored.isInverted = true
        presenter.load(connection: connection, service: slow) { outcome in
            if outcome.applied {
                staleIgnored.fulfill()
            }
        }
        let fast = StubCatalogService()
        fast.payload = ModelCatalogPayload(
            models: [try makeCatalogItem(id: "m2", name: "M2")],
            suggested: false,
            statusMessage: "ok"
        )
        let current = expectation(description: "current")
        presenter.load(connection: connection, service: fast) { outcome in
            XCTAssertTrue(outcome.applied)
            if let models = outcome.models {
                XCTAssertTrue(browser.receive(models, generation: generation))
            }
            current.fulfill()
        }
        wait(for: [current], timeout: 2)
        wait(for: [staleIgnored], timeout: 0.6)
        XCTAssertEqual(browser.entries.first?.id, "m2")
    }
}

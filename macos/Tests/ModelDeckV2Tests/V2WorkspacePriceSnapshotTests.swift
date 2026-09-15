import AppKit
import XCTest
import ModelDeckClient
import ModelDeckContracts
@testable import ModelDeckV2

/// B16/C8e: the price snapshot row `attach(usageService:bridgeSummary:)`
/// installs next to the existing usage summary. Drives a real
/// `EngineUsageService` over `FakeEngineTransport` so the test exercises the
/// actual JSON-RPC + schema-validated round trip, not a mock of it.
///
/// `attach` chains the usage-summary load and the price-snapshot load
/// (see `V2WorkspaceController.refreshUsage(completion:)`) specifically so
/// their two engine calls do not race each other for this connection's
/// `md-N` request ids; that also makes the two calls below deterministic:
/// hello, hello, `usage.query`, `prices.query`.
@MainActor
final class V2WorkspacePriceSnapshotTests: XCTestCase {
    private struct FixedCredentialProvider: EngineInstanceCredentialProviding {
        func readCredential() throws -> String { "operator-secret" }
    }

    private func descriptor() -> EngineRendezvousDescriptor {
        EngineRendezvousDescriptor(
            transport: "unix", socketPath: "/tmp/model-deck-engine.sock",
            engineInstanceID: "550e8400-e29b-41d4-a716-446655440000",
            instanceNonce: "rendezvous-nonce-7f3a", apiProfile: EngineAPIProfile(major: 1, minor: 0)
        )
    }

    private func hello(authenticated: Bool) -> [String: Any] {
        [
            "authenticated": authenticated,
            "api_profile": ["major": 1, "minor": 0],
            "engine_instance_id": "550e8400-e29b-41d4-a716-446655440000",
            "instance_nonce": "rendezvous-nonce-7f3a",
            "capabilities": ["features": ["tools": "supported"]],
        ]
    }

    private func frame(id: String, result: Any) -> Data {
        try! JSONSerialization.data(withJSONObject: ["jsonrpc": "2.0", "id": id, "result": result])
    }

    private func errorFrame(id: String, message: String) -> Data {
        try! JSONSerialization.data(withJSONObject: ["jsonrpc": "2.0", "id": id, "error": ["code": -32000, "message": message]])
    }

    private func makeUsageService(responses: [Data]) throws -> EngineUsageService {
        let transport = FakeEngineTransport(responses: responses)
        let service = EngineUsageService(rendezvous: descriptor(), transport: transport, credentialProvider: FixedCredentialProvider())
        try service.connect()
        return service
    }

    /// Loads the controller's view and returns the price row's label/button,
    /// found by their known initial title/text rather than by selector, so
    /// the test needs no access to the controller's `private` action methods.
    private func makeController() throws -> (V2WorkspaceController, priceLabel: NSTextField, refreshButton: NSButton) {
        let controller = V2WorkspaceController()
        controller.loadView()
        let label = try XCTUnwrap(
            findTextField(in: controller.view) { $0.stringValue == "Prices not loaded" },
            "price snapshot label not found in the loaded view"
        )
        let button = try XCTUnwrap(
            findButton(in: controller.view) { $0.title == "Refresh prices" },
            "refresh prices button not found in the loaded view"
        )
        return (controller, label, button)
    }

    func testAttachShowsUnavailableWhenPricesQueryOperationIsAbsent() throws {
        let usageService = try makeUsageService(responses: [
            frame(id: "md-1", result: hello(authenticated: false)),
            frame(id: "md-2", result: hello(authenticated: true)),
            frame(id: "md-3", result: ["records": []]),
            errorFrame(id: "md-4", message: "method not implemented: engine.v1.prices.query"),
        ])
        let (controller, priceLabel, refreshButton) = try makeController()

        controller.attach(usageService: usageService, bridgeSummary: nil)

        try waitUntil(timeout: 2.0) { priceLabel.stringValue != "Prices not loaded" && priceLabel.stringValue != "Loading prices…" }
        XCTAssertTrue(priceLabel.stringValue.contains("unavailable"), priceLabel.stringValue)
        XCTAssertTrue(priceLabel.stringValue.contains("engine.v1.prices.query"), priceLabel.stringValue)
        // Settled (not .loading), so the refresh action stays available even
        // though the last read reported the capability missing.
        XCTAssertTrue(refreshButton.isEnabled)
    }

    func testAttachShowsReadyWithProvenanceWhenPricesAreCached() throws {
        let fetchedAt = "2026-09-14T12:00:00Z"
        let priceRecord: [String: Any] = [
            "provider_model_id": "deepseek/deepseek-v4.1-flash",
            "currency": "USD",
            "unit_prices": ["input_tokens": 0.00000015, "output_tokens": 0.0000006, "cached_tokens": NSNull()],
            "provenance": ["source_id": "com.modeldeck.openrouter", "fetched_at": fetchedAt, "stale": false],
        ]
        let usageService = try makeUsageService(responses: [
            frame(id: "md-1", result: hello(authenticated: false)),
            frame(id: "md-2", result: hello(authenticated: true)),
            frame(id: "md-3", result: ["records": []]),
            frame(id: "md-4", result: [
                "records": [priceRecord],
                "snapshot": ["source_id": "com.modeldeck.openrouter", "fetched_at": fetchedAt, "stale": false],
                "cached": true,
            ]),
        ])
        let (controller, priceLabel, refreshButton) = try makeController()

        controller.attach(usageService: usageService, bridgeSummary: nil)

        try waitUntil(timeout: 2.0) { priceLabel.stringValue != "Prices not loaded" && priceLabel.stringValue != "Loading prices…" }
        XCTAssertTrue(priceLabel.stringValue.contains("last refreshed"), priceLabel.stringValue)
        XCTAssertFalse(priceLabel.stringValue.contains("stale"), "a fresh, non-stale snapshot must not read as stale")
        XCTAssertTrue(refreshButton.isEnabled)
    }

    /// `job_id` is `format: "uuid"` in
    /// `contracts/engine.v1/methods/prices.refresh.result.schema.json`, and
    /// `EngineUsageService.refreshPrices` validates the result against that
    /// schema — so a readable stub id like "job-refresh-1" would be rejected
    /// before the row ever saw it. Job ids in these tests are real UUIDs.
    private static let refreshJobID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"

    func testRefreshPricesButtonStartsJobAndShowsJobID() throws {
        let usageService = try makeUsageService(responses: [
            frame(id: "md-1", result: hello(authenticated: false)),
            frame(id: "md-2", result: hello(authenticated: true)),
            frame(id: "md-3", result: ["records": []]),
            errorFrame(id: "md-4", message: "method not implemented: engine.v1.prices.query"),
            frame(id: "md-5", result: ["job_id": Self.refreshJobID, "job_kind": "com.modeldeck.engine.prices.refresh"]),
            // First poll from the job observation row the started job opens;
            // a terminal state stops its timer, so the test leaves no polling
            // behind it.
            frame(id: "md-6", result: ["job_id": Self.refreshJobID, "state": "completed"]),
        ])
        let (controller, priceLabel, refreshButton) = try makeController()
        controller.attach(usageService: usageService, bridgeSummary: nil)
        try waitUntil(timeout: 2.0) { priceLabel.stringValue.contains("unavailable") }

        refreshButton.performClick(nil)

        try waitUntil(timeout: 2.0) { priceLabel.stringValue.contains(Self.refreshJobID) }
        XCTAssertTrue(priceLabel.stringValue.contains("Refresh started"), priceLabel.stringValue)
        // The row settles out of the in-flight state, so a second refresh is
        // possible without reloading the workspace.
        XCTAssertTrue(refreshButton.isEnabled)
    }

    /// A refresh that cannot be started must settle the row rather than
    /// leaving it on the in-flight "Starting price refresh…" line, which
    /// keeps the button disabled and strands the user with no retry.
    func testRefreshPricesFailureSettlesRowAndKeepsButtonEnabled() throws {
        let usageService = try makeUsageService(responses: [
            frame(id: "md-1", result: hello(authenticated: false)),
            frame(id: "md-2", result: hello(authenticated: true)),
            frame(id: "md-3", result: ["records": []]),
            errorFrame(id: "md-4", message: "method not implemented: engine.v1.prices.query"),
            errorFrame(id: "md-5", message: "price source unreachable"),
        ])
        let (controller, priceLabel, refreshButton) = try makeController()
        controller.attach(usageService: usageService, bridgeSummary: nil)
        try waitUntil(timeout: 2.0) { priceLabel.stringValue.contains("unavailable") }

        refreshButton.performClick(nil)

        try waitUntil(timeout: 2.0) { priceLabel.stringValue != "Starting price refresh…" }
        // `EngineJSONRPC.parseResponse` maps every JSON-RPC error to
        // `EngineClientError.unavailable`, so this asserts the row settled
        // and named the refresh, not which of the two settled states it took.
        XCTAssertTrue(priceLabel.stringValue.contains("Price refresh"), priceLabel.stringValue)
        XCTAssertTrue(priceLabel.stringValue.contains("price source unreachable"), priceLabel.stringValue)
        XCTAssertTrue(refreshButton.isEnabled)
    }

    // MARK: - view traversal / polling helpers

    private func findTextField(in view: NSView, matching predicate: (NSTextField) -> Bool) -> NSTextField? {
        if let field = view as? NSTextField, predicate(field) { return field }
        for sub in view.subviews {
            if let found = findTextField(in: sub, matching: predicate) { return found }
        }
        return nil
    }

    private func findButton(in view: NSView, matching predicate: (NSButton) -> Bool) -> NSButton? {
        if let button = view as? NSButton, predicate(button) { return button }
        for sub in view.subviews {
            if let found = findButton(in: sub, matching: predicate) { return found }
        }
        return nil
    }

    /// Polls `condition` on the main run loop until it is true or `timeout`
    /// elapses. Background engine calls in this file complete quickly
    /// (in-memory fake transport, no real I/O); this only bridges their
    /// `DispatchQueue.main.async` completion back into the synchronous test.
    private func waitUntil(timeout: TimeInterval, condition: @escaping () -> Bool) throws {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() {
            if Date() >= deadline {
                XCTFail("condition not met within \(timeout)s")
                return
            }
            RunLoop.main.run(until: Date().addingTimeInterval(0.02))
        }
    }
}

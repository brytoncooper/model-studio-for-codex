import AppKit
import XCTest
import ModelDeckClient
import ModelDeckContracts
@testable import ModelDeckV2

/// Fake ``JobObservationService`` used to capture cancellation requests.
final class FakeJobObservationService: JobObservationService {
    private let scriptedSnapshots: [String: [JobSnapshot]]
    private let cancelBehaviour: (String) -> Bool
    private(set) var cancelCalls: [(jobID: String, idempotencyKey: String)] = []
    private var getCallCount = 0

    init(
        scriptedSnapshots: [String: [JobSnapshot]],
        cancelBehaviour: @escaping (String) -> Bool = { _ in true }
    ) {
        self.scriptedSnapshots = scriptedSnapshots
        self.cancelBehaviour = cancelBehaviour
    }

    func getJob(jobID: String) throws -> JobSnapshot {
        getCallCount += 1
        let sequence = scriptedSnapshots[jobID] ?? []
        if sequence.isEmpty {
            return JobSnapshot(jobID: jobID, state: .unknown)
        }
        let index = min(getCallCount - 1, sequence.count - 1)
        return sequence[index]
    }

    func cancelJob(jobID: String, idempotencyKey: String) throws -> Bool {
        cancelCalls.append((jobID: jobID, idempotencyKey: idempotencyKey))
        return cancelBehaviour(jobID)
    }

    func resetGetCounter() { getCallCount = 0 }
}

@MainActor
final class V2JobObservationTests: XCTestCase {
    private let jobID = "11111111-1111-4111-8111-111111111111"

    private func makeController(
        snapshots: [JobSnapshot],
        cancelAccepted: Bool = true
    ) -> (JobObservationViewController, FakeJobObservationService) {
        let fake = FakeJobObservationService(
            scriptedSnapshots: [jobID: snapshots],
            cancelBehaviour: { _ in cancelAccepted }
        )
        let controller = JobObservationViewController(jobID: jobID, service: fake)
        // Force view loading synchronously so we can inspect subviews.
        controller.loadView()
        return (controller, fake)
    }

    func testRunningSnapshotTransitionsToCancelRequestedBeforeTerminal() throws {
        let snapshots: [JobSnapshot] = [
            JobSnapshot(jobID: jobID, state: .running, progress: 0.1),
            JobSnapshot(jobID: jobID, state: .running, progress: 0.5),
            JobSnapshot(jobID: jobID, state: .cancelled, progress: 0.5),
        ]
        let (controller, fake) = makeController(snapshots: snapshots)
        controller.applySnapshotForTest(snapshots[0])
        XCTAssertFalse(controller.isTerminal)
        controller.applySnapshotForTest(snapshots[1])
        XCTAssertFalse(controller.isTerminal)

        // Simulate the user clicking the cancel button.
        let cancelButton = try XCTUnwrap(findButton(in: controller.view, action: #selector(JobObservationViewController.cancelTapped)))
        cancelButton.performClick(nil)

        let exp = expectation(description: "cancel dispatched")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { exp.fulfill() }
        wait(for: [exp], timeout: 1.0)
        XCTAssertEqual(fake.cancelCalls.count, 1)
        XCTAssertEqual(fake.cancelCalls.first?.jobID, jobID)
        XCTAssertNotNil(UUID(uuidString: fake.cancelCalls.first?.idempotencyKey ?? ""))

        // The next poll observes the terminal cancelled snapshot.
        controller.applySnapshotForTest(snapshots[2])
        XCTAssertTrue(controller.isTerminal)
    }

    func testCompletionRendersCanonicalOutputWithoutPluginParsing() throws {
        let output: JSONValue = .object([
            "media_type": .string("text/markdown"),
            "suggested_filename": .string("session-notebook.md"),
            "content": .string("# Hello"),
        ])
        let snapshots: [JobSnapshot] = [
            JobSnapshot(jobID: jobID, state: .completed, output: output, outputPresent: true),
        ]
        let (controller, _) = makeController(snapshots: snapshots)
        controller.applySnapshotForTest(snapshots[0])
        XCTAssertTrue(controller.isTerminal)
        let textView = try XCTUnwrap(findTextView(in: controller.view))
        let rendered = textView.string
        XCTAssertTrue(
            rendered.contains("media_type"),
            "rendered output should be canonical JSON, got: \(rendered)"
        )
        XCTAssertTrue(rendered.contains("session-notebook.md"))
        XCTAssertTrue(rendered.contains("Hello"))
    }

    func testAlreadyTerminalCancelYieldsDisabledButton() throws {
        let snapshots: [JobSnapshot] = [
            JobSnapshot(jobID: jobID, state: .failed, progress: 0.0),
        ]
        let (controller, fake) = makeController(snapshots: snapshots)
        controller.applySnapshotForTest(snapshots[0])
        XCTAssertTrue(controller.isTerminal)
        let cancelButton = try XCTUnwrap(findButton(in: controller.view, action: #selector(JobObservationViewController.cancelTapped)))
        XCTAssertFalse(cancelButton.isEnabled)
        cancelButton.performClick(nil)
        XCTAssertEqual(fake.cancelCalls.count, 0)
    }

    private func findButton(in view: NSView, action: Selector) -> NSButton? {
        if let button = view as? NSButton, button.action == action { return button }
        for sub in view.subviews {
            if let found = findButton(in: sub, action: action) { return found }
        }
        return nil
    }

    private func findTextView(in view: NSView) -> NSTextView? {
        if let tv = view as? NSTextView { return tv }
        for sub in view.subviews {
            if let found = findTextView(in: sub) { return found }
        }
        return nil
    }
}

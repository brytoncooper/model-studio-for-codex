import AppKit
import XCTest
@testable import ModelDeckPresentation
import ModelDeckContracts

@MainActor
final class PanelRendererTests: XCTestCase {
    func testGenericPanelBindsCurrentTextAndPreservesStaticJSONWithoutExecuting() async throws {
        let document = try decode(Self.actionPanelJSON)
        let saveLookup = expectation(description: "save operation lookup")
        let unknownLookup = expectation(description: "unknown operation lookup")
        var intents: [PanelActionIntent] = []

        let renderer = PanelRenderer(
            document: document,
            operationLookup: { operationID in
                switch operationID {
                case "example.notes.save":
                    saveLookup.fulfill()
                    return TrustedPanelOperationDescriptor(operationID: operationID)
                default:
                    unknownLookup.fulfill()
                    return nil
                }
            },
            onActionIntent: { intents.append($0) }
        )
        renderer.loadViewIfNeeded()
        await fulfillment(of: [saveLookup, unknownLookup], timeout: 2)
        await Task.yield()

        let allViews = descendants(of: renderer.view)
        let inertURLText = try XCTUnwrap(allViews.compactMap { $0 as? NSTextField }
            .first { !$0.isEditable && $0.stringValue == "https://example.test/plain" })
        XCTAssertNil(inertURLText.target)
        XCTAssertNil(inertURLText.action)
        XCTAssertNil(inertURLText.attributedStringValue.attribute(
            .link,
            at: 0,
            effectiveRange: nil
        ))

        let titleInput = try XCTUnwrap(allViews.compactMap { $0 as? NSTextField }
            .first { $0.isEditable && $0.accessibilityLabel() == "Title" })
        titleInput.stringValue = "Current title"
        renderer.controlTextDidChange(Notification(
            name: NSControl.textDidChangeNotification,
            object: titleInput
        ))

        let bodyInput = try XCTUnwrap(allViews.compactMap { $0 as? NSTextView }
            .first { $0.accessibilityLabel() == "Body" })
        XCTAssertTrue(bodyInput.isEditable)
        bodyInput.string = "Current\nmultiline body"
        renderer.textDidChange(Notification(name: NSText.didChangeNotification, object: bodyInput))

        let buttons = allViews.compactMap { $0 as? NSButton }
        let saveButton = try XCTUnwrap(buttons.first { $0.title == "Save" })
        let unknownButton = try XCTUnwrap(buttons.first { $0.title == "Missing operation" })
        XCTAssertTrue(saveButton.isEnabled)
        XCTAssertFalse(unknownButton.isEnabled)
        XCTAssertEqual(saveButton.accessibilityLabel(), "Save")

        saveButton.performClick(nil)
        XCTAssertEqual(intents.count, 1)
        XCTAssertEqual(intents[0].panelID, "example.notes.editor")
        XCTAssertEqual(intents[0].documentRevision, 4)
        XCTAssertEqual(intents[0].operationID, "example.notes.save")
        XCTAssertEqual(intents[0].params["title"], .string("Current title"))
        XCTAssertEqual(intents[0].params["body"], .string("Current\nmultiline body"))
        XCTAssertEqual(intents[0].params["enabled"], .bool(false))
        XCTAssertEqual(intents[0].params["count"], .number(0))
        XCTAssertEqual(intents[0].params["selection"], .null)
    }

    func testRevisionStateAndLookupGuardsKeepFocusAndDisableStaleRoot() async throws {
        let lookup = DeferredOperationLookup()
        let oldLookupStarted = expectation(description: "old lookup started")
        let newLookupStarted = expectation(description: "new lookup started")
        let oldDocument = try decode(Self.refreshPanelJSON(
            revision: 1,
            state: "ready",
            operationID: "example.refresh.old",
            value: "old value"
        ))
        var intents: [PanelActionIntent] = []
        let renderer = PanelRenderer(
            document: oldDocument,
            operationLookup: { operationID in
                if operationID == "example.refresh.old" { oldLookupStarted.fulfill() }
                if operationID == "example.refresh.new" { newLookupStarted.fulfill() }
                return await lookup.wait(for: operationID)
            },
            onActionIntent: { intents.append($0) }
        )
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 480, height: 360),
            styleMask: [.titled],
            backing: .buffered,
            defer: false
        )
        window.contentViewController = renderer
        renderer.loadViewIfNeeded()
        await fulfillment(of: [oldLookupStarted], timeout: 2)

        var bodyInput = try XCTUnwrap(descendants(of: renderer.view).compactMap { $0 as? NSTextView }
            .first { $0.accessibilityLabel() == "Draft body" })
        XCTAssertTrue(window.makeFirstResponder(bodyInput))
        bodyInput.setSelectedRange(NSRange(location: 3, length: 2))

        let newerDocument = try decode(Self.refreshPanelJSON(
            revision: 2,
            state: "ready",
            operationID: "example.refresh.new",
            value: "server replacement"
        ))
        XCTAssertTrue(renderer.update(document: newerDocument))
        await fulfillment(of: [newLookupStarted], timeout: 2)
        bodyInput = try XCTUnwrap(descendants(of: renderer.view).compactMap { $0 as? NSTextView }
            .first { $0.accessibilityLabel() == "Draft body" })
        XCTAssertTrue(window.firstResponder === bodyInput)
        XCTAssertEqual(bodyInput.selectedRange(), NSRange(location: 3, length: 2))
        XCTAssertEqual(bodyInput.string, "server replacement")

        await lookup.resolve(
            "example.refresh.old",
            with: TrustedPanelOperationDescriptor(operationID: "example.refresh.old")
        )
        await Task.yield()
        var actionButton = try XCTUnwrap(descendants(of: renderer.view).compactMap { $0 as? NSButton }.first)
        XCTAssertFalse(actionButton.isEnabled)

        await lookup.resolve(
            "example.refresh.new",
            with: TrustedPanelOperationDescriptor(operationID: "example.refresh.new")
        )
        await Task.yield()
        actionButton = try XCTUnwrap(descendants(of: renderer.view).compactMap { $0 as? NSButton }.first)
        XCTAssertTrue(actionButton.isEnabled)

        XCTAssertFalse(renderer.update(document: oldDocument))
        XCTAssertEqual(renderer.document.revision, 2)
        XCTAssertTrue(window.firstResponder === bodyInput)

        let loading = try decode(Self.statePanelJSON(revision: 3, state: "loading"))
        XCTAssertTrue(renderer.update(document: loading))
        XCTAssertEqual(stateText(in: renderer), "Loading…")

        let empty = try decode(Self.statePanelJSON(
            revision: 4,
            state: "empty",
            message: "Nothing to show."
        ))
        XCTAssertTrue(renderer.update(document: empty))
        XCTAssertEqual(stateText(in: renderer), "Nothing to show.")

        let staleError = try decode(Self.refreshPanelJSON(
            revision: 5,
            state: "error",
            operationID: "example.refresh.new",
            value: "stale value",
            message: "Refresh failed."
        ))
        XCTAssertTrue(renderer.update(document: staleError))
        XCTAssertTrue(renderer.document.hasStaleRoot)
        XCTAssertEqual(stateText(in: renderer), "Refresh failed.")
        let staleViews = descendants(of: renderer.view)
        XCTAssertTrue(staleViews.compactMap { $0 as? NSButton }.allSatisfy { !$0.isEnabled })
        XCTAssertTrue(staleViews.compactMap { $0 as? NSTextView }.allSatisfy { !$0.isEditable })
        let staleTitleInputs = staleViews.compactMap { $0 as? NSTextField }
            .filter { $0.accessibilityLabel() == "Draft title" && $0.isBezeled }
        XCTAssertEqual(staleTitleInputs.count, 1)
        XCTAssertTrue(staleTitleInputs.allSatisfy { !$0.isEnabled && !$0.isEditable })

        let unavailable = try decode(Self.statePanelJSON(revision: 6, state: "unavailable"))
        XCTAssertTrue(renderer.update(document: unavailable))
        XCTAssertEqual(stateText(in: renderer), "This panel is unavailable.")
        XCTAssertTrue(intents.isEmpty)
    }

    private func decode(_ json: String) throws -> PanelDocument {
        try PanelDocumentCodec.decode(Data(json.utf8))
    }

    private func descendants(of view: NSView) -> [NSView] {
        view.subviews.flatMap { [$0] + descendants(of: $0) }
    }

    private func stateText(in renderer: PanelRenderer) -> String {
        descendants(of: renderer.view)
            .compactMap { $0 as? NSTextField }
            .first { $0.accessibilityLabel() == "Panel state" }?
            .stringValue ?? ""
    }

    private static let actionPanelJSON = """
    {
      "panel_id": "example.notes.editor",
      "revision": 4,
      "title": "Notes",
      "state": "ready",
      "root": {
        "id": "root",
        "kind": "stack",
        "children": [
          { "id": "url", "kind": "text", "value": "https://example.test/plain" },
          { "id": "title", "kind": "text_input", "value": "Initial title", "multiline": false, "label": "Title" },
          { "id": "body", "kind": "text_input", "value": "Initial body", "multiline": true, "label": "Body" },
          {
            "id": "save",
            "kind": "button",
            "label": "Save",
            "operation_id": "example.notes.save",
            "params": { "enabled": false, "count": 0, "selection": null },
            "field_bindings": { "title": "title", "body": "body" }
          },
          {
            "id": "missing",
            "kind": "button",
            "label": "Missing operation",
            "operation_id": "example.notes.missing"
          }
        ]
      }
    }
    """

    private static func refreshPanelJSON(
        revision: Int,
        state: String,
        operationID: String,
        value: String,
        message: String? = nil
    ) -> String {
        let messageField = message.map { #", "message": "\#($0)""# } ?? ""
        return """
        {
          "panel_id": "example.refresh.panel",
          "revision": \(revision),
          "title": "Refresh panel",
          "state": "\(state)"\(messageField),
          "root": {
            "id": "root",
            "kind": "stack",
            "children": [
              { "id": "title", "kind": "text_input", "value": "Title", "multiline": false, "label": "Draft title" },
              { "id": "draft", "kind": "text_input", "value": "\(value)", "multiline": true, "label": "Draft body" },
              { "id": "action", "kind": "button", "label": "Continue", "operation_id": "\(operationID)" }
            ]
          }
        }
        """
    }

    private static func statePanelJSON(
        revision: Int,
        state: String,
        message: String? = nil
    ) -> String {
        let messageField = message.map { #", "message": "\#($0)""# } ?? ""
        return """
        {
          "panel_id": "example.refresh.panel",
          "revision": \(revision),
          "title": "Refresh panel",
          "state": "\(state)"\(messageField)
        }
        """
    }
}

@MainActor
private final class DeferredOperationLookup {
    private var continuations: [String: CheckedContinuation<TrustedPanelOperationDescriptor?, Never>] = [:]

    func wait(for operationID: String) async -> TrustedPanelOperationDescriptor? {
        await withCheckedContinuation { continuation in
            continuations[operationID] = continuation
        }
    }

    func resolve(_ operationID: String, with descriptor: TrustedPanelOperationDescriptor?) {
        continuations.removeValue(forKey: operationID)?.resume(returning: descriptor)
    }
}

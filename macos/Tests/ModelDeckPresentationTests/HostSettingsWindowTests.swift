import XCTest
import AppKit
@testable import ModelDeckPresentation
import ModelDeckClient
import ModelDeckContracts

@MainActor
final class HostSettingsWindowTests: XCTestCase {
    func makeSnapshot() -> HostSettingsSnapshot {
        let field = HostFieldDescriptor.public(HostPublicFieldDescriptor(
            fieldID: "f1", label: "Cafe Name", key: "name",
            type: .string, valueState: .explicit, editability: .editable,
            applicationEffect: .immediate, value: .string("a")))
        let flag = HostFieldDescriptor.public(HostPublicFieldDescriptor(
            fieldID: "f2", label: "Enabled", key: "enabled",
            type: .boolean, valueState: .explicit, editability: .editable,
            applicationEffect: .immediate, value: .bool(true)))
        let secret = HostFieldDescriptor.secret(HostSecretFieldDescriptor(
            fieldID: "s1", label: "Token", type: .string,
            valueState: .explicit, editability: .editable,
            applicationEffect: .immediate, configured: true))
        return HostSettingsSnapshot(
            hostID: "h", documentID: "d", documentRevision: .present("base-1"),
            exists: true,
            target: HostSettingsTarget(displayName: "T", displayPath: "/t", scope: "user", writable: true),
            schemaProfile: HostSchemaProfile(schemaID: "s", schemaRevision: "1", hostVersion: "9", supportLevel: .supported),
            precedence: [], rawTOML: "a = 1",
            structured: HostStructuredDocument(sections: [
                HostSettingsSection(sectionID: "sec", title: "Sec", fields: [field, flag, secret])
            ]),
            contextRevision: "ctx-1")
    }

    func makeValidPreview() -> HostSettingsPreviewResult {
        HostSettingsPreviewResult(
            valid: true, validationLevel: .schema,
            candidateContentHash: "cand-1", candidateRawTOML: "a = 2",
            candidateStructured: makeSnapshot().structured, diagnostics: [],
            contextRevision: "ctx-2",
            preview: HostSettingsPreview(
                previewID: "p-1", baseContentHash: .present("base-1"),
                candidateContentHash: "cand-1", changed: true, diff: "removed-added",
                diffTruncated: false, changedFieldIDs: ["f1"],
                applicationEffects: [.immediate], protectedProjectionChanges: false))
    }

    func makeController(fake: FakeHostSettingsService, snapshot: HostSettingsSnapshot? = nil) -> (HostSettingsEditorViewController, HostSettingsEditorPresenter) {
        let presenter = HostSettingsEditorPresenter()
        let controller = HostSettingsEditorViewController(presenter: presenter)
        let exp = expectation(description: "start")
        fake.snapshot = snapshot ?? makeSnapshot()
        presenter.start(hostID: "h", service: fake) { _ in exp.fulfill() }
        wait(for: [exp], timeout: 5)
        controller.loadViewIfNeeded()
        controller.render()
        return (controller, presenter)
    }

    func testFormRendersRows() {
        let (controller, _) = makeController(fake: FakeHostSettingsService())
        XCTAssertEqual(controller.testRowViews.count, 3)
        XCTAssertEqual(controller.testRowViews.map { $0.fieldID }.sorted(), ["f1", "f2", "s1"])
    }

    func testSaveDisabledUntilValidPreview() {
        let fake = FakeHostSettingsService()
        fake.previewImpl = { _, _ in self.makeValidPreview() }
        let (controller, presenter) = makeController(fake: fake)
        XCTAssertFalse(controller.testSaveEnabled)
        presenter.editRaw("x = 1")
        let exp = expectation(description: "preview")
        presenter.requestPreview { _ in exp.fulfill() }
        wait(for: [exp], timeout: 5)
        controller.render()
        XCTAssertTrue(controller.testSaveEnabled)
    }

    func testSearchFiltersRows() {
        let (controller, _) = makeController(fake: FakeHostSettingsService())
        controller.testSearch("cafe")
        XCTAssertEqual(controller.testRowViews.map { $0.fieldID }, ["f1"])
        controller.testSearch("")
        XCTAssertEqual(controller.testRowViews.count, 3)
    }

    func testRawDraftRetainedAcrossRenders() {
        let (controller, presenter) = makeController(fake: FakeHostSettingsService())
        controller.testSwitchToRaw()
        XCTAssertTrue(controller.testIsRawVisible)
        presenter.editRaw("draft = true")
        controller.render()
        XCTAssertEqual(controller.testRawText, "draft = true")
        XCTAssertEqual(presenter.rawEditorText(), "draft = true")
    }

    func testSecretValueNeverReflected() {
        let (controller, presenter) = makeController(fake: FakeHostSettingsService())
        presenter.setSecretValue(fieldID: "s1", entryID: nil, value: .string("shh"))
        controller.render()
        let row = presenter.state.rows.first(where: { $0.fieldID == "s1" })
        XCTAssertNil(row?.displayValue)
        XCTAssertFalse(controller.testStatusText.contains("shh"))
    }

    func testSaveShortcutPreviewsThenSaves() {
        let fake = FakeHostSettingsService()
        fake.previewImpl = { _, _ in self.makeValidPreview() }
        fake.saveImpl = { params in
            HostSettingsSaveResult(saved: true, changed: true, documentID: params.documentID, previousContentHash: params.expectedContentHash, documentRevision: "rev-2", backup: nil, applicationEffects: [], contextRevision: "ctx-3")
        }
        let (controller, presenter) = makeController(fake: fake)
        presenter.editRaw("x = 1")
        let exp = expectation(description: "shortcut-save")
        controller.testSaveShortcut()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
            XCTAssertFalse(presenter.state.isDirty)
            exp.fulfill()
        }
        wait(for: [exp], timeout: 5)
    }

    func testAccessibilityLabelsPresent() {
        let (controller, _) = makeController(fake: FakeHostSettingsService())
        controller.testRowViews.forEach { row in
            XCTAssertFalse((row.accessibilityLabel() ?? "").isEmpty)
        }
        XCTAssertFalse(controller.testStatusText.isEmpty)
    }

    func testOffscreenSnapshotBestEffort() {
        let (controller, _) = makeController(fake: FakeHostSettingsService())
        let view = controller.view
        view.frame = NSRect(x: 0, y: 0, width: 720, height: 560)
        view.layoutSubtreeIfNeeded()
        guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
        view.cacheDisplay(in: view.bounds, to: rep)
        guard let png = rep.representation(using: .png, properties: [:]) else { return }
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("host-settings-window-\(UUID().uuidString).png")
        try? png.write(to: url)
        print("SNAPSHOT: \(url.path)")
    }
}

@MainActor
extension HostSettingsWindowTests {
    private func descendants(_ view: NSView) -> [NSView] {
        view.subviews.flatMap { [$0] + descendants($0) }
    }

    private func textInput(in row: HostSettingsFieldRowView) -> NSTextField {
        descendants(row).compactMap { $0 as? NSTextField }.first { $0.isEditable }!
    }

    func testTypedControlsRoundtripThroughValidation() {
        let examples: [(HostFieldType, JSONValue, String)] = [
            (.string, .string("a"), "a"),
            (.integer, .number(42), "42.0"),
            (.number, .number(1.25), "1.25"),
            (.enum, .number(2), "2"),
            (.enum, .bool(true), "true"),
            (.enum, .string("true"), "\"true\""),
            (.stringList, .array([.string("a,b"), .string(""), .string(" x ")]), "[\"a,b\",\"\",\" x \"]"),
        ]
        for (type, value, displayed) in examples {
            let fake = FakeHostSettingsService()
            var snapshot = makeSnapshot()
            snapshot.structured.sections[0].fields = [.public(HostPublicFieldDescriptor(
                fieldID: "f1", label: "Value", key: "value", type: type,
                valueState: .explicit, editability: .editable, applicationEffect: .immediate, value: value))]
            let (controller, presenter) = makeController(fake: fake, snapshot: snapshot)
            let input = textInput(in: controller.testRowViews[0])
            XCTAssertEqual(input.stringValue, displayed)
            input.sendAction(input.action!, to: input.target)
            fake.validateImpl = { params, result in
                XCTAssertEqual(params.draft, .structured([.set(fieldID: "f1", entryID: nil, value: value)]))
                return result
            }
            let checked = expectation(description: "validated \(type)")
            presenter.requestValidation { _ in checked.fulfill() }
            wait(for: [checked], timeout: 5)
        }
        let (controller, presenter) = makeController(fake: FakeHostSettingsService())
        let checkbox = descendants(controller.testRowViews.first { $0.fieldID == "f2" }!)
            .compactMap { $0 as? NSButton }.first { $0.title.isEmpty }!
        XCTAssertEqual(checkbox.state, .on)
        checkbox.performClick(nil)
        XCTAssertEqual(presenter.state.rows.first { $0.fieldID == "f2" }?.displayValue, "false")
    }

    func testInvalidNumericInputDoesNotPreviewOrReplaceDraft() {
        for (type, invalid) in [(HostFieldType.integer, "2.5"), (.number, "nan"), (.number, "inf")] {
            let fake = FakeHostSettingsService()
            var snapshot = makeSnapshot()
            snapshot.structured.sections[0].fields = [.public(HostPublicFieldDescriptor(
                fieldID: "f1", label: "Number", key: "number", type: type,
                valueState: .explicit, editability: .editable, applicationEffect: .immediate, value: .number(1)))]
            let (controller, presenter) = makeController(fake: fake, snapshot: snapshot)
            let input = textInput(in: controller.testRowViews[0])
            input.stringValue = invalid
            input.sendAction(input.action!, to: input.target)
            fake.previewImpl = { _, result in XCTFail("Invalid local input reached preview"); return result }
            controller.handleSaveShortcut()
            XCTAssertFalse(presenter.state.isDirty)
            XCTAssertEqual(input.stringValue, invalid)
            XCTAssertTrue(controller.testRowViews[0].hasInvalidInput)
        }
    }

    func testSecretChangeSurvivesRenderAndCommitNeverReflectsSecret() {
        let (controller, presenter) = makeController(fake: FakeHostSettingsService())
        let row = controller.testRowViews.first { $0.fieldID == "s1" }!
        descendants(row).compactMap { $0 as? NSButton }.first { $0.title == "Change…" }!.performClick(nil)
        controller.render()
        let secure = descendants(controller.testRowViews.first { $0.fieldID == "s1" }!)
            .compactMap { $0 as? NSSecureTextField }.first!
        secure.stringValue = "private fixture"
        controller.render()
        XCTAssertEqual(secure.stringValue, "private fixture")
        secure.sendAction(secure.action!, to: secure.target)
        XCTAssertEqual(secure.stringValue, "")
        XCTAssertTrue(presenter.state.rows.first { $0.fieldID == "s1" }!.hasPendingSecretChange)
        XCTAssertNil(presenter.state.rows.first { $0.fieldID == "s1" }!.displayValue)
        XCTAssertFalse(descendants(controller.view).compactMap { $0 as? NSTextField }.contains { $0.stringValue.contains("private fixture") })
    }

    func testOffscreenWindowLayoutRawFocusAndActualSaveKeyEquivalent() throws {
        let fake = FakeHostSettingsService()
        fake.snapshot = makeSnapshot()
        let presenter = HostSettingsEditorPresenter()
        let loaded = expectation(description: "loaded")
        presenter.start(hostID: "h", service: fake) { _ in loaded.fulfill() }
        wait(for: [loaded], timeout: 5)
        let owner = HostSettingsWindowController(hostLabel: "Fixture", presenter: presenter)
        let window = owner.window!
        let controller = window.contentViewController as! HostSettingsEditorViewController
        controller.loadViewIfNeeded()
        controller.view.layoutSubtreeIfNeeded()
        XCTAssertGreaterThanOrEqual(controller.view.bounds.width, 720)
        for row in controller.testRowViews {
            XCTAssertGreaterThan(row.visibleRect.width, 500)
            XCTAssertGreaterThan(row.visibleRect.height, 20)
        }
        let image = try XCTUnwrap(controller.view.bitmapImageRepForCachingDisplay(in: controller.view.bounds))
        controller.view.cacheDisplay(in: controller.view.bounds, to: image)
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("host-settings-repaired-\(UUID().uuidString).png")
        try XCTUnwrap(image.representation(using: .png, properties: [:])).write(to: url)
        print("REPAIRED SNAPSHOT: \(url.path)")
        controller.testSwitchToRaw()
        controller.view.layoutSubtreeIfNeeded()
        let raw = descendants(controller.view).compactMap { $0 as? NSTextView }.first { $0.accessibilityLabel() == "Raw TOML editor" }!
        XCTAssertGreaterThan(raw.frame.width, 500)
        XCTAssertTrue(window.makeFirstResponder(raw))
        raw.string = "typing = true"
        raw.setSelectedRange(NSRange(location: 4, length: 0))
        controller.textDidChange(Notification(name: NSText.didChangeNotification, object: raw))
        controller.render()
        XCTAssertEqual(presenter.rawEditorText(), "typing = true")
        XCTAssertEqual(raw.selectedRange(), NSRange(location: 4, length: 0))
        XCTAssertTrue(window.firstResponder === raw)
        let saved = expectation(description: "actual shortcut saved")
        fake.saveImpl = { params in
            saved.fulfill()
            return HostSettingsSaveResult(saved: true, changed: true, documentID: params.documentID, previousContentHash: params.expectedContentHash, documentRevision: "rev-2", backup: nil, applicationEffects: [], contextRevision: "ctx-3")
        }
        let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: .command, timestamp: 0, windowNumber: window.windowNumber, context: nil, characters: "s", charactersIgnoringModifiers: "s", isARepeat: false, keyCode: 1)!
        XCTAssertTrue(window.performKeyEquivalent(with: event))
        wait(for: [saved], timeout: 5)
    }
}

@MainActor
extension HostSettingsWindowTests {
    func testCommandSCommitsFocusedTextBeforePreview() {
        let fake = FakeHostSettingsService()
        let (_, presenter) = makeController(fake: fake)
        let owner = HostSettingsWindowController(hostLabel: "Fixture", presenter: presenter)
        let window = owner.window!
        let controller = window.contentViewController as! HostSettingsEditorViewController
        controller.loadViewIfNeeded()
        controller.view.layoutSubtreeIfNeeded()
        let field = textInput(in: controller.testRowViews.first { $0.fieldID == "f1" }!)
        XCTAssertTrue(window.makeFirstResponder(field))
        let editor = field.currentEditor()!
        editor.string = "edited without Return"
        controller.render()
        XCTAssertTrue(field.currentEditor() === editor)
        XCTAssertEqual(editor.string, "edited without Return")
        let previewed = expectation(description: "focused text previewed")
        fake.previewImpl = { params, result in
            XCTAssertEqual(params.draft, .structured([.set(fieldID: "f1", entryID: nil, value: .string("edited without Return"))]))
            previewed.fulfill()
            return result
        }
        let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: .command, timestamp: 0, windowNumber: window.windowNumber, context: nil, characters: "s", charactersIgnoringModifiers: "s", isARepeat: false, keyCode: 1)!
        XCTAssertTrue(window.performKeyEquivalent(with: event))
        wait(for: [previewed], timeout: 5)
    }
}

@MainActor
extension HostSettingsWindowTests {
    func testEnumPickerUsesTypedChoicesEvenWithDuplicateLabels() {
        let values: [JSONValue] = [.number(2), .bool(true), .string("true")]
        let choices = values.map { HostConstraintChoice(value: $0, label: "Same label") }
        let fake = FakeHostSettingsService()
        var snapshot = makeSnapshot()
        snapshot.structured.sections[0].fields = [.public(HostPublicFieldDescriptor(
            fieldID: "f1", label: "Choice", key: "choice", type: .enum,
            valueState: .explicit, editability: .editable, applicationEffect: .immediate,
            value: values[0], constraints: HostConstraints(choices: choices)))]
        let (controller, presenter) = makeController(fake: fake, snapshot: snapshot)
        XCTAssertEqual(presenter.state.rows[0].enumValue, values[0])
        XCTAssertEqual(presenter.state.rows[0].enumChoices, choices)
        for (index, value) in values.enumerated() {
            let picker = descendants(controller.testRowViews[0]).compactMap { $0 as? NSPopUpButton }.first!
            XCTAssertEqual(picker.numberOfItems, 3)
            picker.selectItem(withTag: index)
            picker.sendAction(picker.action!, to: picker.target)
            XCTAssertEqual(presenter.state.rows[0].enumValue, value)
            fake.validateImpl = { params, result in
                XCTAssertEqual(params.draft, .structured([.set(fieldID: "f1", entryID: nil, value: value)]))
                return result
            }
            let validated = expectation(description: "typed enum choice")
            presenter.requestValidation { _ in validated.fulfill() }
            wait(for: [validated], timeout: 5)
            controller.render()
        }
    }

    func testEnumUnknownChoiceIsNotSilentlyReplacedAndJSONRejectsNonScalars() {
        let fake = FakeHostSettingsService()
        var snapshot = makeSnapshot()
        snapshot.structured.sections[0].fields = [.public(HostPublicFieldDescriptor(
            fieldID: "f1", label: "Choice", key: "choice", type: .enum,
            valueState: .explicit, editability: .editable, applicationEffect: .immediate,
            value: .number(3), constraints: HostConstraints(choices: [.init(value: .number(2), label: "Two")])))]
        let (controller, presenter) = makeController(fake: fake, snapshot: snapshot)
        let picker = descendants(controller.testRowViews[0]).compactMap { $0 as? NSPopUpButton }.first!
        XCTAssertEqual(picker.selectedTag(), -1)
        picker.sendAction(picker.action!, to: picker.target)
        XCTAssertFalse(presenter.state.isDirty)
        XCTAssertEqual(presenter.state.rows[0].enumValue, .number(3))
        snapshot.structured.sections[0].fields = [.public(HostPublicFieldDescriptor(
            fieldID: "f1", label: "Choice", key: "choice", type: .enum,
            valueState: .explicit, editability: .editable, applicationEffect: .immediate, value: .bool(true)))]
        let (textController, textPresenter) = makeController(fake: FakeHostSettingsService(), snapshot: snapshot)
        for invalid in ["unquoted", "null", "[]", "{}", "NaN"] {
            let field = textInput(in: textController.testRowViews[0])
            field.stringValue = invalid
            field.sendAction(field.action!, to: field.target)
            XCTAssertTrue(textController.testRowViews[0].hasInvalidInput)
            XCTAssertFalse(textPresenter.state.isDirty)
            XCTAssertEqual(textPresenter.state.rows[0].enumValue, .bool(true))
        }
        XCTAssertNil(presenter.state.rows.first { $0.isSecret }?.enumValue)
    }
}

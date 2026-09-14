import XCTest
@testable import ModelDeckPresentation
import ModelDeckContracts

// Pure Swift tests for `PanelDocument` values + the semantic validator
// exposed by `PanelDocumentCodec.decode(_:)`. No AppKit, no kernel/provider
// reach-through: every test constructs a raw payload (or a JSON string) and
// asserts on the validated `PanelDocument` or the typed `PanelDocumentError`.
//
// Two real-fixture happy paths cover both panel shapes the Session Notebook
// uses (list of `text` rows + per-row `button`; editor with a `text_input`
// bound to a save `button`). The negative suite covers the bounds, the
// shape rejects (unknown kind, unknown field, wrong scalar), the binding
// semantic checks, the stale-state path, the accessibility label requirement,
// and the raw-payload rejections.

final class PanelDocumentTests: XCTestCase {

    // MARK: - Real fixtures (happy path)

    func testRealFixture_listPanelWithPerRowButtons_decodesAndExposesTree() throws {
        let payload = Self.listPanelJSON.data(using: .utf8)!
        let doc = try PanelDocumentCodec.decode(payload)

        XCTAssertEqual(doc.panelID, "example.notebook.list")
        XCTAssertEqual(doc.revision, 2)
        XCTAssertEqual(doc.title, "Session Notebook")
        XCTAssertEqual(doc.state, .ready)
        XCTAssertNil(doc.message)
        XCTAssertTrue(doc.isReady)
        XCTAssertFalse(doc.hasStaleRoot)

        guard let root = doc.root, case let .stack(stack) = root else {
            return XCTFail("expected stack root")
        }
        XCTAssertEqual(stack.id.rawValue, "list_root")
        XCTAssertEqual(stack.children.count, 4)

        guard case let .text(row1) = stack.children[0] else {
            return XCTFail("expected text row_1")
        }
        XCTAssertEqual(row1.value, "Sprint planning")

        guard case let .text(row2) = stack.children[2] else {
            return XCTFail("expected text row_2")
        }
        XCTAssertEqual(row2.value, "Design review")

        guard case let .button(btn1) = stack.children[1] else {
            return XCTFail("expected button row_1_btn")
        }
        XCTAssertEqual(btn1.label, "Resume")
        XCTAssertEqual(btn1.operationID, "example.notebook.resume")
        XCTAssertEqual(btn1.params.count, 1)
        XCTAssertEqual(btn1.params["notebook_id"], .string("abc"))
        XCTAssertTrue(btn1.fieldBindings.isEmpty)

        XCTAssertEqual(root.totalNodeCount(), 5)
        XCTAssertEqual(root.textInputCount(), 0)
    }

    func testRealFixture_editorPanelWithTextInputBinding_decodesAndExposesTree() throws {
        let payload = Self.editorPanelJSON.data(using: .utf8)!
        let doc = try PanelDocumentCodec.decode(payload)

        XCTAssertEqual(doc.panelID, "example.notebook.editor")
        XCTAssertEqual(doc.revision, 5)
        XCTAssertEqual(doc.state, .ready)

        guard let root = doc.root, case let .stack(stack) = root else {
            return XCTFail("expected stack root")
        }
        XCTAssertEqual(stack.children.count, 2)

        guard case let .textInput(input) = stack.children[0] else {
            return XCTFail("expected text_input draft")
        }
        XCTAssertEqual(input.id.rawValue, "draft")
        XCTAssertEqual(input.value, "Initial draft...")
        XCTAssertTrue(input.multiline)
        XCTAssertEqual(input.label, "Draft notes")

        guard case let .button(save) = stack.children[1] else {
            return XCTFail("expected save button")
        }
        XCTAssertEqual(save.operationID, "example.notebook.save")
        XCTAssertEqual(save.fieldBindings.count, 1)
        XCTAssertEqual(save.fieldBindings["body"]?.rawValue, "draft")
        XCTAssertTrue(save.params.keys.contains("notebook_id"))
    }

    // MARK: - Raw payload rejections

    func testDecode_emptyData_throwsPayloadEmpty() {
        XCTAssertThrowsError(try PanelDocumentCodec.decode(Data())) { error in
            XCTAssertEqual(error as? PanelDocumentError, .payloadEmpty)
        }
    }

    func testDecode_payloadLargerThan1MiB_throwsPayloadTooLarge() {
        let oversized = Data(repeating: 0x20, count: PanelDocumentCodec.maxRawBytes + 1)
        XCTAssertThrowsError(try PanelDocumentCodec.decode(oversized)) { error in
            XCTAssertEqual(error as? PanelDocumentError, .payloadTooLarge)
        }
    }

    func testDecode_nonUTF8Bytes_throwsInvalidUTF8() {
        let bytes: [UInt8] = [0xFF, 0xFE, 0xFD, 0x00, 0x80]
        let data = Data(bytes)
        XCTAssertThrowsError(try PanelDocumentCodec.decode(data)) { error in
            XCTAssertEqual(error as? PanelDocumentError, .invalidUTF8)
        }
    }

    func testDecode_notJSON_throwsInvalidJSON() {
        XCTAssertThrowsError(
            try PanelDocumentCodec.decode("this is not json".data(using: .utf8)!)
        ) { error in
            XCTAssertEqual(error as? PanelDocumentError, .invalidJSON)
        }
    }

    func testDecode_jsonArrayRoot_throwsRootNotObject() {
        let payload = "[1, 2, 3]".data(using: .utf8)!
        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            XCTAssertEqual(error as? PanelDocumentError, .rootNotObject)
        }
    }

    func testDecode_jsonScalarRoot_throwsRootNotObject() {
        let payload = "42".data(using: .utf8)!
        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            XCTAssertEqual(error as? PanelDocumentError, .rootNotObject)
        }
    }

    func testDecode_missingRequiredField_throwsMissingField() {
        let payload = """
        { "panel_id": "example.x", "revision": 1, "title": "t" }
        """.data(using: .utf8)!
        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            XCTAssertEqual(error as? PanelDocumentError, .missingField("state"))
        }
    }

    func testDecode_unknownPanelLevelField_throwsUnknownField() {
        let payload = """
        {
          "panel_id": "example.x",
          "revision": 1,
          "title": "t",
          "state": "ready",
          "color": "red"
        }
        """.data(using: .utf8)!
        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            XCTAssertEqual(error as? PanelDocumentError, .unknownField)
        }
    }

    func testDecode_unknownNodeKind_throwsInvalidNodeKind() {
        let payload = """
        {
          "panel_id": "example.x",
          "revision": 1,
          "title": "t",
          "state": "ready",
          "root": { "id": "root", "kind": "tree", "children": [
            { "id": "leaf", "kind": "leaf", "value": "x" }
          ]}
        }
        """.data(using: .utf8)!
        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            XCTAssertEqual(error as? PanelDocumentError, .invalidNodeKind)
        }
    }

    func testDecode_kindSpecificFieldsAreAcceptedAfterKindDispatch() throws {
        let payload = """
        {
          "panel_id": "example.x", "revision": 1, "title": "t", "state": "ready",
          "root": { "id": "root", "kind": "stack", "children": [
            { "id": "text", "kind": "text", "value": "copy" },
            { "id": "input", "kind": "text_input", "value": "draft", "multiline": false, "label": "Draft" },
            { "id": "button", "kind": "button", "label": "Save", "operation_id": "example.save" }
          ]}
        }
        """.data(using: .utf8)!

        let document = try PanelDocumentCodec.decode(payload)
        XCTAssertEqual(document.root?.totalNodeCount(), 4)
    }

    func testDecode_unknownKindSpecificField_throwsUnknownField() {
        let payload = """
        {
          "panel_id": "example.x", "revision": 1, "title": "t", "state": "ready",
          "root": { "id": "text", "kind": "text", "value": "copy", "color": "red" }
        }
        """.data(using: .utf8)!

        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            XCTAssertEqual(error as? PanelDocumentError, .unknownField)
        }
    }

    // MARK: - Bounds

    func testDecode_depthAbove16_throwsDepthExceeded() {
        // Build a payload whose root stack nests 17 stacks. Each child stack
        // contains one child stack, and the leaf is a single text node so the
        // walker can reach the limit without bailing on an empty-children error
        // first.
        var nested = #"{ "id": "leaf", "kind": "text", "value": "x" }"#
        for i in stride(from: 15, through: 0, by: -1) {
            nested = """
            { "id": "s\(i)", "kind": "stack", "children": [\(nested)] }
            """
        }
        let payload = """
        {
          "panel_id": "example.x", "revision": 1, "title": "t", "state": "ready",
          "root": \(nested)
        }
        """.data(using: .utf8)!
        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            XCTAssertEqual(error as? PanelDocumentError, .depthExceeded)
        }
    }

    func testDecode_exactlyMaxDepth_succeeds() throws {
        // Depth 16: 15 nested stacks wrapping a text leaf = depth 16 total.
        var nested = #"{ "id": "leaf", "kind": "text", "value": "x" }"#
        for i in stride(from: 14, through: 0, by: -1) {
            nested = """
            { "id": "s\(i)", "kind": "stack", "children": [\(nested)] }
            """
        }
        let payload = """
        {
          "panel_id": "example.x", "revision": 1, "title": "t", "state": "ready",
          "root": \(nested)
        }
        """.data(using: .utf8)!
        let doc = try PanelDocumentCodec.decode(payload)
        XCTAssertNotNil(doc.root)
    }

    func testDecode_nodeCountAbove256_throwsNodeCountExceeded() {
        // 257+ total nodes. Each stack caps children at 64, so use 4 nested
        // stacks each holding 64 text leaves: 1 root + 4 stacks + 4*64 leaves
        // = 261 total nodes.
        let leaves = (0..<64).map { "{ \"id\": \"l\($0)\", \"kind\": \"text\", \"value\": \"x\" }" }.joined(separator: ",")
        let leaves2 = (64..<128).map { "{ \"id\": \"l\($0)\", \"kind\": \"text\", \"value\": \"x\" }" }.joined(separator: ",")
        let leaves3 = (128..<192).map { "{ \"id\": \"l\($0)\", \"kind\": \"text\", \"value\": \"x\" }" }.joined(separator: ",")
        let leaves4 = (192..<256).map { "{ \"id\": \"l\($0)\", \"kind\": \"text\", \"value\": \"x\" }" }.joined(separator: ",")
        let fifthLeaf = "{ \"id\": \"overflow\", \"kind\": \"text\", \"value\": \"x\" }"
        let payload = """
        {
          "panel_id": "example.x", "revision": 1, "title": "t", "state": "ready",
          "root": { "id": "root", "kind": "stack", "children": [
            { "id": "g0", "kind": "stack", "children": [\(leaves)] },
            { "id": "g1", "kind": "stack", "children": [\(leaves2)] },
            { "id": "g2", "kind": "stack", "children": [\(leaves3)] },
            { "id": "g3", "kind": "stack", "children": [\(leaves4)] },
            { "id": "g4", "kind": "stack", "children": [\(fifthLeaf)] }
          ]}
        }
        """.data(using: .utf8)!
        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            XCTAssertEqual(error as? PanelDocumentError, .nodeCountExceeded)
        }
    }
    func testDecode_emptyChildren_throwsCollectionTooSmall() {
        let payload = """
        {
          "panel_id": "example.x", "revision": 1, "title": "t", "state": "ready",
          "root": { "id": "root", "kind": "stack", "children": [] }
        }
        """.data(using: .utf8)!
        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            if case let .collectionTooSmall(field, min) = error as? PanelDocumentError {
                XCTAssertEqual(field, "stack.children")
                XCTAssertEqual(min, 1)
            } else {
                XCTFail("expected collectionTooSmall, got \(error)")
            }
        }
    }

    func testDecode_titleOver256Scalars_throwsStringLengthOutOfRange() {
        let longTitle = String(repeating: "x", count: 257)
        let payload = """
        {
          "panel_id": "example.x", "revision": 1, "title": "\(longTitle)", "state": "loading"
        }
        """.data(using: .utf8)!
        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            XCTAssertEqual(
                error as? PanelDocumentError,
                .stringLengthOutOfRange(field: "title", min: 1, max: 256)
            )
        }
    }

    func testDecode_titleAt256NonASCIIScalars_succeeds() throws {
        let title = String(repeating: "é", count: 256)
        XCTAssertEqual(title.unicodeScalars.count, 256)
        XCTAssertGreaterThan(title.utf8.count, 256)
        let payload = """
        {
          "panel_id": "example.x", "revision": 1, "title": "\(title)", "state": "loading"
        }
        """.data(using: .utf8)!

        let document = try PanelDocumentCodec.decode(payload)
        XCTAssertEqual(document.title, title)
    }

    func testDecode_titleOver256ScalarsInFewGraphemes_throwsStringLengthOutOfRange() {
        let title = String(repeating: "👨‍👩‍👧‍👦", count: 37)
        XCTAssertLessThan(title.count, 256)
        XCTAssertGreaterThan(title.unicodeScalars.count, 256)
        let payload = """
        {
          "panel_id": "example.x", "revision": 1, "title": "\(title)", "state": "loading"
        }
        """.data(using: .utf8)!

        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            XCTAssertEqual(
                error as? PanelDocumentError,
                .stringLengthOutOfRange(field: "title", min: 1, max: 256)
            )
        }
    }

    func testDecode_revisionNegative_throwsRevisionOutOfRange() {
        let payload = """
        {
          "panel_id": "example.x", "revision": -1, "title": "t", "state": "loading"
        }
        """.data(using: .utf8)!
        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            XCTAssertEqual(error as? PanelDocumentError, .revisionOutOfRange)
        }
    }

    func testDecode_revisionFractional_throwsInvalidJSON() {
        let payload = """
        {
          "panel_id": "example.x", "revision": 1.5, "title": "t", "state": "loading"
        }
        """.data(using: .utf8)!
        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            XCTAssertEqual(error as? PanelDocumentError, .invalidJSON)
        }
    }

    func testDecode_revisionWithIntegralDecimalOrExponent_succeeds() throws {
        for revisionJSON in ["1.0", "1e0"] {
            let payload = """
            {
              "panel_id": "example.x", "revision": \(revisionJSON), "title": "t", "state": "loading"
            }
            """.data(using: .utf8)!

            let document = try PanelDocumentCodec.decode(payload)
            XCTAssertEqual(document.revision, 1)
        }
    }

    func testDecode_revisionAboveIntMax_throwsRevisionOutOfRange() {
        let payload = """
        {
          "panel_id": "example.x", "revision": 9223372036854775808, "title": "t", "state": "loading"
        }
        """.data(using: .utf8)!

        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            XCTAssertEqual(error as? PanelDocumentError, .revisionOutOfRange)
        }
    }

    func testDecode_revisionMaxBoundary_succeeds() throws {
        // IEEE-754 safe integer maximum (2^53 - 1). The shared upper
        // bound must be accepted exactly and without coercion in the
        // native panel codec, mirroring
        // `contracts/common/types.schema.json#/definitions/revision`.
        let revision = 9_007_199_254_740_991
        let payload = """
        {
          "panel_id": "example.x", "revision": \(revision), "title": "t", "state": "loading"
        }
        """.data(using: .utf8)!

        let document = try PanelDocumentCodec.decode(payload)
        XCTAssertEqual(document.revision, revision)
    }

    func testDecode_revisionAboveMaxBoundary_throwsRevisionOutOfRange() {
        // One above 2^53 - 1 must reject as out-of-range. The native
        // codec must not silently clamp to the bound.
        let payload = """
        {
          "panel_id": "example.x", "revision": 9007199254740992, "title": "t", "state": "loading"
        }
        """.data(using: .utf8)!

        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            XCTAssertEqual(error as? PanelDocumentError, .revisionOutOfRange)
        }
    }

    // MARK: - Node-id and panel-id format

    func testDecode_invalidNodeIDFormat_throwsInvalidNodeIDFormat() {
        // Node id starts with a digit; pattern requires a lowercase letter.
        let payload = """
        {
          "panel_id": "example.x", "revision": 1, "title": "t", "state": "ready",
          "root": { "id": "1bad", "kind": "text", "value": "x" }
        }
        """.data(using: .utf8)!
        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            XCTAssertEqual(error as? PanelDocumentError, .invalidNodeIDFormat)
        }
    }

    func testDecode_nonASCIINodeID_throwsInvalidNodeIDFormat() {
        for invalidID in ["rooté", "root١"] {
            let payload = """
            {
              "panel_id": "example.x", "revision": 1, "title": "t", "state": "ready",
              "root": { "id": "\(invalidID)", "kind": "text", "value": "x" }
            }
            """.data(using: .utf8)!
            XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
                XCTAssertEqual(error as? PanelDocumentError, .invalidNodeIDFormat)
            }
        }
    }

    func testDecode_invalidPanelIDFormat_throwsInvalidPanelIDFormat() {
        // Panel id lacks the required dot separator.
        let payload = """
        {
          "panel_id": "nodot", "revision": 1, "title": "t", "state": "loading"
        }
        """.data(using: .utf8)!
        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            XCTAssertEqual(error as? PanelDocumentError, .invalidPanelIDFormat)
        }
    }

    func testDecode_invalidOperationIDFormat_throwsInvalidOperationIDFormat() {
        let payload = """
        {
          "panel_id": "example.x", "revision": 1, "title": "t", "state": "ready",
          "root": { "id": "root", "kind": "stack", "children": [
            { "id": "btn", "kind": "button", "label": "Go",
              "operation_id": "no-dots-here" }
          ]}
        }
        """.data(using: .utf8)!
        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            XCTAssertEqual(
                error as? PanelDocumentError,
                .invalidOperationIDFormat("button.operation_id")
            )
        }
    }

    func testDecode_duplicateNodeID_throwsDuplicateNodeID() {
        let payload = """
        {
          "panel_id": "example.x", "revision": 1, "title": "t", "state": "ready",
          "root": { "id": "root", "kind": "stack", "children": [
            { "id": "dup", "kind": "text", "value": "a" },
            { "id": "dup", "kind": "text", "value": "b" }
          ]}
        }
        """.data(using: .utf8)!
        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            XCTAssertEqual(error as? PanelDocumentError, .duplicateNodeID)
        }
    }

    // MARK: - Bindings (semantic pass)

    func testDecode_bindingTargetNotInTree_throwsBindingTargetNotFound() {
        let payload = """
        {
          "panel_id": "example.x", "revision": 1, "title": "t", "state": "ready",
          "root": { "id": "root", "kind": "stack", "children": [
            { "id": "draft", "kind": "text_input", "value": "", "multiline": true, "label": "Draft" },
            { "id": "save", "kind": "button", "label": "Save",
              "operation_id": "example.save",
              "field_bindings": { "body": "ghost" } }
          ]}
        }
        """.data(using: .utf8)!
        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            XCTAssertEqual(error as? PanelDocumentError, .bindingTargetNotFound)
        }
    }

    func testDecode_bindingTargetIsNotTextInput_throwsBindingTargetNotTextInput() {
        let payload = """
        {
          "panel_id": "example.x", "revision": 1, "title": "t", "state": "ready",
          "root": { "id": "root", "kind": "stack", "children": [
            { "id": "label_text", "kind": "text", "value": "hi" },
            { "id": "save", "kind": "button", "label": "Save",
              "operation_id": "example.save",
              "field_bindings": { "body": "label_text" } }
          ]}
        }
        """.data(using: .utf8)!
        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            XCTAssertEqual(error as? PanelDocumentError, .bindingTargetNotTextInput)
        }
    }

    func testDecode_paramKeyCollidesWithBindingKey_throwsParamsBindingsCollision() {
        let payload = """
        {
          "panel_id": "example.x", "revision": 1, "title": "t", "state": "ready",
          "root": { "id": "root", "kind": "stack", "children": [
            { "id": "draft", "kind": "text_input", "value": "", "multiline": true, "label": "Draft" },
            { "id": "save", "kind": "button", "label": "Save",
              "operation_id": "example.save",
              "params": { "body": "static" },
              "field_bindings": { "body": "draft" } }
          ]}
        }
        """.data(using: .utf8)!
        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            XCTAssertEqual(error as? PanelDocumentError, .paramsBindingsCollision)
        }
    }

    func testDecode_textInputLabelMissing_throwsMissingField() {
        let payload = """
        {
          "panel_id": "example.x", "revision": 1, "title": "t", "state": "ready",
          "root": { "id": "root", "kind": "stack", "children": [
            { "id": "draft", "kind": "text_input", "value": "", "multiline": true }
          ]}
        }
        """.data(using: .utf8)!
        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            XCTAssertEqual(error as? PanelDocumentError, .missingField("label"))
        }
    }

    func testDecode_textInputLabelEmpty_throwsStringLengthOutOfRange() {
        let payload = """
        {
          "panel_id": "example.x", "revision": 1, "title": "t", "state": "ready",
          "root": { "id": "root", "kind": "stack", "children": [
            { "id": "draft", "kind": "text_input", "value": "", "multiline": true, "label": "" }
          ]}
        }
        """.data(using: .utf8)!
        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            XCTAssertEqual(
                error as? PanelDocumentError,
                .stringLengthOutOfRange(field: "text_input.label", min: 1, max: 256)
            )
        }
    }

    func testDecode_numericZeroForMultiline_throwsInvalidJSON() {
        let payload = """
        {
          "panel_id": "example.x", "revision": 1, "title": "t", "state": "ready",
          "root": { "id": "draft", "kind": "text_input", "value": "", "multiline": 0, "label": "Draft" }
        }
        """.data(using: .utf8)!

        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            XCTAssertEqual(error as? PanelDocumentError, .invalidJSON)
        }
    }

    // MARK: - Stale state

    func testDecode_loadingStateWithoutRoot_succeedsAndIsNotReady() throws {
        let payload = """
        {
          "panel_id": "example.x", "revision": 1, "title": "t",
          "state": "loading",
          "message": "Working..."
        }
        """.data(using: .utf8)!
        let doc = try PanelDocumentCodec.decode(payload)
        XCTAssertEqual(doc.state, .loading)
        XCTAssertFalse(doc.isReady)
        XCTAssertNil(doc.root)
        XCTAssertFalse(doc.hasStaleRoot)
    }

    func testDecode_loadingStateWithRetainedRoot_exposesHasStaleRootTrue() throws {
        let payload = """
        {
          "panel_id": "example.x", "revision": 2, "title": "t",
          "state": "loading",
          "message": "Refreshing",
          "root": { "id": "old", "kind": "text", "value": "stale" }
        }
        """.data(using: .utf8)!
        let doc = try PanelDocumentCodec.decode(payload)
        XCTAssertEqual(doc.state, .loading)
        XCTAssertFalse(doc.isReady)
        XCTAssertNotNil(doc.root)
        XCTAssertTrue(doc.hasStaleRoot)
    }

    func testDecode_readyStateWithoutRoot_throwsRootRequiredForReady() {
        let payload = """
        {
          "panel_id": "example.x", "revision": 1, "title": "t", "state": "ready"
        }
        """.data(using: .utf8)!
        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            XCTAssertEqual(error as? PanelDocumentError, .rootRequiredForReady)
        }
    }

    func testDecode_readyStateWithExplicitNullRoot_throwsInvalidJSON() {
        let payload = """
        {
          "panel_id": "example.x", "revision": 1, "title": "t", "state": "ready",
          "root": null
        }
        """.data(using: .utf8)!
        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            XCTAssertEqual(error as? PanelDocumentError, .invalidJSON)
        }
    }

    func testDecode_nonReadyStateWithExplicitNullRoot_throwsInvalidJSON() {
        let payload = """
        {
          "panel_id": "example.x", "revision": 1, "title": "t", "state": "loading",
          "root": null
        }
        """.data(using: .utf8)!
        XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
            XCTAssertEqual(error as? PanelDocumentError, .invalidJSON)
        }
    }

    // MARK: - Static JSON params (preserved through JSONValue)

    func testDecode_buttonParamsPreserveNestedJSONValue() throws {
        let payload = """
        {
          "panel_id": "example.x", "revision": 1, "title": "t", "state": "ready",
          "root": { "id": "root", "kind": "stack", "children": [
            { "id": "draft", "kind": "text_input", "value": "v", "multiline": true, "label": "L" },
            { "id": "save", "kind": "button", "label": "Save",
              "operation_id": "example.save",
              "params": {
                "id": "abc",
                "count": 3,
                "enabled": true,
                "tags": ["draft", "weekly"],
                "config": { "tier": "pro", "score": null }
              },
              "field_bindings": { "body": "draft" } }
          ]}
        }
        """.data(using: .utf8)!
        let doc = try PanelDocumentCodec.decode(payload)
        guard let root = doc.root, case let .stack(stack) = root else {
            return XCTFail("expected stack root")
        }
        guard case let .button(save) = stack.children[1] else {
            return XCTFail("expected save button")
        }
        XCTAssertEqual(save.params["id"], .string("abc"))
        XCTAssertEqual(save.params["count"], .number(3))
        XCTAssertEqual(save.params["enabled"], .bool(true))
        XCTAssertEqual(save.params["tags"], .array([.string("draft"), .string("weekly")]))
        XCTAssertEqual(save.params["config"], .object([
            "tier": .string("pro"),
            "score": .null
        ]))
    }

    func testDecode_errorDescriptionsDoNotEchoSourceControlledNamesOrIDs() {
        let unknownKey = "private_unknown_key"
        let unknownKind = "private_unknown_kind"
        let duplicateID = "private_duplicate_id"
        let bindingName = "private_binding_name"
        let missingTarget = "private_missing_target"
        let collisionName = "private_collision_name"

        let payloads: [(Data, [String])] = [
            (
                """
                { "panel_id": "example.x", "revision": 1, "title": "t", "state": "loading", "\(unknownKey)": true }
                """.data(using: .utf8)!,
                [unknownKey]
            ),
            (
                """
                {
                  "panel_id": "example.x", "revision": 1, "title": "t", "state": "ready",
                  "root": { "id": "root", "kind": "\(unknownKind)" }
                }
                """.data(using: .utf8)!,
                [unknownKind]
            ),
            (
                """
                {
                  "panel_id": "example.x", "revision": 1, "title": "t", "state": "ready",
                  "root": { "id": "root", "kind": "stack", "children": [
                    { "id": "\(duplicateID)", "kind": "text", "value": "a" },
                    { "id": "\(duplicateID)", "kind": "text", "value": "b" }
                  ]}
                }
                """.data(using: .utf8)!,
                [duplicateID]
            ),
            (
                """
                {
                  "panel_id": "example.x", "revision": 1, "title": "t", "state": "ready",
                  "root": { "id": "save", "kind": "button", "label": "Save", "operation_id": "example.save",
                    "field_bindings": { "\(bindingName)": "\(missingTarget)" } }
                }
                """.data(using: .utf8)!,
                [bindingName, missingTarget]
            ),
            (
                """
                {
                  "panel_id": "example.x", "revision": 1, "title": "t", "state": "ready",
                  "root": { "id": "root", "kind": "stack", "children": [
                    { "id": "input", "kind": "text_input", "value": "", "multiline": false, "label": "Input" },
                    { "id": "save", "kind": "button", "label": "Save", "operation_id": "example.save",
                      "params": { "\(collisionName)": "fixed" },
                      "field_bindings": { "\(collisionName)": "input" } }
                  ]}
                }
                """.data(using: .utf8)!,
                [collisionName]
            )
        ]

        for (payload, sentinels) in payloads {
            XCTAssertThrowsError(try PanelDocumentCodec.decode(payload)) { error in
                let representations = [String(describing: error), String(reflecting: error)]
                for sentinel in sentinels {
                    for representation in representations {
                        XCTAssertFalse(
                            representation.contains(sentinel),
                            "error leaked \(sentinel)"
                        )
                    }
                }
            }
        }
    }

    // MARK: - Helpers

    private static let listPanelJSON = """
    {
      "panel_id": "example.notebook.list",
      "revision": 2,
      "title": "Session Notebook",
      "state": "ready",
      "root": {
        "id": "list_root",
        "kind": "stack",
        "children": [
          { "id": "row_1", "kind": "text", "value": "Sprint planning" },
          { "id": "row_1_btn", "kind": "button", "label": "Resume",
            "operation_id": "example.notebook.resume",
            "params": { "notebook_id": "abc" },
            "field_bindings": {} },
          { "id": "row_2", "kind": "text", "value": "Design review" },
          { "id": "row_2_btn", "kind": "button", "label": "Resume",
            "operation_id": "example.notebook.resume",
            "params": { "notebook_id": "def" },
            "field_bindings": {} }
        ]
      }
    }
    """

    private static let editorPanelJSON = """
    {
      "panel_id": "example.notebook.editor",
      "revision": 5,
      "title": "Editor",
      "state": "ready",
      "root": {
        "id": "editor_root",
        "kind": "stack",
        "children": [
          { "id": "draft", "kind": "text_input", "value": "Initial draft...",
            "multiline": true, "label": "Draft notes" },
          { "id": "save_btn", "kind": "button", "label": "Save",
            "operation_id": "example.notebook.save",
            "params": { "notebook_id": "abc" },
            "field_bindings": { "body": "draft" } }
        ]
      }
    }
    """
}

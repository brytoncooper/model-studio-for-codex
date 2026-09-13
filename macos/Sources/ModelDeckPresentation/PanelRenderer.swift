import AppKit
import Foundation
import ModelDeckContracts

/// An operation that the trusted host has made available to a panel.
///
/// This is a presentation-layer descriptor. It does not carry transport,
/// authorization, or execution behavior.
public struct TrustedPanelOperationDescriptor: Equatable, Sendable {
    public let operationID: String

    public init(operationID: String) {
        self.operationID = operationID
    }
}

/// A user action emitted by ``PanelRenderer`` after local field binding.
///
/// The host owns authorization and execution. The renderer only reports the
/// operation selected by the user and the immutable parameter snapshot that
/// existed at click time.
public struct PanelActionIntent: Equatable {
    public let panelID: String
    public let documentRevision: Int
    public let operationID: String
    public let params: [String: JSONValue]

    public init(
        panelID: String,
        documentRevision: Int,
        operationID: String,
        params: [String: JSONValue]
    ) {
        self.panelID = panelID
        self.documentRevision = documentRevision
        self.operationID = operationID
        self.params = params
    }
}

public typealias TrustedPanelOperationLookup =
    (_ operationID: String) async -> TrustedPanelOperationDescriptor?
public typealias PanelActionIntentCallback = (_ intent: PanelActionIntent) -> Void

/// Generic AppKit rendering for validated extension panel documents.
///
/// The renderer understands only the four public node kinds. It treats text
/// as inert text, resolves button availability through a trusted host lookup,
/// and emits action intents without executing operations itself.
@MainActor
public final class PanelRenderer: NSViewController, NSTextFieldDelegate, NSTextViewDelegate {
    public private(set) var document: PanelDocument

    private let operationLookup: TrustedPanelOperationLookup
    private let onActionIntent: PanelActionIntentCallback

    private let titleLabel = NSTextField(labelWithString: "")
    private let stateLabel = NSTextField(wrappingLabelWithString: "")
    private let contentScrollView = NSScrollView()
    private let contentStack = NSStackView()

    private var fieldValues: [NodeID: String]
    private var singleLineFields: [NodeID: NSTextField] = [:]
    private var multilineTextViews: [NodeID: NSTextView] = [:]
    private var buttonNodes: [ObjectIdentifier: ButtonNode] = [:]
    private var buttonsByNodeID: [NodeID: NSButton] = [:]
    private var inputControlOrder: [NSView] = []
    private var buttonOrder: [NSButton] = []
    private var resolvedOperations: [NodeID: TrustedPanelOperationDescriptor] = [:]
    private var operationLookupTasks: [NodeID: Task<Void, Never>] = [:]
    private var renderGeneration = 0

    public init(
        document: PanelDocument,
        operationLookup: @escaping TrustedPanelOperationLookup,
        onActionIntent: @escaping PanelActionIntentCallback
    ) {
        self.document = document
        self.operationLookup = operationLookup
        self.onActionIntent = onActionIntent
        self.fieldValues = Self.fieldValues(in: document.root)
        super.init(nibName: nil, bundle: nil)
    }

    @available(*, unavailable)
    public required init?(coder: NSCoder) {
        fatalError("PanelRenderer must be initialized with a PanelDocument")
    }

    public override func loadView() {
        let container = NSView()
        let rootStack = NSStackView()
        rootStack.orientation = .vertical
        rootStack.alignment = .leading
        rootStack.spacing = 10
        rootStack.edgeInsets = NSEdgeInsets(top: 12, left: 12, bottom: 12, right: 12)
        rootStack.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(rootStack)

        NSLayoutConstraint.activate([
            rootStack.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            rootStack.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            rootStack.topAnchor.constraint(equalTo: container.topAnchor),
            rootStack.bottomAnchor.constraint(equalTo: container.bottomAnchor),
        ])

        titleLabel.font = NSFont.systemFont(ofSize: 16, weight: .semibold)
        titleLabel.setAccessibilityLabel("Panel title")
        rootStack.addArrangedSubview(titleLabel)

        stateLabel.textColor = .secondaryLabelColor
        stateLabel.setAccessibilityLabel("Panel state")
        rootStack.addArrangedSubview(stateLabel)

        contentStack.orientation = .vertical
        contentStack.alignment = .leading
        contentStack.spacing = 8
        contentStack.translatesAutoresizingMaskIntoConstraints = false

        contentScrollView.documentView = contentStack
        contentScrollView.hasVerticalScroller = true
        contentScrollView.drawsBackground = false
        contentScrollView.translatesAutoresizingMaskIntoConstraints = false
        contentScrollView.setContentHuggingPriority(.defaultLow, for: .vertical)
        rootStack.addArrangedSubview(contentScrollView)

        NSLayoutConstraint.activate([
            contentStack.leadingAnchor.constraint(equalTo: contentScrollView.contentView.leadingAnchor),
            contentStack.topAnchor.constraint(equalTo: contentScrollView.contentView.topAnchor),
            contentStack.widthAnchor.constraint(equalTo: contentScrollView.contentView.widthAnchor),
            contentScrollView.widthAnchor.constraint(equalTo: rootStack.widthAnchor, constant: -24),
            contentScrollView.heightAnchor.constraint(greaterThanOrEqualToConstant: 120),
        ])

        view = container
    }

    public override func viewDidLoad() {
        super.viewDidLoad()
        rebuildRenderedDocument(restoring: nil)
    }

    /// Applies a newer snapshot and returns whether it replaced the current one.
    ///
    /// Revisions are compared within a panel id. Equal or older revisions are
    /// ignored. A different panel id starts a new revision sequence.
    @discardableResult
    public func update(document newDocument: PanelDocument) -> Bool {
        let isSamePanel = newDocument.panelID == document.panelID
        if isSamePanel && newDocument.revision <= document.revision {
            return false
        }

        let focus = isSamePanel && isViewLoaded ? captureFocus() : nil
        document = newDocument
        fieldValues = Self.fieldValues(in: newDocument.root)

        if isViewLoaded {
            rebuildRenderedDocument(restoring: focus)
        }
        return true
    }

    public func controlTextDidChange(_ notification: Notification) {
        guard document.isReady,
              let field = notification.object as? NSTextField,
              let nodeID = singleLineFields.first(where: { $0.value === field })?.key
        else {
            return
        }
        fieldValues[nodeID] = field.stringValue
    }

    public func textDidChange(_ notification: Notification) {
        guard document.isReady,
              let textView = notification.object as? NSTextView,
              let nodeID = multilineTextViews.first(where: { $0.value === textView })?.key
        else {
            return
        }
        fieldValues[nodeID] = textView.string
    }

    private func rebuildRenderedDocument(restoring focus: FocusSnapshot?) {
        cancelOperationLookups()
        renderGeneration += 1
        resolvedOperations.removeAll()
        singleLineFields.removeAll()
        multilineTextViews.removeAll()
        buttonNodes.removeAll()
        buttonsByNodeID.removeAll()
        inputControlOrder.removeAll()
        buttonOrder.removeAll()
        contentStack.arrangedSubviews.forEach { $0.removeFromSuperview() }

        titleLabel.stringValue = document.title
        titleLabel.setAccessibilityValue(document.title)
        renderStateMessage()

        if let root = document.root {
            let renderedRoot = makeView(for: root)
            renderedRoot.translatesAutoresizingMaskIntoConstraints = false
            contentStack.addArrangedSubview(renderedRoot)
            renderedRoot.widthAnchor.constraint(equalTo: contentStack.widthAnchor).isActive = true
        }

        contentScrollView.isHidden = document.root == nil
        contentStack.alphaValue = document.hasStaleRoot ? 0.65 : 1
        view.layoutSubtreeIfNeeded()
        restoreFocus(focus)
        startOperationLookups()
        updateKeyViewLoop()
    }

    private func renderStateMessage() {
        let text: String?
        switch document.state {
        case .loading:
            text = document.message ?? "Loading…"
        case .ready:
            text = document.message
        case .empty:
            text = document.message ?? "No content."
        case .error:
            text = document.message ?? "Unable to load this panel."
        case .unavailable:
            text = document.message ?? "This panel is unavailable."
        }
        stateLabel.stringValue = text ?? ""
        stateLabel.setAccessibilityValue(text ?? "Ready")
        stateLabel.isHidden = text == nil
    }

    private func makeView(for node: PanelNode) -> NSView {
        switch node {
        case .stack(let stackNode):
            return makeStackView(for: stackNode)
        case .text(let textNode):
            return makeTextView(for: textNode)
        case .textInput(let textInputNode):
            return makeTextInputView(for: textInputNode)
        case .button(let buttonNode):
            return makeButton(for: buttonNode)
        }
    }

    private func makeStackView(for node: StackNode) -> NSView {
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8
        stack.setAccessibilityRole(.group)
        stack.setAccessibilityLabel("Panel content group")

        for childNode in node.children {
            let childView = makeView(for: childNode)
            childView.translatesAutoresizingMaskIntoConstraints = false
            stack.addArrangedSubview(childView)
            childView.widthAnchor.constraint(lessThanOrEqualTo: stack.widthAnchor).isActive = true
        }
        return stack
    }

    private func makeTextView(for node: TextNode) -> NSView {
        let label = NSTextField(wrappingLabelWithString: node.value)
        label.isSelectable = true
        label.allowsEditingTextAttributes = false
        label.target = nil
        label.action = nil
        label.setAccessibilityLabel(node.value)
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return label
    }

    private func makeTextInputView(for node: TextInputNode) -> NSView {
        let row = NSStackView()
        row.orientation = .vertical
        row.alignment = .leading
        row.spacing = 4
        row.setAccessibilityRole(.group)
        row.setAccessibilityLabel(node.label)

        let label = NSTextField(labelWithString: node.label)
        label.setAccessibilityLabel(node.label)
        row.addArrangedSubview(label)

        if node.multiline {
            let textView = NSTextView()
            textView.string = fieldValues[node.id] ?? node.value
            textView.font = NSFont.systemFont(ofSize: NSFont.systemFontSize)
            textView.isEditable = document.isReady
            textView.isSelectable = true
            textView.isRichText = false
            textView.isVerticallyResizable = true
            textView.isHorizontallyResizable = false
            textView.autoresizingMask = [.width]
            textView.isAutomaticLinkDetectionEnabled = false
            textView.isAutomaticDataDetectionEnabled = false
            textView.delegate = self
            textView.setAccessibilityLabel(node.label)
            textView.textContainer?.widthTracksTextView = true

            let scrollView = NSScrollView()
            scrollView.documentView = textView
            scrollView.hasVerticalScroller = true
            scrollView.borderType = .bezelBorder
            scrollView.translatesAutoresizingMaskIntoConstraints = false
            scrollView.heightAnchor.constraint(greaterThanOrEqualToConstant: 96).isActive = true
            scrollView.widthAnchor.constraint(greaterThanOrEqualToConstant: 280).isActive = true
            row.addArrangedSubview(scrollView)
            multilineTextViews[node.id] = textView
            inputControlOrder.append(textView)
        } else {
            let field = NSTextField(string: fieldValues[node.id] ?? node.value)
            field.isEditable = document.isReady
            field.isEnabled = document.isReady
            field.delegate = self
            field.setAccessibilityLabel(node.label)
            field.translatesAutoresizingMaskIntoConstraints = false
            field.widthAnchor.constraint(greaterThanOrEqualToConstant: 280).isActive = true
            row.addArrangedSubview(field)
            singleLineFields[node.id] = field
            inputControlOrder.append(field)
        }
        return row
    }

    private func makeButton(for node: ButtonNode) -> NSView {
        let button = NSButton(title: node.label, target: self, action: #selector(didSelectButton(_:)))
        button.isEnabled = false
        button.setAccessibilityLabel(node.label)
        buttonNodes[ObjectIdentifier(button)] = node
        buttonsByNodeID[node.id] = button
        buttonOrder.append(button)
        return button
    }

    private func startOperationLookups() {
        guard document.isReady else { return }
        let panelID = document.panelID
        let revision = document.revision
        let generation = renderGeneration

        for (buttonIdentity, buttonNode) in buttonNodes {
            guard let button = buttonForIdentity(buttonIdentity) else { continue }
            let nodeID = buttonNode.id
            let operationID = buttonNode.operationID
            let operationLookup = self.operationLookup
            operationLookupTasks[nodeID] = Task { [weak self] in
                let descriptor = await operationLookup(operationID)
                guard !Task.isCancelled, let self else { return }
                self.applyResolvedOperation(
                    descriptor,
                    operationID: operationID,
                    nodeID: nodeID,
                    button: button,
                    panelID: panelID,
                    revision: revision,
                    generation: generation
                )
            }
        }
    }

    private func applyResolvedOperation(
        _ descriptor: TrustedPanelOperationDescriptor?,
        operationID: String,
        nodeID: NodeID,
        button: NSButton,
        panelID: String,
        revision: Int,
        generation: Int
    ) {
        guard generation == renderGeneration,
              document.panelID == panelID,
              document.revision == revision,
              document.isReady,
              buttonsByNodeID[nodeID] === button
        else {
            return
        }

        operationLookupTasks[nodeID] = nil
        guard let descriptor, descriptor.operationID == operationID else {
            button.isEnabled = false
            resolvedOperations[nodeID] = nil
            updateKeyViewLoop()
            return
        }
        resolvedOperations[nodeID] = descriptor
        button.isEnabled = true
        updateKeyViewLoop()
    }

    private func buttonForIdentity(_ identity: ObjectIdentifier) -> NSButton? {
        buttonsByNodeID.values.first { ObjectIdentifier($0) == identity }
    }

    private func cancelOperationLookups() {
        operationLookupTasks.values.forEach { $0.cancel() }
        operationLookupTasks.removeAll()
    }

    @objc private func didSelectButton(_ sender: NSButton) {
        guard document.isReady,
              sender.isEnabled,
              let buttonNode = buttonNodes[ObjectIdentifier(sender)],
              let descriptor = resolvedOperations[buttonNode.id],
              descriptor.operationID == buttonNode.operationID
        else {
            return
        }

        var params = buttonNode.params
        for (paramName, inputNodeID) in buttonNode.fieldBindings {
            guard params[paramName] == nil, let currentText = fieldValues[inputNodeID] else {
                return
            }
            params[paramName] = .string(currentText)
        }

        onActionIntent(PanelActionIntent(
            panelID: document.panelID,
            documentRevision: document.revision,
            operationID: descriptor.operationID,
            params: params
        ))
    }

    private struct FocusSnapshot {
        let nodeID: NodeID
        let selectedRange: NSRange
    }

    private func captureFocus() -> FocusSnapshot? {
        guard let responder = view.window?.firstResponder else { return nil }

        if let textView = responder as? NSTextView {
            if let nodeID = multilineTextViews.first(where: { $0.value === textView })?.key {
                return FocusSnapshot(nodeID: nodeID, selectedRange: textView.selectedRange())
            }
            if textView.isFieldEditor,
               let nodeID = singleLineFields.first(where: {
                   ($0.value.currentEditor() as? NSTextView) === textView
               })?.key {
                return FocusSnapshot(nodeID: nodeID, selectedRange: textView.selectedRange())
            }
        }

        if let field = responder as? NSTextField,
           let nodeID = singleLineFields.first(where: { $0.value === field })?.key {
            return FocusSnapshot(nodeID: nodeID, selectedRange: NSRange(location: 0, length: 0))
        }
        return nil
    }

    private func restoreFocus(_ focus: FocusSnapshot?) {
        guard document.isReady, let focus, let window = view.window else { return }

        if let textView = multilineTextViews[focus.nodeID] {
            guard window.makeFirstResponder(textView) else { return }
            textView.setSelectedRange(clamped(focus.selectedRange, for: textView.string))
            return
        }

        if let field = singleLineFields[focus.nodeID] {
            guard window.makeFirstResponder(field),
                  let editor = field.currentEditor() as? NSTextView
            else {
                return
            }
            editor.setSelectedRange(clamped(focus.selectedRange, for: field.stringValue))
        }
    }

    private func clamped(_ range: NSRange, for text: String) -> NSRange {
        let utf16Count = text.utf16.count
        let location = min(range.location, utf16Count)
        let length = min(range.length, utf16Count - location)
        return NSRange(location: location, length: length)
    }

    private func updateKeyViewLoop() {
        let enabledInputs = inputControlOrder.filter { control in
            if let field = control as? NSTextField { return field.isEnabled && field.isEditable }
            if let textView = control as? NSTextView { return textView.isEditable }
            return false
        }
        let controls = enabledInputs + buttonOrder.filter(\.isEnabled)

        for (index, control) in controls.enumerated() {
            control.nextKeyView = controls[(index + 1) % controls.count]
        }
    }

    private static func fieldValues(in root: PanelNode?) -> [NodeID: String] {
        guard let root else { return [:] }
        var values: [NodeID: String] = [:]
        root.walk { node in
            if case .textInput(let textInputNode) = node {
                values[textInputNode.id] = textInputNode.value
            }
        }
        return values
    }
}

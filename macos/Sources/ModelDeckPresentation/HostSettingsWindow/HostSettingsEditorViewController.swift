import AppKit
import ModelDeckClient

/// Native AppKit editor for one host's settings document.
///
/// Renders ``HostSettingsEditorPresenter/state`` into a searchable native
/// form plus a full-TOML editor behind a segmented mode switch. All mutations
/// go through presenter methods; every presenter call is followed by
/// ``render()`` (immediately for synchronous calls, in the completion for
/// asynchronous ones). The raw ``NSTextView`` is seeded from the authorized
/// source ``HostSettingsEditorPresenter/rawEditorText()``; typing calls
/// `editRaw` only and never triggers validation, preview, save, or restarts.
/// Cmd+S (``handleSaveShortcut()``) chains preview then save, and only saves
/// when the resulting state reports `canSave`.
@MainActor
final class HostSettingsEditorViewController: NSViewController, NSTextViewDelegate {
    private let presenter: HostSettingsEditorPresenter
    private var onSaved: (() -> Void)?

    private var searchField = NSSearchField()
    private var modeControl = NSSegmentedControl()
    private var statusLabel = NSTextField(labelWithString: "")
    private var formStack = FlippedSettingsStackView()
    private var formScroll = NSScrollView()
    private var rawScroll = NSScrollView()
    private var rawTextView = NSTextView()
    private var diagnosticsStack = NSStackView()
    private var previewTextView = NSTextView()
    private var previewBox = NSBox()
    private var progress = NSProgressIndicator()
    private var validateButton = NSButton()
    private var previewButton = NSButton()
    private var saveButton = NSButton()
    private var retryButton = NSButton()
    private var discardButton = NSButton()
    private var resetButton = NSButton()
    private var lastRenderedRawText: String?

    init(presenter: HostSettingsEditorPresenter, onSaved: (() -> Void)? = nil) {
        self.presenter = presenter
        self.onSaved = onSaved
        super.init(nibName: nil, bundle: nil)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { nil }

    override func loadView() {
        let container = SettingsBackgroundView(frame: NSRect(x: 0, y: 0, width: 720, height: 560))
        NSLayoutConstraint.activate([
            container.widthAnchor.constraint(greaterThanOrEqualToConstant: 720),
            container.heightAnchor.constraint(greaterThanOrEqualToConstant: 560),
        ])
        let root = NSStackView()
        root.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            root.topAnchor.constraint(equalTo: container.topAnchor),
            root.bottomAnchor.constraint(equalTo: container.bottomAnchor),
        ])
        root.alignment = .leading
        root.orientation = .vertical
        root.spacing = 8
        root.edgeInsets = NSEdgeInsets(top: 12, left: 12, bottom: 12, right: 12)
        self.view = container
        buildHeader(into: root)
        buildStatus(into: root)
        buildForm(into: root)
        buildRaw(into: root)
        buildDiagnostics(into: root)
        buildPreview(into: root)
        buildButtons(into: root)
        for child in root.arrangedSubviews {
            child.translatesAutoresizingMaskIntoConstraints = false
            child.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -24).isActive = true
        }
        formScroll.setContentHuggingPriority(.defaultLow, for: .vertical)
        rawScroll.setContentHuggingPriority(.defaultLow, for: .vertical)
    }

    override func viewDidLayout() {
        super.viewDidLayout()
        let size = rawScroll.contentSize
        rawTextView.setFrameSize(NSSize(width: size.width, height: max(size.height, rawTextView.frame.height)))
        rawTextView.textContainer?.containerSize = NSSize(width: size.width, height: .greatestFiniteMagnitude)
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        render()
    }

    private func buildHeader(into root: NSStackView) {
        let header = NSStackView()
        header.orientation = .horizontal
        header.spacing = 8
        searchField.target = self
        searchField.action = #selector(didSearch)
        searchField.setAccessibilityLabel("Search settings fields")
        searchField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        header.addArrangedSubview(searchField)
        modeControl = NSSegmentedControl(labels: ["Form", "TOML"], trackingMode: .selectOne, target: self, action: #selector(didSwitchMode))
        modeControl.setAccessibilityLabel("Editor mode")
        header.addArrangedSubview(modeControl)
        root.addArrangedSubview(header)
        searchField.nextKeyView = modeControl
    }

    private func buildStatus(into root: NSStackView) {
        statusLabel.font = NSFont.systemFont(ofSize: NSFont.smallSystemFontSize)
        statusLabel.textColor = .secondaryLabelColor
        statusLabel.setAccessibilityLabel("Editor status")
        root.addArrangedSubview(statusLabel)
        progress.style = .spinning
        progress.controlSize = .small
        root.addArrangedSubview(progress)
    }

    private func buildForm(into root: NSStackView) {
        formStack.orientation = .vertical
        formStack.spacing = 8
        formStack.alignment = .leading
        formStack.translatesAutoresizingMaskIntoConstraints = false
        formScroll.documentView = formStack
        formScroll.hasVerticalScroller = true
        NSLayoutConstraint.activate([
            formStack.leadingAnchor.constraint(equalTo: formScroll.contentView.leadingAnchor),
            formStack.topAnchor.constraint(equalTo: formScroll.contentView.topAnchor),
            formStack.widthAnchor.constraint(equalTo: formScroll.contentView.widthAnchor),
        ])
        formScroll.translatesAutoresizingMaskIntoConstraints = false
        formScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 200).isActive = true
        root.addArrangedSubview(formScroll)
    }

    private func buildRaw(into root: NSStackView) {
        rawTextView = NSTextView()
        rawTextView.font = NSFont.monospacedSystemFont(ofSize: NSFont.systemFontSize, weight: .regular)
        rawTextView.isAutomaticQuoteSubstitutionEnabled = false
        rawTextView.delegate = self
        rawTextView.isVerticallyResizable = true
        rawTextView.isHorizontallyResizable = false
        rawTextView.autoresizingMask = [.width]
        rawTextView.textContainer?.widthTracksTextView = true
        rawTextView.setAccessibilityLabel("Raw TOML editor")
        rawScroll = NSScrollView()
        rawScroll.documentView = rawTextView
        rawScroll.hasVerticalScroller = true
        rawScroll.translatesAutoresizingMaskIntoConstraints = false
        rawScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 200).isActive = true
        root.addArrangedSubview(rawScroll)
    }

    private func buildDiagnostics(into root: NSStackView) {
        diagnosticsStack.orientation = .vertical
        diagnosticsStack.spacing = 2
        root.addArrangedSubview(diagnosticsStack)
    }

    private func buildPreview(into root: NSStackView) {
        previewTextView = NSTextView()
        previewTextView.font = NSFont.monospacedSystemFont(ofSize: NSFont.smallSystemFontSize, weight: .regular)
        previewTextView.isEditable = false
        previewTextView.isSelectable = true
        previewTextView.setAccessibilityLabel("Preview diff, read only")
        previewBox = NSBox()
        previewBox.title = "Preview"
        previewBox.contentView = previewTextView
        previewBox.translatesAutoresizingMaskIntoConstraints = false
        previewBox.heightAnchor.constraint(greaterThanOrEqualToConstant: 120).isActive = true
        root.addArrangedSubview(previewBox)
    }

    private func button(_ title: String, action: Selector, label: String) -> NSButton {
        let button = NSButton(title: title, target: self, action: action)
        button.setAccessibilityLabel(label)
        return button
    }

    private func buildButtons(into root: NSStackView) {
        let row = NSStackView()
        row.orientation = .horizontal
        row.spacing = 8
        validateButton = button("Validate", action: #selector(didTapValidate), label: "Validate draft")
        previewButton = button("Preview", action: #selector(didTapPreview), label: "Preview changes")
        saveButton = button("Save", action: #selector(didTapSave), label: "Save changes")
        retryButton = button("Retry Save", action: #selector(didTapRetry), label: "Retry save")
        discardButton = button("Discard", action: #selector(didTapDiscard), label: "Discard draft")
        resetButton = button("Reset", action: #selector(didTapReset), label: "Reload from host")
        [validateButton, previewButton, saveButton, retryButton, discardButton, resetButton].forEach(row.addArrangedSubview)
        root.addArrangedSubview(row)
    }

    func render() {
        let state = presenter.state
        modeControl.selectedSegment = (state.mode == .raw) ? 1 : 0
        formScroll.isHidden = (state.mode == .raw)
        rawScroll.isHidden = (state.mode != .raw)
        if searchField.stringValue != state.searchQuery { searchField.stringValue = state.searchQuery }
        rebuildForm(rows: state.visibleRows)
        syncRawText()
        rebuildDiagnostics()
        renderPreview()
        renderStatus()
        renderButtons()
        updateKeyViewLoop()
    }

    private func rebuildForm(rows: [HostSettingsEditorRow]) {
        let existing = formStack.arrangedSubviews.compactMap { $0 as? HostSettingsFieldRowView }
        let desired: [NSView] = rows.map { row in
            if let old = existing.first(where: { $0.represents(row) }) { return old }
            let rowView = HostSettingsFieldRowView(row: row, presenter: presenter) { [weak self] in self?.render() }
            rowView.translatesAutoresizingMaskIntoConstraints = false
            return rowView
        }
        for old in formStack.arrangedSubviews where !desired.contains(where: { $0 === old }) {
            old.removeFromSuperview()
        }
        for (index, rowView) in desired.enumerated() {
            if formStack.arrangedSubviews.indices.contains(index), formStack.arrangedSubviews[index] === rowView { continue }
            if rowView.superview != nil { rowView.removeFromSuperview() }
            formStack.insertArrangedSubview(rowView, at: index)
            rowView.widthAnchor.constraint(equalTo: formStack.widthAnchor).isActive = true
        }
        if rows.isEmpty {
            let empty = NSTextField(labelWithString: state().phase == .loading ? "Loading…" : "No fields match.")
            empty.textColor = .secondaryLabelColor
            formStack.addArrangedSubview(empty)
        }
    }

    private var rowViews: [HostSettingsFieldRowView] {
        formStack.arrangedSubviews.compactMap { $0 as? HostSettingsFieldRowView }
    }

    private func state() -> HostSettingsEditorState { presenter.state }

    private func syncRawText() {
        let current = presenter.rawEditorText()
        guard current != lastRenderedRawText || rawTextView.string != current else { return }
        if rawTextView.window?.firstResponder == rawTextView, rawTextView.string != lastRenderedRawText {
            return
        }
        rawTextView.string = current
        lastRenderedRawText = current
    }

    private func rebuildDiagnostics() {
        diagnosticsStack.arrangedSubviews.forEach { $0.removeFromSuperview() }
        diagnosticsStack.isHidden = state().diagnostics.isEmpty
        for diagnostic in state().diagnostics {
            var location = ""
            if let line = diagnostic.line { location += "line \(line)" }
            if let column = diagnostic.column { location += (location.isEmpty ? "" : ", ") + "col \(column)" }
            let text = "[\(diagnostic.severity.rawValue)] \(diagnostic.code) \(location) \(diagnostic.message)".trimmingCharacters(in: .whitespaces)
            let label = NSTextField(labelWithString: text)
            label.font = NSFont.systemFont(ofSize: NSFont.smallSystemFontSize)
            label.textColor = diagnostic.severity == .error ? .systemRed : .secondaryLabelColor
            label.setAccessibilityLabel("Diagnostic: \(text)")
            diagnosticsStack.addArrangedSubview(label)
        }
    }

    private func renderPreview() {
        if let summary = state().previewSummary {
            previewBox.isHidden = false
            var header = summary.changed ? "Changes ready to save." : "No changes."
            if summary.diffTruncated { header += " (diff truncated)" }
            if !summary.changedFieldIDs.isEmpty { header += " Fields: " + summary.changedFieldIDs.joined(separator: ", ") }
            previewTextView.string = header + "\n" + summary.diff
        } else {
            previewBox.isHidden = true
        }
    }

    private func renderStatus() {
        let state = state()
        var parts: [String] = ["Phase: \(state.phase)"]
        if state.isDirty { parts.append("unsaved changes") }
        if let conflict = state.conflict { parts.append("conflict: \(String(describing: conflict))") }
        if let error = state.error { parts.append("error: \(String(describing: error))") }
        if !state.unrepresentedPaths.isEmpty { parts.append("\(state.unrepresentedPaths.count) unrepresented path(s), edit in TOML") }
        statusLabel.stringValue = parts.joined(separator: " \u{00B7} ")
        progress.isHidden = !state.isBusy
        if state.isBusy { progress.startAnimation(nil) } else { progress.stopAnimation(nil) }
    }

    private func renderButtons() {
        let state = state()
        saveButton.isEnabled = state.canSave && !state.isBusy
        previewButton.isEnabled = state.isDirty && !state.isBusy
        validateButton.isEnabled = state.isDirty && !state.isBusy
        retryButton.isEnabled = state.savePending && !state.isBusy
        discardButton.isEnabled = state.isDirty && !state.isBusy
        resetButton.isEnabled = !state.isBusy
    }

    private func updateKeyViewLoop() {
        var chain: [NSView] = [searchField, modeControl]
        if state().mode == .raw {
            chain.append(rawTextView)
        } else {
            chain += rowViews.flatMap(\.editingControls)
        }
        chain += [validateButton, previewButton, saveButton, retryButton, discardButton, resetButton]
        for (index, view) in chain.enumerated() {
            view.nextKeyView = chain[(index + 1) % chain.count]
        }
    }

    func textDidChange(_ notification: Notification) {
        guard notification.object as? NSTextView === rawTextView else { return }
        presenter.editRaw(rawTextView.string)
        lastRenderedRawText = rawTextView.string
        render()
    }

    @objc private func didSearch() {
        presenter.search(searchField.stringValue)
        render()
    }

    @objc private func didSwitchMode() {
        presenter.setMode(modeControl.selectedSegment == 1 ? .raw : .structured)
        render()
    }

    private func commitFocusedField() -> Bool {
        if let fieldEditor = view.window?.firstResponder as? NSTextView, fieldEditor.isFieldEditor {
            guard view.window?.makeFirstResponder(view) == true else { return false }
        }
        return !rowViews.contains(where: \.hasInvalidInput)
    }

    @objc private func didTapValidate() {
        guard commitFocusedField() else { return }
        presenter.requestValidation { [weak self] _ in self?.render() }
        render()
    }

    @objc private func didTapPreview() {
        guard commitFocusedField() else { return }
        presenter.requestPreview { [weak self] _ in self?.render() }
        render()
    }

    @objc private func didTapSave() {
        guard commitFocusedField() else { return }
        presenter.beginSave { [weak self] outcome in
            guard let self else { return }
            self.render()
            if outcome.applied { self.onSaved?() }
        }
    }

    @objc private func didTapRetry() {
        presenter.retrySave { [weak self] outcome in
            guard let self else { return }
            self.render()
            if outcome.applied { self.onSaved?() }
        }
    }

    @objc private func didTapDiscard() {
        presenter.cancelEditing()
        render()
    }

    @objc private func didTapReset() {
        presenter.refresh { [weak self] _ in self?.render() }
    }

    /// Cmd+S entry point. Chains preview then save and only saves when the
    /// previewed state reports `canSave`.
    func handleSaveShortcut() {
        guard commitFocusedField() else { return }
        let state = state()
        guard state.isDirty, !state.isBusy else { return }
        if state.canSave {
            didTapSave()
        } else {
            presenter.requestPreview { [weak self] outcome in
                guard let self else { return }
                self.render()
                if outcome.applied, self.state().canSave { self.didTapSave() }
            }
        }
    }
}

/// Window hosting the settings editor. Forwards Cmd+S to the editor so the
/// shortcut works regardless of focus.
final class HostSettingsWindowController: NSWindowController {
    private let editor: HostSettingsEditorViewController

    init(hostLabel: String, presenter: HostSettingsEditorPresenter, onSaved: (() -> Void)? = nil) {
        self.editor = HostSettingsEditorViewController(presenter: presenter, onSaved: onSaved)
        let window = HostSettingsEditorWindow(
            contentRect: NSRect(x: 0, y: 0, width: 720, height: 560),
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false)
        window.title = "Settings \u{2013} \(hostLabel)"
        super.init(window: window)
        window.contentViewController = editor
        window.contentMinSize = NSSize(width: 720, height: 560)
        window.setContentSize(NSSize(width: 720, height: 560))
        window.saveShortcut = { [weak editor] in editor?.handleSaveShortcut() }
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { nil }

}

private final class SettingsBackgroundView: NSView {
    override func draw(_ dirtyRect: NSRect) {
        NSColor.windowBackgroundColor.setFill()
        dirtyRect.fill()
    }
}

private final class FlippedSettingsStackView: NSStackView {
    override var isFlipped: Bool { true }
}

private final class HostSettingsEditorWindow: NSWindow {
    var saveShortcut: (() -> Void)?

    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        let modifiers = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        if modifiers == .command, event.charactersIgnoringModifiers == "s" {
            saveShortcut?()
            return true
        }
        return super.performKeyEquivalent(with: event)
    }
}

@MainActor
extension HostSettingsEditorViewController {
    var testRowViews: [HostSettingsFieldRowView] { rowViews }
    var testSaveEnabled: Bool { saveButton.isEnabled }
    var testPreviewEnabled: Bool { previewButton.isEnabled }
    var testRawText: String { rawTextView.string }
    var testStatusText: String { statusLabel.stringValue }
    var testDiagnosticCount: Int { diagnosticsStack.arrangedSubviews.count }
    var testIsRawVisible: Bool { !rawScroll.isHidden }
    func testSearch(_ query: String) { searchField.stringValue = query; didSearch() }
    func testSwitchToRaw() { modeControl.selectedSegment = 1; didSwitchMode() }
    func testSaveShortcut() { handleSaveShortcut() }
}

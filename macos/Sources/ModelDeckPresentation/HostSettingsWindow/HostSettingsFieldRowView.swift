import AppKit
import ModelDeckClient
import ModelDeckContracts

/// One native row for a single ``HostSettingsEditorRow``.
///
/// Control selection follows the supplied row metadata only: booleans use a
/// checkbox, strings/numbers/enums use a text field, secrets use an
/// ``NSSecureTextField`` with explicit set/clear/change steps, and complex
/// `.entries` fields fall back to an explicit "Edit in TOML" button because
/// nested entry-group controls are not settled yet. Non-editable rows render
/// disabled controls with a status label. No settings keys or defaults are
/// hardcoded; every action funnels through ``HostSettingsEditorPresenter``.
final class HostSettingsFieldRowView: NSView {
    private let row: HostSettingsEditorRow
    private let presenter: HostSettingsEditorPresenter
    private let onChange: () -> Void

    private var valueControl: NSView?
    private var secretField: NSSecureTextField?
    private var textField: NSTextField?
    private var checkBox: NSButton?
    private var statusLabel = NSTextField(labelWithString: "")
    private var secretChanging = false
    private(set) var hasInvalidInput = false

    init(row: HostSettingsEditorRow, presenter: HostSettingsEditorPresenter, onChange: @escaping () -> Void) {
        self.row = row
        self.presenter = presenter
        self.onChange = onChange
        super.init(frame: .zero)
        setAccessibilityRole(.group)
        setAccessibilityLabel(row.label)
        build()
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { nil }

    var fieldID: String { row.fieldID }
    func represents(_ candidate: HostSettingsEditorRow) -> Bool { row == candidate }
    var editingControls: [NSView] {
        func controls(in view: NSView) -> [NSView] {
            view.subviews.flatMap { child -> [NSView] in
                if let button = child as? NSButton, button.isEnabled { return [button] }
                if let field = child as? NSTextField, field.isEnabled, field.isEditable || field.isSelectable { return [field] }
                return controls(in: child)
            }
        }
        return controls(in: self)
    }

    private func rebuildControls() {
        subviews.forEach { $0.removeFromSuperview() }
        secretField?.stringValue = ""
        secretField = nil
        textField = nil
        checkBox = nil
        valueControl = nil
        build()
    }

    private var isEditable: Bool { row.editability == .editable }

    private func build() {
        let stack = NSStackView()
        stack.orientation = .horizontal
        stack.spacing = 8
        stack.alignment = .centerY
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: trailingAnchor),
            stack.topAnchor.constraint(equalTo: topAnchor, constant: 2),
            stack.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -2),
        ])

        let label = NSTextField(labelWithString: row.label)
        label.font = NSFont.systemFont(ofSize: NSFont.systemFontSize)
        label.setContentHuggingPriority(.defaultHigh, for: .horizontal)
        NSLayoutConstraint.activate([label.widthAnchor.constraint(equalToConstant: 180)])
        label.setAccessibilityLabel("Field \(row.label)")
        stack.addArrangedSubview(label)

        if row.isSecret {
            buildSecret(into: stack)
        } else if row.type == .entries {
            buildEntriesFallback(into: stack)
        } else if row.type == .enum, !row.enumChoices.isEmpty {
            buildEnum(into: stack)
        } else if row.type == .boolean {
            buildBoolean(into: stack)
        } else {
            buildText(into: stack)
        }

        statusLabel.font = NSFont.systemFont(ofSize: NSFont.smallSystemFontSize)
        statusLabel.textColor = .secondaryLabelColor
        statusLabel.lineBreakMode = .byTruncatingTail
        statusLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        statusLabel.stringValue = statusText()
        statusLabel.setAccessibilityLabel("Status for \(row.label): \(statusText())")
        stack.addArrangedSubview(statusLabel)

        if isEditable && !row.isUnset && row.type != .entries {
            let unset = NSButton(title: "Unset", target: self, action: #selector(didTapUnset))
            unset.bezelStyle = .rounded
            unset.setAccessibilityLabel("Unset \(row.label)")
            stack.addArrangedSubview(unset)
        }
        if row.type == .entries {
            let open = NSButton(title: "Edit in TOML", target: self, action: #selector(didTapOpenTOML))
            open.bezelStyle = .rounded
            open.setAccessibilityLabel("Edit \(row.label) in TOML")
            stack.addArrangedSubview(open)
        }
    }

    private func statusText() -> String {
        var parts: [String] = [row.valueState.rawValue, row.editability.rawValue]
        if row.isSecret { parts.append(row.secretConfigured ? "secret set" : "secret unset") }
        if row.hasPendingSecretChange { parts.append("secret change pending") }
        if row.isUnset { parts.append("unset") }
        return parts.joined(separator: " \u{00B7} ")
    }

    private func buildBoolean(into stack: NSStackView) {
        let box = NSButton(checkboxWithTitle: "", target: self, action: #selector(didToggleBool))
        box.state = (row.displayValue == "true") ? .on : .off
        box.isEnabled = isEditable
        box.setAccessibilityLabel("\(row.label) boolean value")
        checkBox = box
        valueControl = box
        stack.addArrangedSubview(box)
    }

    private func buildEnum(into stack: NSStackView) {
        let picker = NSPopUpButton(frame: .zero, pullsDown: false)
        picker.target = self
        picker.action = #selector(didSelectEnum)
        picker.isEnabled = isEditable
        picker.setAccessibilityLabel("\(row.label) value")
        let menu = NSMenu()
        let selected = row.enumChoices.firstIndex { $0.value == row.enumValue }
        if selected == nil {
            let placeholder = NSMenuItem(title: row.enumValue == nil ? "Choose a value" : "Current: \(row.displayValue ?? "")", action: nil, keyEquivalent: "")
            placeholder.tag = -1
            placeholder.isEnabled = false
            menu.addItem(placeholder)
        }
        for (index, choice) in row.enumChoices.enumerated() {
            let item = NSMenuItem(title: choice.label, action: nil, keyEquivalent: "")
            item.tag = index
            menu.addItem(item)
        }
        picker.menu = menu
        picker.selectItem(withTag: selected ?? -1)
        valueControl = picker
        stack.addArrangedSubview(picker)
    }

    @objc private func didSelectEnum(_ sender: NSPopUpButton) {
        let index = sender.selectedTag()
        guard row.enumChoices.indices.contains(index) else { return }
        presenter.setFieldValue(fieldID: row.fieldID, entryID: nil, value: row.enumChoices[index].value)
        onChange()
    }

    private func buildText(into stack: NSStackView) {
        let field = NSTextField(string: row.displayValue ?? "")
        field.font = NSFont.systemFont(ofSize: NSFont.systemFontSize)
        field.isEditable = isEditable
        field.isSelectable = true
        field.target = self
        field.action = #selector(didCommitText)
        field.cell?.sendsActionOnEndEditing = true
        field.setAccessibilityLabel("\(row.label) value")
        if row.type == .enum {
            field.placeholderString = "JSON string, number, or Boolean"
            field.toolTip = "Use quotes for strings, for example \"fast\"; numbers and true/false remain typed values."
        }
        field.setContentHuggingPriority(.defaultLow, for: .horizontal)
        field.widthAnchor.constraint(greaterThanOrEqualToConstant: 80).isActive = true
        textField = field
        valueControl = field
        stack.addArrangedSubview(field)
    }

    private func buildSecret(into stack: NSStackView) {
        if row.secretConfigured && !secretChanging {
            let change = NSButton(title: "Change\u{2026}", target: self, action: #selector(didTapChangeSecret))
            change.isEnabled = isEditable
            change.setAccessibilityLabel("Change secret \(row.label)")
            stack.addArrangedSubview(change)
            let clear = NSButton(title: "Clear", target: self, action: #selector(didTapClearSecret))
            clear.isEnabled = isEditable
            clear.bezelStyle = .rounded
            clear.setAccessibilityLabel("Clear secret \(row.label)")
            stack.addArrangedSubview(clear)
        } else {
            let field = NSSecureTextField(string: "")
            field.isEditable = isEditable
            field.widthAnchor.constraint(greaterThanOrEqualToConstant: 80).isActive = true
            field.target = self
            field.action = #selector(didCommitSecret)
            field.cell?.sendsActionOnEndEditing = true
            field.setAccessibilityLabel(row.secretConfigured ? "New value for \(row.label)" : "Set secret \(row.label)")
            secretField = field
            valueControl = field
            stack.addArrangedSubview(field)
            if row.secretConfigured {
                let cancel = NSButton(title: "Cancel", target: self, action: #selector(didTapCancelSecretChange))
                cancel.bezelStyle = .rounded
                stack.addArrangedSubview(cancel)
            }
        }
    }

    private func buildEntriesFallback(into stack: NSStackView) {
        let note = NSTextField(labelWithString: "Complex entries edit in the TOML editor.")
        note.font = NSFont.systemFont(ofSize: NSFont.smallSystemFontSize)
        note.textColor = .secondaryLabelColor
        stack.addArrangedSubview(note)
    }

    @objc private func didToggleBool(_ sender: NSButton) {
        presenter.setFieldValue(fieldID: row.fieldID, entryID: nil, value: .bool(sender.state == .on))
        onChange()
    }

    @objc private func didCommitText(_ sender: NSTextField) {
        commitText(sender.stringValue)
    }

    private func commitText(_ text: String) {
        let value: JSONValue
        switch row.type {
        case .boolean:
            value = .bool(text.lowercased() == "true")
        case .integer, .number:
            guard let number = Double(text), number.isFinite,
                  row.type != .integer || number.rounded(.towardZero) == number else {
                hasInvalidInput = true
                statusLabel.stringValue = row.type == .integer ? "Enter a whole number." : "Enter a finite number."
                return
            }
            value = .number(number)
        case .enum:
            guard let scalar = try? JSONDecoder().decode(JSONValue.self, from: Data(text.utf8)) else {
                hasInvalidInput = true
                statusLabel.stringValue = "Enter a JSON string, number, or Boolean."
                return
            }
            switch scalar {
            case .string, .bool: value = scalar
            case .number(let number) where number.isFinite: value = scalar
            default:
                hasInvalidInput = true
                statusLabel.stringValue = "Enter a JSON string, number, or Boolean."
                return
            }
        case .stringList:
            guard let strings = try? JSONDecoder().decode([String].self, from: Data(text.utf8)) else {
                hasInvalidInput = true
                statusLabel.stringValue = "Enter a JSON list of strings."
                return
            }
            value = .array(strings.map(JSONValue.string))
        default:
            value = .string(text)
        }
        hasInvalidInput = false
        presenter.setFieldValue(fieldID: row.fieldID, entryID: nil, value: value)
        onChange()
    }

    @objc private func didTapUnset() {
        presenter.unsetField(fieldID: row.fieldID, entryID: nil)
        onChange()
    }

    @objc private func didTapOpenTOML() {
        presenter.setMode(.raw)
        onChange()
    }

    @objc private func didCommitSecret(_ sender: NSSecureTextField) {
        guard !sender.stringValue.isEmpty else { return }
        presenter.setSecretValue(fieldID: row.fieldID, entryID: nil, value: .string(sender.stringValue))
        sender.stringValue = ""
        secretChanging = false
        rebuildControls()
        onChange()
    }

    @objc private func didTapChangeSecret() {
        secretChanging = true
        rebuildControls()
        onChange()
        if let secretField { window?.makeFirstResponder(secretField) }
    }

    @objc private func didTapCancelSecretChange() {
        secretChanging = false
        rebuildControls()
        onChange()
    }

    @objc private func didTapClearSecret() {
        presenter.clearSecret(fieldID: row.fieldID, entryID: nil)
        onChange()
    }
}

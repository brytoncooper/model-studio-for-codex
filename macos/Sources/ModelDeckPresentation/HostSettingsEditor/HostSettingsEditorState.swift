import Foundation
import ModelDeckClient
import ModelDeckContracts

/// Public read-only UI state for the native host settings editor.
///
/// AppKit renders from `HostSettingsEditorState` only. Full TOML text lives
/// inside the presenter; this snapshot carries row metadata, safe diagnostics,
/// and preview summaries. Secret field values never appear here.
public struct HostSettingsEditorState: Equatable, Sendable {
    public var phase: HostSettingsEditorPhase
    public var mode: HostSettingsEditorMode
    public var rows: [HostSettingsEditorRow]
    public var searchQuery: String
    public var visibleRows: [HostSettingsEditorRow]
    public var diagnostics: [HostDiagnostic]
    public var previewSummary: HostSettingsPreviewSummary?
    public var canSave: Bool
    public var isDirty: Bool
    public var isBusy: Bool
    public var conflict: HostSettingsEditorConflict?
    public var error: HostSettingsEditorError?
    public var savePending: Bool
    public var unrepresentedPaths: [String]

    public init(
        phase: HostSettingsEditorPhase = .idle,
        mode: HostSettingsEditorMode = .structured,
        rows: [HostSettingsEditorRow] = [],
        searchQuery: String = "",
        visibleRows: [HostSettingsEditorRow] = [],
        diagnostics: [HostDiagnostic] = [],
        previewSummary: HostSettingsPreviewSummary? = nil,
        canSave: Bool = false,
        isDirty: Bool = false,
        isBusy: Bool = false,
        conflict: HostSettingsEditorConflict? = nil,
        error: HostSettingsEditorError? = nil,
        savePending: Bool = false,
        unrepresentedPaths: [String] = []
    ) {
        self.phase = phase
        self.mode = mode
        self.rows = rows
        self.searchQuery = searchQuery
        self.visibleRows = visibleRows
        self.diagnostics = diagnostics
        self.previewSummary = previewSummary
        self.canSave = canSave
        self.isDirty = isDirty
        self.isBusy = isBusy
        self.conflict = conflict
        self.error = error
        self.savePending = savePending
        self.unrepresentedPaths = unrepresentedPaths
    }
}

/// Lifecycle phase of the editor. Raw TOML and secret values are never carried here.
public enum HostSettingsEditorPhase: Equatable, Sendable {
    case idle
    case loading
    case ready
    case validating
    case previewing
    case saving
    case saved(changed: Bool)
    case failure
}

/// Which draft surface the user is editing.
public enum HostSettingsEditorMode: Equatable, Sendable {
    case structured
    case raw
}

/// One searchable structured row. `displayValue` and `hasPendingSecretChange`
/// never carry secret content: secrets expose `secretConfigured` plus a
/// pending-change flag only.
public struct HostSettingsEditorRow: Equatable, Sendable {
    public var fieldID: String
    public var label: String
    public var key: String?
    public var type: HostFieldType
    public var valueState: HostValueState
    public var editability: HostEditability
    public var isSecret: Bool
    public var secretConfigured: Bool
    public var hasPendingSecretChange: Bool
    public var displayValue: String?
    /// Typed public enum metadata. Never populated for secret fields.
    public var enumValue: JSONValue?
    public var enumChoices: [HostConstraintChoice]
    public var isUnset: Bool

    public init(
        fieldID: String,
        label: String,
        key: String? = nil,
        type: HostFieldType,
        valueState: HostValueState,
        editability: HostEditability,
        isSecret: Bool,
        secretConfigured: Bool = false,
        hasPendingSecretChange: Bool = false,
        displayValue: String? = nil,
        enumValue: JSONValue? = nil,
        enumChoices: [HostConstraintChoice] = [],
        isUnset: Bool = false
    ) {
        self.fieldID = fieldID
        self.label = label
        self.key = key
        self.type = type
        self.valueState = valueState
        self.editability = editability
        self.isSecret = isSecret
        self.secretConfigured = secretConfigured
        self.hasPendingSecretChange = hasPendingSecretChange
        self.displayValue = displayValue
        self.enumValue = enumValue
        self.enumChoices = enumChoices
        self.isUnset = isUnset
    }
}

/// Safe preview summary: diff text comes from the server and may be shown;
/// candidate TOML itself is held privately by the presenter for save.
public struct HostSettingsPreviewSummary: Equatable, Sendable {
    public var changed: Bool
    public var diff: String
    public var diffTruncated: Bool
    public var changedFieldIDs: [String]
    public var applicationEffects: [HostApplicationEffect]
    public var protectedProjectionChanges: Bool

    public init(
        changed: Bool,
        diff: String,
        diffTruncated: Bool,
        changedFieldIDs: [String],
        applicationEffects: [HostApplicationEffect],
        protectedProjectionChanges: Bool
    ) {
        self.changed = changed
        self.diff = diff
        self.diffTruncated = diffTruncated
        self.changedFieldIDs = changedFieldIDs
        self.applicationEffects = applicationEffects
        self.protectedProjectionChanges = protectedProjectionChanges
    }
}

/// Conflict notice. The draft is always preserved; this only signals the base moved.
public struct HostSettingsEditorConflict: Equatable, Sendable {
    public var message: String
    public init(message: String = "Settings changed on the server. Your draft is preserved.") {
        self.message = message
    }
}

/// Safe error states. Codes and field IDs only; never TOML, secrets, or log text.
public enum HostSettingsEditorError: Equatable, Sendable {
    case notLoaded
    case validationFailed
    case previewFailed(code: String)
    case saveFailed(code: String)
    case readFailed(code: String)
    case conflict
}

/// Outcome delivered to AppKit after each async step. `applied` is false for
/// suppressed stale responses.
public struct HostSettingsEditorOutcome: Sendable {
    public var state: HostSettingsEditorState
    public var applied: Bool
    public init(state: HostSettingsEditorState, applied: Bool) {
        self.state = state
        self.applied = applied
    }
}

import Foundation
import ModelDeckClient
import ModelDeckContracts

/// Native settings editor presenter: read snapshot, edit raw or structured,
/// validate, preview, then save.
///
/// - Pure `@MainActor` state model. No filesystem access, no settings names.
/// - Full TOML (snapshot, draft, candidate) is held private; the public state
///   carries row metadata, safe diagnostics, and preview summaries only. Error
///   states carry codes and field IDs, never values, TOML, or log text.
/// - Any draft change invalidates the preview and bumps the generation, so
///   stale validate/preview/read/save responses are suppressed.
/// - Save is enabled only for a current valid preview with changed intent.
/// - Dirty cancel restores the last saved snapshot. Conflicts never discard
///   the draft. No automatic save retry. The idempotency key is stable per
///   save attempt and `retrySave()` resends the exact same params.
@MainActor
public final class HostSettingsEditorPresenter {
    public private(set) var state = HostSettingsEditorState()

    private var service: HostSettingsServing?
    private var hostID: String?
    private var generation: UInt64 = 0
    private var opSeq: UInt64 = 0
    private var busyOwner: UInt64?
    private var saveAttemptKey: String?
    private var pendingSaveParams: HostSettingsSaveParams?
    private var pendingSaveGeneration: UInt64?
    private var pendingSaveStructured: HostStructuredDocument?

    private var lastSaved: HostSettingsSnapshot?
    private var rawDraft: String?
    private var structuredChanges: [HostStructuredChange] = []
    private var pendingSecrets: [String: JSONValue] = [:]
    private var candidateHash: String?
    private var candidateRawTOML: String?
    private var candidateStructured: HostStructuredDocument?
    private var previewToken: HostSettingsPreview?
    private var previewValidForGeneration: UInt64?
    private var contextRevision: String?
    private var lastValidationValid = false

    public init() {}

    // MARK: - Lifecycle

    public func start(hostID: String, service: HostSettingsServing, completion: @escaping (HostSettingsEditorOutcome) -> Void) {
        cancelInflight()
        self.hostID = hostID
        self.service = service
        state = HostSettingsEditorState(phase: .loading, isBusy: true)
        let gen = generation
        opSeq &+= 1
        let op = opSeq
        busyOwner = op
        Task { @MainActor in
            do {
                let result = try await service.read(params: HostSettingsReadParams(hostID: hostID))
                guard gen == self.generation else {
                    self.clearOwnedBusyFlags(op: op)
                    completion(HostSettingsEditorOutcome(state: self.state, applied: false))
                    return
                }
                self.applySnapshot(result.snapshot)
                completion(HostSettingsEditorOutcome(state: self.state, applied: true))
            } catch {
                guard gen == self.generation else {
                    self.clearOwnedBusyFlags(op: op)
                    completion(HostSettingsEditorOutcome(state: self.state, applied: false))
                    return
                }
                self.busyOwner = nil
                self.state = HostSettingsEditorState(
                    phase: .failure,
                    error: .readFailed(code: Self.safeCode(for: error)),
                    savePending: false
                )
                completion(HostSettingsEditorOutcome(state: self.state, applied: true))
            }
        }
    }

    /// Refresh from the server without discarding a dirty draft. A moved base
    /// surfaces a conflict notice; the draft is preserved.
    public func refresh(completion: @escaping (HostSettingsEditorOutcome) -> Void) {
        guard let hostID, let service else {
            completion(HostSettingsEditorOutcome(state: state, applied: false))
            return
        }
        let gen = generation
        Task { @MainActor in
            do {
                let result = try await service.read(params: HostSettingsReadParams(hostID: hostID))
                guard gen == self.generation else {
                    self.clearOwnedBusyFlags(op: gen)
                    completion(HostSettingsEditorOutcome(state: self.state, applied: false))
                    return
                }
                if self.state.isDirty {
                    self.state.conflict = HostSettingsEditorConflict()
                    self.state.error = .conflict
                    completion(HostSettingsEditorOutcome(state: self.state, applied: true))
                } else {
                    self.applySnapshot(result.snapshot)
                    completion(HostSettingsEditorOutcome(state: self.state, applied: true))
                }
            } catch {
                guard gen == self.generation else {
                    completion(HostSettingsEditorOutcome(state: self.state, applied: false))
                    return
                }
                if Self.looksLikeConflict(error) {
                    self.state.conflict = HostSettingsEditorConflict()
                    self.state.error = .conflict
                } else {
                    self.state.error = .readFailed(code: Self.safeCode(for: error))
                }
                completion(HostSettingsEditorOutcome(state: self.state, applied: true))
            }
        }
    }

    /// Dirty cancel: drop the draft and preview, keep the last saved snapshot.
    public func cancelEditing() {
        cancelInflight()
        pendingSecrets = [:]
        structuredChanges = []
        candidateHash = nil
        candidateRawTOML = nil
        candidateStructured = nil
        previewToken = nil
        previewValidForGeneration = nil
        lastValidationValid = false
        saveAttemptKey = nil
        pendingSaveParams = nil
        pendingSaveGeneration = nil
        pendingSaveStructured = nil
        if let snapshot = lastSaved {
            rawDraft = snapshot.rawTOML
            rebuildRows(from: snapshot.structured)
            state.previewSummary = nil
            state.diagnostics = []
            state.conflict = nil
            state.error = nil
            state.phase = .ready
            state.isDirty = false
            state.canSave = false
            state.isBusy = false
            state.savePending = false
        } else {
            rawDraft = nil
            state = HostSettingsEditorState()
        }
    }

    public func cancel() {
        service?.cancel()
        cancelEditing()
    }

    // MARK: - Editing

    public func setMode(_ mode: HostSettingsEditorMode) {
        state.mode = mode
        syncVisibleRows()
    }

    public func editRaw(_ toml: String) {
        rawDraft = toml
        state.mode = .raw
        invalidatePreview()
    }

    public func setStructuredChanges(_ changes: [HostStructuredChange]) {
        structuredChanges = changes
        state.mode = .structured
        invalidatePreview()
        rebuildPendingSecretFlags()
    }

    public func setFieldValue(fieldID: String, entryID: String?, value: JSONValue) {
        structuredChanges.removeAll { $0.fieldID == fieldID && $0.entryID == entryID }
        structuredChanges.append(.set(fieldID: fieldID, entryID: entryID, value: value))
        pendingSecrets.removeValue(forKey: Self.secretKey(fieldID: fieldID, entryID: entryID))
        state.mode = .structured
        invalidatePreview()
        rebuildPendingSecretFlags()
    }

    public func unsetField(fieldID: String, entryID: String?) {
        structuredChanges.removeAll { $0.fieldID == fieldID && $0.entryID == entryID }
        structuredChanges.append(.unset(fieldID: fieldID, entryID: entryID))
        pendingSecrets.removeValue(forKey: Self.secretKey(fieldID: fieldID, entryID: entryID))
        state.mode = .structured
        invalidatePreview()
        rebuildPendingSecretFlags()
    }

    /// Stage a secret value write-only: the pending value is held privately
    /// and never reflected in public rows or errors.
    public func setSecretValue(fieldID: String, entryID: String?, value: JSONValue) {
        pendingSecrets[Self.secretKey(fieldID: fieldID, entryID: entryID)] = value
        structuredChanges.removeAll { $0.fieldID == fieldID && $0.entryID == entryID }
        structuredChanges.append(.set(fieldID: fieldID, entryID: entryID, value: value))
        state.mode = .structured
        invalidatePreview()
        rebuildPendingSecretFlags()
    }

    public func clearSecret(fieldID: String, entryID: String?) {
        pendingSecrets.removeValue(forKey: Self.secretKey(fieldID: fieldID, entryID: entryID))
        unsetField(fieldID: fieldID, entryID: entryID)
    }

    public func search(_ query: String) {
        state.searchQuery = query
        syncVisibleRows()
    }

    // MARK: - Validate / preview / save

    public func requestValidation(completion: @escaping (HostSettingsEditorOutcome) -> Void) {
        guard let hostID, let service, let snapshot = lastSaved else {
            state.error = .notLoaded
            completion(HostSettingsEditorOutcome(state: state, applied: true))
            return
        }
        guard let draft = currentDraft() else {
            state.error = .notLoaded
            completion(HostSettingsEditorOutcome(state: state, applied: true))
            return
        }
        let gen = generation
        opSeq &+= 1
        let op = opSeq
        busyOwner = op
        state.phase = .validating
        state.isBusy = true
        let params = HostSettingsValidateParams(
            hostID: hostID,
            documentID: snapshot.documentID,
            expectedContentHash: snapshot.documentRevision,
            draft: draft,
            contextRevision: contextRevision ?? snapshot.contextRevision
        )
        Task { @MainActor in
            do {
                let result = try await service.validate(params: params)
                guard gen == self.generation else {
                    self.clearOwnedBusyFlags(op: op)
                    completion(HostSettingsEditorOutcome(state: self.state, applied: false))
                    return
                }
                self.contextRevision = result.contextRevision
                self.state.diagnostics = result.diagnostics
                self.lastValidationValid = result.valid
                if result.valid {
                    self.candidateHash = result.candidateContentHash
                    self.candidateRawTOML = result.candidateRawTOML
                    self.candidateStructured = result.candidateStructured
                    self.syncFromCandidate()
                    self.state.error = nil
                    self.state.phase = .ready
                } else {
                    self.candidateHash = nil
                    self.candidateRawTOML = nil
                    self.candidateStructured = nil
                    self.state.error = .validationFailed
                    self.state.phase = .ready
                }
                self.state.isBusy = false
                self.updateCanSave()
                completion(HostSettingsEditorOutcome(state: self.state, applied: true))
            } catch {
                guard gen == self.generation else {
                    completion(HostSettingsEditorOutcome(state: self.state, applied: false))
                    return
                }
                self.clearOwnedBusyFlags(op: op)
                self.lastValidationValid = false
                if Self.looksLikeConflict(error) {
                    self.state.conflict = HostSettingsEditorConflict()
                    self.state.error = .conflict
                } else {
                    self.state.error = .previewFailed(code: Self.safeCode(for: error))
                }
                completion(HostSettingsEditorOutcome(state: self.state, applied: true))
            }
        }
    }

    public func requestPreview(completion: @escaping (HostSettingsEditorOutcome) -> Void) {
        guard let hostID, let service, let snapshot = lastSaved else {
            state.error = .notLoaded
            completion(HostSettingsEditorOutcome(state: state, applied: true))
            return
        }
        guard let draft = currentDraft() else {
            state.error = .notLoaded
            completion(HostSettingsEditorOutcome(state: state, applied: true))
            return
        }
        let gen = generation
        opSeq &+= 1
        let op = opSeq
        busyOwner = op
        state.phase = .previewing
        state.isBusy = true
        let params = HostSettingsPreviewParams(
            hostID: hostID,
            documentID: snapshot.documentID,
            expectedContentHash: snapshot.documentRevision,
            draft: draft,
            contextRevision: contextRevision ?? snapshot.contextRevision
        )
        Task { @MainActor in
            do {
                let result = try await service.preview(params: params)
                guard gen == self.generation else {
                    self.clearOwnedBusyFlags(op: op)
                    completion(HostSettingsEditorOutcome(state: self.state, applied: false))
                    return
                }
                self.contextRevision = result.contextRevision
                self.state.diagnostics = result.diagnostics
                if result.valid, let preview = result.preview {
                    self.candidateHash = result.candidateContentHash
                    self.candidateRawTOML = result.candidateRawTOML
                    self.candidateStructured = result.candidateStructured
                    self.previewToken = preview
                    self.previewValidForGeneration = gen
                    self.lastValidationValid = true
                    self.syncFromCandidate()
                    self.state.previewSummary = HostSettingsPreviewSummary(
                        changed: preview.changed,
                        diff: preview.diff,
                        diffTruncated: preview.diffTruncated,
                        changedFieldIDs: preview.changedFieldIDs,
                        applicationEffects: preview.applicationEffects,
                        protectedProjectionChanges: preview.protectedProjectionChanges
                    )
                    self.state.error = nil
                    self.state.phase = .ready
                } else {
                    self.previewToken = nil
                    self.previewValidForGeneration = nil
                    self.lastValidationValid = false
                    self.candidateHash = nil
                    self.candidateRawTOML = nil
                    self.candidateStructured = nil
                    self.state.previewSummary = nil
                    self.state.error = .validationFailed
                    self.state.phase = .ready
                }
                self.state.isBusy = false
                self.updateCanSave()
                completion(HostSettingsEditorOutcome(state: self.state, applied: true))
            } catch {
                guard gen == self.generation else {
                    completion(HostSettingsEditorOutcome(state: self.state, applied: false))
                    return
                }
                self.clearOwnedBusyFlags(op: op)
                if Self.looksLikeConflict(error) {
                    self.state.conflict = HostSettingsEditorConflict()
                    self.state.error = .conflict
                } else {
                    self.state.error = .previewFailed(code: Self.safeCode(for: error))
                }
                self.state.phase = .ready
                completion(HostSettingsEditorOutcome(state: self.state, applied: true))
            }
        }
    }

    /// Save using the current valid preview token. Requires changed intent:
    /// unchanged previews leave saving disabled. The exact save params are
    /// frozen here; `retrySave()` resends the stored params verbatim.
    public func beginSave(completion: @escaping (HostSettingsEditorOutcome) -> Void) {
        guard let hostID, let snapshot = lastSaved else {
            state.error = .notLoaded
            completion(HostSettingsEditorOutcome(state: state, applied: true))
            return
        }
        guard let preview = previewToken,
            previewValidForGeneration == generation,
            let candidateHash,
            let candidateRawTOML else {
            state.error = .previewFailed(code: "stale_preview")
            state.savePending = false
            completion(HostSettingsEditorOutcome(state: state, applied: true))
            return
        }
        guard preview.changed else {
            state.error = .previewFailed(code: "unchanged")
            state.savePending = false
            completion(HostSettingsEditorOutcome(state: state, applied: true))
            return
        }
        saveAttemptKey = UUID().uuidString
        pendingSaveGeneration = generation
        pendingSaveStructured = candidateStructured
        pendingSaveParams = HostSettingsSaveParams(
            hostID: hostID,
            documentID: snapshot.documentID,
            expectedContentHash: snapshot.documentRevision,
            contextRevision: contextRevision ?? snapshot.contextRevision,
            previewID: preview.previewID,
            candidateContentHash: candidateHash,
            candidateRawTOML: candidateRawTOML,
            idempotencyKey: saveAttemptKey!
        )
        performSave(completion: completion)
    }

    /// Retry the failed save with the exact same frozen params.
    public func retrySave(completion: @escaping (HostSettingsEditorOutcome) -> Void) {
        guard pendingSaveParams != nil else {
            state.error = .previewFailed(code: "stale_preview")
            state.savePending = false
            completion(HostSettingsEditorOutcome(state: state, applied: true))
            return
        }
        performSave(completion: completion)
    }

    // MARK: - Private

    private func performSave(completion: @escaping (HostSettingsEditorOutcome) -> Void) {
        guard let service else {
            state.error = .notLoaded
            completion(HostSettingsEditorOutcome(state: state, applied: true))
            return
        }
        guard let params = pendingSaveParams else {
            state.error = .previewFailed(code: "stale_preview")
            state.savePending = false
            completion(HostSettingsEditorOutcome(state: state, applied: true))
            return
        }
        let gen = generation
        opSeq &+= 1
        let op = opSeq
        busyOwner = op
        state.phase = .saving
        state.isBusy = true
        state.savePending = true
        Task { @MainActor in
            do {
                let result = try await service.save(params: params)
                guard gen == self.generation else {
                    self.clearOwnedBusyFlags(op: op)
                    completion(HostSettingsEditorOutcome(state: self.state, applied: false))
                    return
                }
                let savedParams = params
                let savedStructured = self.pendingSaveStructured ?? self.candidateStructured
                if self.pendingSaveGeneration == self.generation {
                    self.applySaveSuccess(result, candidateRawTOML: savedParams.candidateRawTOML)
                } else {
                    self.commitSaveBasePreservingDraft(result, candidateRawTOML: savedParams.candidateRawTOML, candidateStructured: savedStructured)
                }
                completion(HostSettingsEditorOutcome(state: self.state, applied: true))
            } catch {
                guard gen == self.generation else {
                    self.clearOwnedBusyFlags(op: op)
                    completion(HostSettingsEditorOutcome(state: self.state, applied: false))
                    return
                }
                self.clearOwnedBusyFlags(op: op)
                self.state.phase = .ready
                if Self.looksLikeConflict(error) {
                    self.state.conflict = HostSettingsEditorConflict()
                    self.state.error = .conflict
                    self.state.savePending = false
                } else {
                    self.state.error = .saveFailed(code: Self.safeCode(for: error))
                    self.state.savePending = true
                }
                completion(HostSettingsEditorOutcome(state: self.state, applied: true))
            }
        }
    }

    /// Authorized UI access to the canonical raw draft text for the AppKit raw editor.
    ///
    /// Returns the current draft (structured candidates included via canonical sync),
    /// falling back to the last saved snapshot before load. Never exposes secrets
    /// beyond what the draft itself holds; diagnostics and errors stay in state.
    public func rawEditorText() -> String {
        rawDraft ?? lastSaved?.rawTOML ?? ""
    }

    /// A finished save commits the server revision: the saved snapshot becomes
    /// the new base, so cancel and later validate calls build on it.
    private func applySaveSuccess(_ result: HostSettingsSaveResult, candidateRawTOML: String) {
        if let snapshot = lastSaved {
            lastSaved = HostSettingsSnapshot(
                hostID: snapshot.hostID,
                documentID: result.documentID,
                documentRevision: .present(result.documentRevision),
                exists: snapshot.exists,
                target: snapshot.target,
                schemaProfile: snapshot.schemaProfile,
                precedence: snapshot.precedence,
                rawTOML: candidateRawTOML,
                structured: candidateStructured ?? snapshot.structured,
                contextRevision: result.contextRevision
            )
            rawDraft = candidateRawTOML
        }
        contextRevision = result.contextRevision
        structuredChanges = []
        pendingSecrets = [:]
        previewToken = nil
        previewValidForGeneration = nil
        lastValidationValid = false
        saveAttemptKey = nil
        pendingSaveParams = nil
        pendingSaveGeneration = nil
        pendingSaveStructured = nil
        clearOwnedBusyFlags(op: busyOwner ?? 0)
        state.isBusy = false
        state.savePending = false
        state.phase = .saved(changed: result.changed)
        state.canSave = false
        state.isDirty = false
        state.error = nil
        rebuildPendingSecretFlags()
    }

    private func commitSaveBasePreservingDraft(_ result: HostSettingsSaveResult, candidateRawTOML: String, candidateStructured: HostStructuredDocument?) {
        if let snapshot = lastSaved {
            lastSaved = HostSettingsSnapshot(
                hostID: snapshot.hostID,
                documentID: result.documentID,
                documentRevision: .present(result.documentRevision),
                exists: snapshot.exists,
                target: snapshot.target,
                schemaProfile: snapshot.schemaProfile,
                precedence: snapshot.precedence,
                rawTOML: candidateRawTOML,
                structured: candidateStructured ?? snapshot.structured,
                contextRevision: result.contextRevision
            )
        }
        contextRevision = result.contextRevision
        previewToken = nil
        previewValidForGeneration = nil
        lastValidationValid = false
        saveAttemptKey = nil
        pendingSaveParams = nil
        pendingSaveGeneration = nil
        pendingSaveStructured = nil
        clearOwnedBusyFlags(op: busyOwner ?? 0)
        state.isBusy = false
        state.savePending = false
        state.phase = .ready
        state.previewSummary = nil
        state.canSave = false
        state.isDirty = true
        state.error = nil
    }

    /// Clears busy flags owned by `op` without touching draft data. Stale
    /// completions always release their own flags; a newer operation owns
    /// the flags once it starts, so a stale completion never clears those.
    private func clearOwnedBusyFlags(op: UInt64) {
        guard busyOwner == op else { return }
        busyOwner = nil
        state.isBusy = false
        if state.phase == .saving || state.phase == .validating || state.phase == .previewing {
            state.phase = .ready
        }
        if state.phase == .ready {
            state.savePending = false
        }
        updateCanSave()
    }

    private func cancelInflight() {
        generation &+= 1
        busyOwner = nil
        service?.cancel()
    }

    private func invalidatePreview() {
        generation &+= 1
        previewToken = nil
        previewValidForGeneration = nil
        lastValidationValid = false
        state.previewSummary = nil
        state.isDirty = true
        if state.phase == .saved(changed: true) || state.phase == .saved(changed: false) {
            state.phase = .ready
        } else if state.phase == .idle || state.phase == .loading {
            state.phase = .ready
        }
        updateCanSave()
    }

    private func currentDraft() -> HostDraft? {
        guard lastSaved != nil else { return nil }
        if state.mode == .raw, let raw = rawDraft { return .raw(raw) }
        var changes = structuredChanges
        for (key, value) in pendingSecrets {
            let parts = key.split(separator: "|", omittingEmptySubsequences: false)
            guard parts.count == 2 else { continue }
            let fieldID = String(parts[0])
            let entryID = parts[1].isEmpty ? nil : String(parts[1])
            if !changes.contains(where: { $0.fieldID == fieldID && $0.entryID == entryID }) {
                changes.append(.set(fieldID: fieldID, entryID: entryID, value: value))
            }
        }
        return .structured(changes)
    }

    private func applySnapshot(_ snapshot: HostSettingsSnapshot) {
        lastSaved = snapshot
        contextRevision = snapshot.contextRevision
        rawDraft = snapshot.rawTOML
        structuredChanges = []
        pendingSecrets = [:]
        candidateHash = nil
        candidateRawTOML = nil
        candidateStructured = nil
        previewToken = nil
        previewValidForGeneration = nil
        lastValidationValid = false
        saveAttemptKey = nil
        pendingSaveParams = nil
        pendingSaveGeneration = nil
        pendingSaveStructured = nil
        busyOwner = nil
        rebuildRows(from: snapshot.structured)
        state = HostSettingsEditorState(
            phase: .ready,
            mode: state.mode,
            rows: state.rows,
            searchQuery: state.searchQuery,
            visibleRows: state.visibleRows,
            diagnostics: [],
            previewSummary: nil,
            canSave: false,
            isDirty: false,
            isBusy: false,
            conflict: nil,
            error: nil,
            savePending: false,
            unrepresentedPaths: snapshot.structured.unrepresentedPaths ?? []
        )
        syncVisibleRows()
    }

    /// Server candidate is authoritative when syncing structured/raw views.
    private func syncFromCandidate() {
        if let structured = candidateStructured {
            rebuildRows(from: structured)
            state.unrepresentedPaths = structured.unrepresentedPaths ?? state.unrepresentedPaths
        }
        if let raw = candidateRawTOML {
            rawDraft = raw
        }
    }

    private func rebuildRows(from structured: HostStructuredDocument) {
        var rows: [HostSettingsEditorRow] = []
        for section in structured.sections {
            for field in section.fields {
                rows.append(Self.row(for: field, pendingSecrets: pendingSecrets, changes: structuredChanges))
            }
            for group in section.fields.flatMap({ Self.entryGroups(of: $0) }) {
                for field in group.fields {
                    rows.append(Self.row(for: field, pendingSecrets: pendingSecrets, changes: structuredChanges))
                }
            }
        }
        state.rows = rows
        syncVisibleRows()
    }

    private func rebuildPendingSecretFlags() {
        guard let snapshot = lastSaved else { return }
        rebuildRows(from: candidateStructured ?? snapshot.structured)
    }

    private func syncVisibleRows() {
        let query = state.searchQuery.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else {
            state.visibleRows = state.rows
            return
        }
        let folded = query.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: .current)
        state.visibleRows = state.rows.filter { row in
            row.label.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: .current).contains(folded)
                || row.fieldID.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: .current).contains(folded)
                || (row.key?.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: .current).contains(folded) ?? false)
        }
    }

    private func updateCanSave() {
        let current = previewValidForGeneration == generation
            && previewToken?.changed == true
            && lastValidationValid
            && !state.isBusy
            && state.error != .validationFailed
        state.canSave = current
    }

    private static func secretKey(fieldID: String, entryID: String?) -> String {
        fieldID + "|" + (entryID ?? "")
    }

    private static func row(for field: HostFieldDescriptor, pendingSecrets: [String: JSONValue], changes: [HostStructuredChange]) -> HostSettingsEditorRow {
        switch field {
        case .secret(let descriptor):
            let pending = changes.contains(where: { $0.fieldID == descriptor.fieldID })
                || pendingSecrets.keys.contains(where: { $0.hasPrefix(descriptor.fieldID + "|") })
            return HostSettingsEditorRow(
                fieldID: descriptor.fieldID,
                label: descriptor.label,
                key: descriptor.key,
                type: descriptor.type,
                valueState: descriptor.valueState,
                editability: descriptor.editability,
                isSecret: true,
                secretConfigured: descriptor.configured,
                hasPendingSecretChange: pending,
                displayValue: nil,
                isUnset: descriptor.valueState == .unset
            )
        case .public(let descriptor):
            let pendingUnset = changes.contains(where: {
                if case .unset(let fieldID, _) = $0 { return fieldID == descriptor.fieldID }
                return false
            })
            return HostSettingsEditorRow(
                fieldID: descriptor.fieldID,
                label: descriptor.label,
                key: descriptor.key,
                type: descriptor.type,
                valueState: descriptor.valueState,
                editability: descriptor.editability,
                isSecret: false,
                displayValue: descriptor.value.map { String(describing: $0) },
                isUnset: pendingUnset || descriptor.valueState == .unset
            )
        }
    }

    private static func entryGroups(of field: HostFieldDescriptor) -> [HostEntryGroup] {
        if case .public(let descriptor) = field { return descriptor.entries ?? [] }
        return []
    }

    private static func looksLikeConflict(_ error: Error) -> Bool {
        let text = String(describing: error).lowercased()
        return text.contains("conflict") || text.contains("stale") || text.contains("revision") || text.contains("precondition")
    }

    private static func safeCode(for error: Error) -> String {
        if let client = error as? EngineClientError {
            switch client {
            case .invalidRendezvous: return "invalid_rendezvous"
            case .missingCredential: return "missing_credential"
            case .rendezvousMismatch: return "rendezvous_mismatch"
            case .negotiationFailed: return "negotiation_failed"
            case .protocolError: return "protocol_error"
            case .requestCancelled: return "request_cancelled"
            case .unavailable: return "unavailable"
            }
        }
        return "unknown"
    }
}

private extension HostStructuredChange {
    var fieldID: String {
        switch self {
        case .set(let fieldID, _, _): return fieldID
        case .unset(let fieldID, _): return fieldID
        }
    }

    var entryID: String? {
        switch self {
        case .set(_, let entryID, _): return entryID
        case .unset(_, let entryID): return entryID
        }
    }
}

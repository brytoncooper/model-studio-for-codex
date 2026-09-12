import XCTest
@testable import ModelDeckPresentation
import ModelDeckClient
import ModelDeckContracts

@MainActor
final class HostSettingsEditorTests: XCTestCase {
    func makeSnapshot(revision: String = "ctx-1") -> HostSettingsSnapshot {
        let field = HostFieldDescriptor.public(HostPublicFieldDescriptor(
            fieldID: "f1", label: "Cafe Name", key: "name",
            type: .string, valueState: .explicit, editability: .editable,
            applicationEffect: .immediate, value: .string("a")))
        let secret = HostFieldDescriptor.secret(HostSecretFieldDescriptor(
            fieldID: "s1", label: "Token", type: .string,
            valueState: .unset, editability: .editable,
            applicationEffect: .immediate, configured: false))
        return HostSettingsSnapshot(
            hostID: "h", documentID: "d", documentRevision: .present("base-1"),
            exists: true,
            target: HostSettingsTarget(displayName: "T", displayPath: "/t", scope: "user", writable: true),
            schemaProfile: HostSchemaProfile(schemaID: "s", schemaRevision: "1", hostVersion: "9", supportLevel: .supported),
            precedence: [], rawTOML: "a = 1",
            structured: HostStructuredDocument(sections: [
                HostSettingsSection(sectionID: "sec", title: "Sec", fields: [field, secret])
            ]),
            contextRevision: revision)
    }

    func makeValidPreview(changed: Bool = true) -> HostSettingsPreviewResult {
        HostSettingsPreviewResult(
            valid: true, validationLevel: .schema,
            candidateContentHash: "cand-1", candidateRawTOML: "a = 2",
            candidateStructured: makeSnapshot().structured, diagnostics: [],
            contextRevision: "ctx-2",
            preview: HostSettingsPreview(
                previewID: "p-1", baseContentHash: .present("base-1"),
                candidateContentHash: "cand-1", changed: changed, diff: "removed-added",
                diffTruncated: false, changedFieldIDs: ["f1"],
                applicationEffects: [.immediate], protectedProjectionChanges: false))
    }

    func testStalePreviewSuppressed() {
        let fake = FakeHostSettingsService()
        let presenter = HostSettingsEditorPresenter()
        let exp = expectation(description: "stale")
        exp.expectedFulfillmentCount = 2
        fake.snapshot = makeSnapshot()
        fake.previewImpl = { _, _ in
            try await Task.sleep(nanoseconds: 50_000_000)
            return self.makeValidPreview()
        }
        presenter.start(hostID: "h", service: fake) { _ in
            presenter.editRaw("first")
            presenter.requestPreview { first in
                XCTAssertFalse(first.applied)
                exp.fulfill()
            }
            presenter.editRaw("second")
            presenter.requestPreview { second in
                XCTAssertTrue(second.applied)
                XCTAssertTrue(second.state.canSave)
                exp.fulfill()
            }
        }
        wait(for: [exp], timeout: 5)
    }

    func testDirtyCancelPreservesLastSaved() {
        let fake = FakeHostSettingsService()
        fake.snapshot = makeSnapshot()
        let presenter = HostSettingsEditorPresenter()
        let exp = expectation(description: "cancel")
        presenter.start(hostID: "h", service: fake) { _ in
            presenter.editRaw("dirty toml")
            XCTAssertTrue(presenter.state.isDirty)
            presenter.cancelEditing()
            XCTAssertFalse(presenter.state.isDirty)
            XCTAssertFalse(presenter.state.canSave)
            XCTAssertEqual(presenter.state.phase, .ready)
            exp.fulfill()
        }
        wait(for: [exp], timeout: 5)
    }

    func testRawStructuredSyncUsesServerCandidate() {
        let fake = FakeHostSettingsService()
        fake.snapshot = makeSnapshot()
        fake.validateImpl = { _, result in result }
        let presenter = HostSettingsEditorPresenter()
        let exp = expectation(description: "sync")
        presenter.start(hostID: "h", service: fake) { _ in
            presenter.setFieldValue(fieldID: "f1", entryID: nil, value: .string("b"))
            presenter.requestValidation { outcome in
                XCTAssertTrue(outcome.applied)
                XCTAssertTrue(outcome.state.diagnostics.isEmpty)
                exp.fulfill()
            }
        }
        wait(for: [exp], timeout: 5)
    }

    func testValidationConflictAndSavePending() {
        let fake = FakeHostSettingsService()
        fake.snapshot = makeSnapshot()
        fake.previewImpl = { _, _ in self.makeValidPreview() }
        fake.saveImpl = { params in
            XCTAssertFalse(params.idempotencyKey.isEmpty)
            throw EngineClientError.unavailable("conflict stale revision")
        }
        let presenter = HostSettingsEditorPresenter()
        let exp = expectation(description: "conflict")
        presenter.start(hostID: "h", service: fake) { _ in
            presenter.editRaw("x = 1")
            presenter.requestPreview { previewed in
                XCTAssertTrue(previewed.state.canSave)
                presenter.beginSave { saved in
                    XCTAssertEqual(saved.state.error, .conflict)
                    XCTAssertNotNil(saved.state.conflict)
                    XCTAssertTrue(presenter.state.isDirty)
                    exp.fulfill()
                }
            }
        }
        wait(for: [exp], timeout: 5)
    }

    func testRetryKeepsIdempotencyKey() {
        let fake = FakeHostSettingsService()
        fake.snapshot = makeSnapshot()
        fake.previewImpl = { _, _ in self.makeValidPreview() }
        var keys: [String] = []
        fake.saveImpl = { params in
            keys.append(params.idempotencyKey)
            throw EngineClientError.unavailable("boom")
        }
        let presenter = HostSettingsEditorPresenter()
        let exp = expectation(description: "retry")
        presenter.start(hostID: "h", service: fake) { _ in
            presenter.editRaw("x = 1")
            presenter.requestPreview { _ in
                presenter.beginSave { _ in
                    presenter.retrySave { _ in
                        XCTAssertEqual(keys.count, 2)
                        XCTAssertEqual(keys[0], keys[1])
                        XCTAssertTrue(presenter.state.savePending)
                        exp.fulfill()
                    }
                }
            }
        }
        wait(for: [exp], timeout: 5)
    }

    func testSecretsNeverReflectedAndSearch() {
        let fake = FakeHostSettingsService()
        fake.snapshot = makeSnapshot()
        let presenter = HostSettingsEditorPresenter()
        let exp = expectation(description: "secrets")
        presenter.start(hostID: "h", service: fake) { _ in
            presenter.setSecretValue(fieldID: "s1", entryID: nil, value: .string("shh"))
            let row = presenter.state.rows.first(where: { $0.fieldID == "s1" })
            XCTAssertEqual(row?.displayValue, nil)
            XCTAssertEqual(row?.hasPendingSecretChange, true)
            presenter.search("cafe")
            XCTAssertEqual(presenter.state.visibleRows.map { r in r.fieldID }, ["f1"])
            let text = String(describing: presenter.state)
            XCTAssertFalse(text.contains("shh"))
            exp.fulfill()
        }
        wait(for: [exp], timeout: 5)
    }

    func testSafeCodeNeverReflectsErrorText() {
        let fake = FakeHostSettingsService()
        fake.snapshot = makeSnapshot()
        fake.previewImpl = { _, _ in throw EngineClientError.unavailable("SENTINEL SECRET super-secret-value") }
        let presenter = HostSettingsEditorPresenter()
        let exp = expectation(description: "safecode")
        presenter.start(hostID: "h", service: fake) { _ in
            presenter.editRaw("x = 1")
            presenter.requestPreview { outcome in
                if case .previewFailed(let code) = outcome.state.error {
                    XCTAssertEqual(code, "unavailable")
                    XCTAssertFalse(code.contains("SENTINEL"))
                } else {
                    XCTFail("expected previewFailed, got \(String(describing: outcome.state.error))")
                }
                let text = String(describing: outcome.state)
                XCTAssertFalse(text.contains("SENTINEL"))
                XCTAssertFalse(text.contains("super-secret-value"))
                exp.fulfill()
            }
        }
        wait(for: [exp], timeout: 5)
    }

    func testStaleSaveCompletionClearsBusyFlags() {
        let fake = FakeHostSettingsService()
        fake.snapshot = makeSnapshot()
        fake.previewImpl = { _, _ in self.makeValidPreview() }
        fake.saveImpl = { params in
            try await Task.sleep(nanoseconds: 100_000_000)
            return HostSettingsSaveResult(saved: true, changed: true, documentID: params.documentID, previousContentHash: params.expectedContentHash, documentRevision: "rev-2", backup: nil, applicationEffects: [], contextRevision: "ctx-3")
        }
        let presenter = HostSettingsEditorPresenter()
        let exp = expectation(description: "stalesave")
        presenter.start(hostID: "h", service: fake) { _ in
            presenter.editRaw("x = 1")
            presenter.requestPreview { _ in
                presenter.beginSave { first in
                    XCTAssertFalse(first.applied)
                    XCTAssertFalse(presenter.state.isBusy)
                    XCTAssertFalse(presenter.state.savePending)
                    XCTAssertEqual(presenter.state.phase, .ready)
                    exp.fulfill()
                }
                presenter.editRaw("edited while saving")
            }
        }
        wait(for: [exp], timeout: 5)
    }

    func testSaveSuccessAdvancesBaseRevision() {
        let fake = FakeHostSettingsService()
        fake.snapshot = makeSnapshot()
        fake.previewImpl = { _, _ in self.makeValidPreview() }
        fake.saveImpl = { params in
            HostSettingsSaveResult(saved: true, changed: true, documentID: params.documentID, previousContentHash: params.expectedContentHash, documentRevision: "rev-2", backup: nil, applicationEffects: [], contextRevision: "ctx-3")
        }
        let presenter = HostSettingsEditorPresenter()
        let exp = expectation(description: "successbase")
        presenter.start(hostID: "h", service: fake) { _ in
            presenter.editRaw("x = 1")
            presenter.requestPreview { _ in
                presenter.beginSave { saved in
                    XCTAssertTrue(saved.applied)
                    XCTAssertFalse(presenter.state.isDirty)
                    presenter.cancelEditing()
                    XCTAssertFalse(presenter.state.isDirty)
                    var seenBase: HostContentHash?
                    fake.validateImpl = { params, result in
                        seenBase = params.expectedContentHash
                        return result
                    }
                    presenter.editRaw("another edit")
                    presenter.requestValidation { _ in
                        XCTAssertEqual(seenBase, .present("rev-2"))
                        exp.fulfill()
                    }
                }
            }
        }
        wait(for: [exp], timeout: 5)
    }

    func testStructuredCandidateSyncsRawDraft() {
        let fake = FakeHostSettingsService()
        fake.snapshot = makeSnapshot()
        let presenter = HostSettingsEditorPresenter()
        let exp = expectation(description: "candidatesync")
        presenter.start(hostID: "h", service: fake) { _ in
            presenter.setFieldValue(fieldID: "f1", entryID: nil, value: .string("b"))
            presenter.requestValidation { _ in
                presenter.setMode(.raw)
                var sentDraft: HostDraft?
                fake.previewImpl = { params, fallback in
                    sentDraft = params.draft
                    return fallback
                }
                presenter.requestPreview { _ in
                    if case .raw(let toml) = sentDraft {
                        XCTAssertEqual(toml, "a = 9")
                    } else {
                        XCTFail("expected raw draft from server candidate, got \(String(describing: sentDraft))")
                    }
                    exp.fulfill()
                }
            }
        }
        wait(for: [exp], timeout: 5)
    }

    func testRetryResendsIdenticalParams() {
        let fake = FakeHostSettingsService()
        fake.snapshot = makeSnapshot()
        fake.previewImpl = { _, _ in self.makeValidPreview() }
        var seen: [HostSettingsSaveParams] = []
        fake.saveImpl = { params in
            seen.append(params)
            throw EngineClientError.unavailable("boom")
        }
        let presenter = HostSettingsEditorPresenter()
        let exp = expectation(description: "retryexact")
        presenter.start(hostID: "h", service: fake) { _ in
            presenter.editRaw("x = 1")
            presenter.requestPreview { _ in
                presenter.beginSave { _ in
                    presenter.editRaw("mutate after failure")
                    presenter.retrySave { _ in
                        XCTAssertEqual(seen.count, 2)
                        XCTAssertEqual(seen[0], seen[1])
                        exp.fulfill()
                    }
                }
            }
        }
        wait(for: [exp], timeout: 5)
    }

    func testRetrySuccessPreservesInterveningDraft() {
        let fake = FakeHostSettingsService()
        fake.snapshot = makeSnapshot()
        fake.previewImpl = { _, _ in self.makeValidPreview() }
        var attempts = 0
        fake.saveImpl = { params in
            attempts += 1
            if attempts == 1 { throw EngineClientError.unavailable("boom") }
            return HostSettingsSaveResult(saved: true, changed: true, documentID: params.documentID, previousContentHash: params.expectedContentHash, documentRevision: "rev-2", backup: nil, applicationEffects: [], contextRevision: "ctx-3")
        }
        let presenter = HostSettingsEditorPresenter()
        let exp = expectation(description: "retrypreserves")
        presenter.start(hostID: "h", service: fake) { _ in
            presenter.editRaw("a = 2")
            presenter.requestPreview { _ in
                presenter.beginSave { _ in
                    presenter.editRaw("a = 3")
                    presenter.retrySave { retried in
                        XCTAssertTrue(retried.applied)
                        XCTAssertEqual(presenter.rawEditorText(), "a = 3")
                        XCTAssertTrue(presenter.state.isDirty)
                        XCTAssertFalse(presenter.state.savePending)
                        XCTAssertNil(presenter.state.previewSummary)
                        var seenBase: HostContentHash?
                        fake.validateImpl = { params, result in
                            seenBase = params.expectedContentHash
                            return result
                        }
                        presenter.requestValidation { _ in
                            XCTAssertEqual(seenBase, .present("rev-2"))
                            exp.fulfill()
                        }
                    }
                }
            }
        }
        wait(for: [exp], timeout: 5)
    }

    func testSaveDisabledUntilValidPreview() {
        let fake = FakeHostSettingsService()
        fake.snapshot = makeSnapshot()
        let presenter = HostSettingsEditorPresenter()
        let exp = expectation(description: "disabled")
        presenter.start(hostID: "h", service: fake) { _ in
            presenter.editRaw("x")
            XCTAssertFalse(presenter.state.canSave)
            exp.fulfill()
        }
        wait(for: [exp], timeout: 5)
    }
}

final class FakeHostSettingsService: HostSettingsServing {
    var snapshot: HostSettingsSnapshot!
    var previewImpl: ((HostSettingsPreviewParams, HostSettingsPreviewResult) async throws -> HostSettingsPreviewResult)?
    var validateImpl: ((HostSettingsValidateParams, HostSettingsValidateResult) async throws -> HostSettingsValidateResult)?
    var saveImpl: ((HostSettingsSaveParams) async throws -> HostSettingsSaveResult)?
    private var cancelled = false

    func read(params: HostSettingsReadParams) async throws -> HostSettingsReadResult {
        HostSettingsReadResult(snapshot: snapshot)
    }

    func validate(params: HostSettingsValidateParams) async throws -> HostSettingsValidateResult {
        if let impl = validateImpl { return try await impl(params, HostSettingsValidateResult(valid: true, validationLevel: .schema, candidateContentHash: "c", candidateRawTOML: "a = 9", candidateStructured: snapshot.structured, diagnostics: [], contextRevision: "ctx-9")) }
        return HostSettingsValidateResult(valid: true, validationLevel: .schema, candidateContentHash: "c", candidateRawTOML: "a = 9", candidateStructured: snapshot.structured, diagnostics: [], contextRevision: "ctx-9")
    }

    func preview(params: HostSettingsPreviewParams) async throws -> HostSettingsPreviewResult {
        let fallback = HostSettingsPreviewResult(valid: true, validationLevel: .schema, candidateContentHash: "cand-1", candidateRawTOML: "a = 2", candidateStructured: snapshot.structured, diagnostics: [], contextRevision: "ctx-2", preview: HostSettingsPreview(previewID: "p-1", baseContentHash: .present("base-1"), candidateContentHash: "cand-1", changed: true, diff: "d", diffTruncated: false, changedFieldIDs: [], applicationEffects: [], protectedProjectionChanges: false))
        if let impl = previewImpl { return try await impl(params, fallback) }
        return fallback
    }

    func save(params: HostSettingsSaveParams) async throws -> HostSettingsSaveResult {
        if let impl = saveImpl { return try await impl(params) }
        return HostSettingsSaveResult(saved: true, changed: true, documentID: params.documentID, previousContentHash: params.expectedContentHash, documentRevision: "rev-2", backup: nil, applicationEffects: [], contextRevision: "ctx-3")
    }

    func cancel() { cancelled = true }
}

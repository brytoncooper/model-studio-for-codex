import Foundation

/// Typed serving boundary for `engine.v1.hosts.settings.*`.
///
/// Implementations authenticate through `ModelDeckEngineClient` and send
/// caller-supplied params verbatim, returning schema-validated, typed
/// results. Server-owned values (preview IDs, content hashes, base hashes,
/// context revisions, idempotency keys) propagate exactly; this boundary
/// never synthesizes or rewrites them and never retries a failed call.
public protocol HostSettingsServing: AnyObject {
    /// Calls `engine.v1.hosts.settings.read`.
    func read(params: HostSettingsReadParams) async throws -> HostSettingsReadResult
    /// Calls `engine.v1.hosts.settings.validate`.
    func validate(params: HostSettingsValidateParams) async throws -> HostSettingsValidateResult
    /// Calls `engine.v1.hosts.settings.preview`.
    func preview(params: HostSettingsPreviewParams) async throws -> HostSettingsPreviewResult
    /// Calls `engine.v1.hosts.settings.save` exactly once per invocation.
    func save(params: HostSettingsSaveParams) async throws -> HostSettingsSaveResult
    /// Cancels coordinated in-flight settings calls.
    func cancel()
}

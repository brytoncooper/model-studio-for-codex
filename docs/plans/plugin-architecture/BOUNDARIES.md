# Independence without speculative ports

Status: canonical clarification of the user's scope. Build a provider/host/platform-independent engine, using today's integrations and small fixtures. Do not design another named host or another operating-system app.

## What the core knows

The kernel knows extension identities, declared operations, grants and lifecycle. The engine knows application models, connections, sessions, capabilities, usage and run policy. Neither knows a provider URL convention, SDK class, Codex file layout, AppKit view, Keychain command, POSIX lock or local socket implementation.

Three independent edges exist:

| Edge | Current implementation | Core-facing boundary |
|---|---|---|
| Model execution | Existing OpenRouter/compatible HTTP and Cursor SDK adapters | ProviderExecution plus optional discovery/compaction/resume ports |
| Agent host | Existing Codex integration; headless fixture client | AgentHost capabilities and application operations; no assumed host permission model |
| Platform | Current macOS paths, credentials, process/lock/IPC and window services | Narrow injected platform ports; optional window attachment |

These are separate responsibilities, not one giant HostProvider interface. Concrete adapters are selected in composition code. Future authors can implement the relevant edge without changing domain rules; they still own proving their integration on their target.

## Minimum portable contracts

- `ApplicationPaths`: supply an explicit state/artifact root; engine builds no hard-coded home-directory paths.
- `InstanceLock`: acquire/release exclusive ownership with timeout/conflict semantics; concrete lock primitives stay at edge.
- `LocalTransport`: framed authenticated local duplex messages and disconnect/cancel behavior; engine operations have no socket/path assumption.
- `OwnedProcessSupervisor`: start and terminate only owned children using opaque handles, with bounded output/deadlines; no process signals in use cases.
- `CredentialStore`: resolve/store/delete scoped references through a privileged broker; existing helper remains the implementation.
- `AtomicFileWriter`: compare-before-write and explicit failure receipt for projection files; not a promise of multi-file atomicity.
- `WindowAttachment`: optional platform/presentation capability; core sessions work without it.

Define only methods required by actual consumers. Do not implement placeholder classes or choose APIs for hypothetical operating systems. The fixture implementations are deliberately minimal and exercise the contract, not an invented replacement product.

## Proof that the wall is real

1. Import and run engine use cases with the real Codex/provider/platform packages absent from the import path.
2. Start a minimal engine composition with an in-memory model repository, fixture provider, fixture host and temporary transport/storage adapters.
3. Perform list → start text run → receive events → cancel/get terminal outcome through the public API.
4. Register an external feature and invoke it without private engine imports.
5. Deliberately introduce forbidden imports and require the architecture checker to fail.
6. Keep the existing macOS/Codex/provider conformance fixtures green through extraction.

A provider or host capability may be unavailable; core startup must not require all installed integrations. The engine does not use a fallback provider simply because the requested one is missing. Unsupported authentication/tool/continuation semantics are explicit results, never guessed compatibility.

## Scope line

No new host-specific protocol, alternate UI framework, alternate OS package, native IPC implementation for another platform, or vendor compatibility investigation is required. The repository should make future ports possible through small owned adapters. This plan does not claim those ports already work. External integration avoids patching a host binary, but changes to an existing host's protocol can still require adapter maintenance.

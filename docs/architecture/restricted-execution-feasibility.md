# Restricted execution feasibility (B24)

## Purpose

Determine whether Model Deck can offer an optional restricted execution mode
for external plugin runtimes on macOS with actual OS-level enforcement, and
record what remains unproven. This document is feasibility evidence only. It
changes no runtime behavior, app privileges, signing, or entitlements.

## Ownership and contracts

- Owner: macOS platform security boundary (BACKLOG B24).
- Scope: this document only. No runtime, manifest, profile, or public-contract
  change is included.
- Engine invariant preserved: the plugin engine stays host/platform agnostic.
  macOS restriction knowledge lives behind the execution-profile seam and must
  not leak macOS specifics into engine contracts.
- Related contracts: `ProcessRuntimeConfig(argv, package_dir, ...)` launches
  exactly one explicitly named child with no shell, download, or discovery
  (`python/src/model_deck/plugins/process_runtime/`). Existing authority, path
  guards, and process ownership are application-level checks, not OS enforcement.
- B24 acceptance (BACKLOG) requires adversarial fixtures to be denied at OS
  level: sibling data, private socket, credential access, unauthorized
  subprocess, and network, including child inheritance. App API errors do not
  satisfy this gate. Unknown profiles must reject activation, and no manifest
  may be advertised as OS enforcement.

## Current repo facts

- `ProcessRuntime.spawn()` uses `subprocess` with verbatim argv and an
  explicit `package_dir`. There is no sandbox profile, entitlement, seatbelt
  flag, or restricted-profile adapter in the inspected source.
- Status note (`docs/plans/plugin-architecture/status/B19-B27.md`, B24
  section): no isolated OS sandbox prototype, qualified restricted-profile
  adapter, or adversarial OS-enforcement suite was located. G6/G7
  restricted-mode claims remain unsupported.
- Rollback position: trusted-executable mode only, with its boundary
  disclosed, until OS-denial evidence qualifies a restricted mode.

## Authoritative macOS mechanisms (read-only findings)

- App Sandbox confines an app via the `com.apple.security.app-sandbox`
  entitlement; it limits files, network, hardware, and user data per
  entitlement. A directly spawned child (fork/exec, e.g. `Process` /
  `subprocess` / `Popen`) inherits the parent's sandbox: sandboxing the
  engine would confine its children, but that confines the whole engine, not
  one plugin child selectively.
  Evidence: [App Sandbox](https://developer.apple.com/documentation/security/app-sandbox)
  and [violation diagnosis](https://developer.apple.com/documentation/security/discovering-and-diagnosing-app-sandbox-violations) (sandbox stops
  unentitled operations; remedy is extending the app entitlement, shipping a
  separate helper tool with the capability, or removing the access).
- Separating capabilities per child is a distinct mechanism from direct
  Process helpers: Apple documents (a) embedded helper tools inside a
  sandboxed app and (b) XPC services with their own bundle identifier,
  signature, and sandbox. An XPC service is an independently signed and
  entitled executable candidate: it does not require the containing app
  itself to be sandboxed, and its own sandbox/entitlement set governs what
  it can do.
  Evidence: [embedding a helper tool](https://developer.apple.com/documentation/Xcode/embedding-a-helper-tool-in-a-sandboxed-app)
  (helper tool target carries its own App Sandbox / Hardened Runtime
  capabilities; `com.apple.security.inherit` discussion) and
  [XPC service design](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingXPCServices.html).
- Hardened Runtime plus SIP protects runtime integrity (code injection, dylib
  hijacking, memory tampering), with narrow exception entitlements (e.g. JIT).
  It is not a file/network/socket confinement mechanism for plugin children.
  Evidence: [Hardened Runtime](https://developer.apple.com/documentation/security/hardened-runtime).
- `sandbox-exec` is DEPRECATED on this machine (`man sandbox-exec`: "Developers
  who wish to sandbox an app should instead adopt the App Sandbox feature").
  That deprecation alone is not proof that no enforcement path exists; it
  only removes ad hoc seatbelt-profile strings as a supported per-plugin
  confinement path for this repo. The supported split-privilege directions
  above (helper / XPC service) remain the candidates to qualify, and neither
  is proven for our runtimes here.
- Candidate for this repo, unproven: an independently signed and entitled
  XPC service or helper launcher that hosts or fronts plugin execution under
  its own sandbox, behind a new execution-profile adapter. Integration and
  runtime compatibility are unproven: whether arbitrary runtimes
  (Python/Node) run under such a helper without JIT/unsigned-memory
  exceptions, what exact entitlement set suffices, and what signing,
  packaging (B26 scope), notarization, performance, and debuggability costs
  apply. No such helper exists in the repo; no claim is made here.

## Decision-facing summary

- Narrow finding: the current unchanged `Popen`-family spawn path (verbatim argv,
  no profile, no helper, no XPC service) applies no OS restriction to the
  child. That is a statement about the current code path only, not a
  universal claim that no OS confinement is possible from an unsandboxed
  parent: the independently signed/entitled XPC/helper candidate above is
  explicitly left open and unproven.
- If restricted mode ships, the viable direction is a qualified
  runtime/package scope behind a new execution-profile adapter: a signed,
  entitled helper or launcher plus per-profile denial evidence, with
  everything else remaining in explicitly trusted mode. Arbitrary runtimes
  stay in trusted mode unless proven otherwise.
- No sandbox, profile, or enforcement is claimed here. Do not describe the
  manifest, path guards, subprocess ownership, or API errors as OS-level
  restriction.

## Minimal experiment specification (not run)

Run only as a later isolated fixture batch with temporary targets; do not run
against live app data, credentials, sockets, or the installed app.

1. Fixture layout: temp parent dir, temp sibling dir with a canary file, temp
   private socket path, temp credential file and reachable loopback network
   target. Each target is created only for the experiment and removed after.
2. Positive baselines first: run each adversarial attempt unconfined and
   record that it succeeds (network connection opens, file/socket reads
   succeed, direct unauthorized subprocess launch succeeds), proving the
   fixture is capable of the operation before any denial is claimed.
3. Subjects: (a) current `ProcessRuntime` child as control (expected: no OS
   denial); (b) candidate restricted launcher, only if a signed/entitled
   helper exists (expected: OS denial with a sandbox/entitlement violation,
   not an app error).
4. Adversarial child attempts, each asserting denial origin: read sibling
   canary, connect to private socket, read credential file, open a network
   connection, and directly execute an unauthorized subprocess binary.
   Separately, test descendant inheritance: a nested subprocess repeating
   the reads from inside an already-restricted child, distinct from the
   direct unauthorized-execution denial.
5. Pass criteria per attempt: operation fails with an OS enforcement trace
   (sandbox violation log / entitlement denial), the nested child inherits the
   restriction, and bypass via inherited file descriptors is tested and
   recorded.
6. Failure handling: any attempt that fails only with an app API error, or
   any nested child that escapes the restriction, disqualifies the profile.
   Rollback is disabling the profile and disclosing trusted-mode boundary.

## Limitations and still-needed qualification

- This document provides no execution evidence: no fixture was run, no denial
  log captured, no signature/entitlement inspected, per task bounds.
- Unknowns: which runtimes (Python, Node, other) can run under a qualifying
  helper without JIT/unsigned-memory exceptions; exact entitlement set;
  performance and debuggability cost; notarization impact.
- Qualification path: implement the isolated harness and adversarial suite in
  a separate slice, capture OS-denial logs per attempt, then root reviews
  before any restricted-mode claim or B26 packaging dependency.

## Extension instructions

- Keep macOS restriction logic behind the execution-profile seam; do not add
  platform conditionals to engine or provider contracts.
- Unknown or unqualified profiles reject activation; trusted mode stays the
  default with its boundary stated.
- Future authors: append dated evidence entries (fixture commit, denial logs,
  runtime versions, signing identity type) rather than rewriting this
  feasibility conclusion.

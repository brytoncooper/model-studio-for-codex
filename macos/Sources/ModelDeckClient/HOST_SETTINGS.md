# Host settings (native typed contract)

Purpose: `Codable`/`Equatable` request, result, draft, and descriptor types
for `engine.v1.hosts.settings.read`, `.validate`, `.preview`, and `.save`,
matching the frozen schemas in `contracts/common/host_settings.schema.json`
and `contracts/engine.v1/methods/hosts.settings.*` exactly.

Ownership: `HostSettingsTypes.swift` (types) and
`Tests/ModelDeckClientTests/HostSettingsTypesTests.swift` (tests). No UI,
transport, `Package.swift`, or schema edits belong to this slice.

Contracts: snake_case wire keys; optional fields decode as missing (never as
substituted defaults); `content_hash` is `absent` or `sha256:<lower64>` via
`HostContentHash`. Valid validate results require all three candidate
fields; valid preview results additionally require a non-null `preview`, and
invalid preview results require `preview: null` -- all enforced in the
decoders, mirroring the schema `oneOf` rules.

Invariants: sensitivity is structural. `sensitivity == "secret"` decodes into
`HostSecretFieldDescriptor`, which has no `value`/`default`/`effective`/
`entries` members, and decoding rejects those keys when present. `unset`
changes carry no value by construction and decoding rejects a `value` key on
`unset`. There is no Codex field allowlist; field and entry IDs are opaque
strings owned by the host adapter. Raw TOML and diff text stay in their
payload strings; these types expose no textual descriptions of themselves
and must never enter logs, diagnostics, or failure messages.

Extension: add new operations or fields only after the corresponding contract
schema freezes; keep nullability (omitted vs explicit null) identical to the
schema, and keep secret content out of the secret descriptor type.

Tests: fixture-backed `decodeValidated` checks against the committed schemas
plus in-test `Codable` roundtrips for raw drafts, structured drafts,
context revisions, and preview objects, including secret-descriptor and
unset-change key-absence checks. Run `swift test --filter
HostSettingsTypesTests` from `macos/`; never build the app or installer for
this slice.

Limitations: types model known shapes only; unknown wire fields are rejected
at schema validation, not here. Per-type value-shape rules (e.g. boolean
values for boolean fields) are adapter-enforced, not reimplemented.

## Required-nullable wire keys

`preview` on preview results and `backup` on save results are required keys
that may be null. Decoders reject a missing key and encoders always emit an
explicit null, so re-encoded wire validates against the frozen schemas.

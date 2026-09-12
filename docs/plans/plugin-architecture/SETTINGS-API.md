# Settings API contract (v1 proposal)

Status: proposal for root review; freezes `engine.v1.hosts.settings.*` before schema codegen and consumer work. Conventions follow `API.md`: JSON-RPC 2.0, numeric protocol errors, application error vocabulary (`invalid_argument`, `capability_denied`, `not_found`, `conflict`, `version_mismatch`, `resource_exhausted`, `unsupported_capability`, `internal`), encoded request AND response frames capped at 1 MiB each, no batch requests. All objects reject unknown top-level fields.

## 1. Operations and authorization

| Method | Required grant | Effect |
|---|---|---|
| `engine.v1.hosts.settings.read` | `hosts.settings.read` | Read-only snapshot, including `raw_toml` |
| `engine.v1.hosts.settings.validate` | `hosts.settings.read` | Dry-run; returns `candidate_raw_toml` + `candidate_structured`, no write |
| `engine.v1.hosts.settings.preview` | `hosts.settings.read` | Validation plus server-computed diff and single-use `preview_id`, no write |
| `engine.v1.hosts.settings.save` | `hosts.settings.write` | Validated atomic write with CAS + receipt |

All four operations are `local_operator`-only: the caller's server-assigned principal must be the local operator. `host_session` and `plugin_activation` principals cannot read or write this document. Plugin grants alone never authorize these methods, even when the plugin holds `hosts.settings.read` or `hosts.settings.write`.

## 2. Identity and revision fields

| Field | Type | Meaning |
|---|---|---|
| `host_id` | reverse-domain string, 1..128 | Host selector (e.g. `com.openai.codex`). Never a filesystem path. |
| `document_id` | opaque server-issued string, 1..256 | Document selector issued by `read` (e.g. `ref:codex-user-config`). Callers echo verbatim; later calls use it, never a path. |
| `document_revision` / `expected_content_hash` / `base_content_hash` | `absent` \| `sha256:<lower64>` | SHA-256 over exact document bytes; `absent` means the document does not exist (distinct from an existing empty file). Lowercase hex, exactly 64 chars. |
| `context_revision` | opaque string, 1..256 | Schema/protection-context version coupling validation and preview. Clients echo verbatim. |
| `preview_id` | opaque string, 1..256 | Server-issued by `preview`; bound to principal + `host_id` + `document_id` + base hash + `context_revision` + candidate hash. Mandatory for `save`; single-use, consumed only on successful save. |
| `idempotency_key` | string, 1..128, client-generated | Deduplicates `save` replays for at least 24h by principal + operation + key + request fingerprint. |

Cooperative CAS: the engine compares against its last observed document state; external writers outside the engine are detected only when observed, and concurrent external edits surface as `conflict`, never as silent merge or automatic rebase.

## 3. Generic envelope rule

The engine owns authentication, request bounds, dispatch, domain-error mapping, and the generic envelopes below. It treats host IDs, document IDs, field IDs, entry IDs, schema IDs, TOML text, descriptors, and diagnostics as opaque host-settings data. The Codex host adapter owns Codex keys and aliases, supported-version reference data, precedence discovery, TOML parsing and lossless writing, setting descriptions and constraints, sensitive-field classification, effect/restart descriptions, and B09 projection protection. There is no root-owned Codex field allowlist: descriptors and addresses are adapter-supplied. Generic `field_id`/`entry_id` values are exact opaque strings; the native client never constructs Codex TOML paths.

## 4. Field model

### 4.1 Structured descriptors

`read` returns ordered sections (max 64); each section has opaque `section_id` (1..128), title (1..256), optional description (<=2048), and ordered fields (max 256 descriptors per snapshot, application-enforced). Each field descriptor is exactly: `field_id` (1..128, exact opaque string), `label` (1..256), optional `key` display (1..256), optional `description` (<=2048), `type`, `value_state`, `editability`, `application_effect`, `sensitivity` (`public | secret`), optional `constraints` object, plus value displays or entry groups per the rules below. Unknown properties are rejected:

- `value_state`: `unset | explicit | inherited | managed | unknown`.
- `editability`: `editable | managed | projection_owned | unsupported`.
- `application_effect`: `immediate | future_session | host_restart | model_deck_restart | unknown`.
- Value types are `boolean`, `integer`, `number`, `string`, `enum`, `string_list`, and `entries`. Present `value`/`default`/`effective` displays must match their declared type: strings are at most 2048 characters; enums are boolean, number, or bounded string scalars; string lists contain at most 256 bounded strings. Adapter validation applies descriptor-specific constraints and enum membership. Nested tables and arrays use stable entry IDs.
- `constraints` (optional, unknown properties rejected): `minimum`/`maximum` (finite numbers), `min_length`/`max_length`/`max_items` (nonnegative integers), `choices` (max 256 entries of `{ value: boolean | integer | number | string (<=2048), label: 1..256 }`). No host-owned key allowlist exists; constraints are adapter-supplied.
- Entries: Public `type: "entries"` requires an `entries` array (max 256 total per snapshot, application-enforced) of `{ entry_id: 1..128, label: <=256, fields: [field descriptors] }`; max nesting depth 4 (application-enforced). Secret entry containers omit `entries` entirely and expose only `configured`; this prevents nested public descriptors from exposing secret-container content. Non-`entries` types forbid `entries`. `field_id`/`entry_id` are exact opaque strings.
- Structured `changes` (max 64): `{ field_id, entry_id?, operation: set | unset }`; `set` requires a JSON `value`, `unset` forbids `value`.

Secret fields are redacted everywhere in structured mode and diagnostics: structured values, defaults, and effective displays are omitted and replaced by a `configured` boolean; diagnostics identify locations and field IDs but never include literal values or source lines; display diffs mask secret lines as `<redacted>`. Raw mode is the explicit exception: the authorized local-operator editor receives complete `raw_toml` / `candidate_raw_toml` bytes, which remain sensitive local-operator content (see section 7).

### 4.2 Draft union (validate/preview)

Exactly one draft form per request; sending both or neither is `invalid_argument`:

```json
{"kind":"raw","raw_toml":"complete candidate document"}
```

or

```json
{"kind":"structured","changes":[{"field_id":"...","operation":"set","value":8},{"field_id":"...","entry_id":"...","operation":"unset"}]}
```

`operation` is `set | unset`. Duplicate entries for the same field/entry, or conflicting operations on the same target, are rejected with `invalid_argument`. Unknown field/entry IDs are `invalid_argument`. Structured edits apply to the exact base TOML bytes, preserving all nodes the adapter did not own. Every validate/preview result returns the candidate in both `candidate_raw_toml` and `candidate_structured` forms (omitted only when parsing cannot produce them) so the editor can switch tabs without reparsing TOML.

### 4.3 Preservation and protection

Untouched bytes, comments, unknown fields, and protected-subtree bytes are preserved byte-for-byte. B09 remains the only writer for generated Codex model projections; the adapter enforces a B09-supplied projection-protection policy. A candidate changing a managed restriction or B09-owned projection is invalid and never reaches save (`capability_denied` / `protection_denied` naming the descriptor or TOML path, never secret content).

## 5. Requests and results

### read

Params: `{ host_id }`. Result `snapshot`: `{ host_id, document_id, document_revision, exists, target { display_name, display_path, scope, writable }, schema_profile { schema_id, schema_revision, host_version, support_level, reference_url }, precedence [{ layer_id, label, editable, status }], raw_toml, structured { sections, unrepresented_paths } }`. `support_level` is `supported | toml_only | unavailable`; unknown versions return `toml_only` with empty sections, intact raw TOML, and a warning (TOML-syntax and projection rules only, no invented defaults). `precedence` layers are adapter-provided strings with `status` `active | inactive | unknown`; the UI reports "effective value unknown" when a higher layer cannot be observed. `scope`, `layer_id`, and ordering are adapter-provided; the engine assigns no Codex precedence.

### validate

Params: `{ host_id, document_id, expected_content_hash, context_revision, draft }`. The adapter rereads the target and checks the expected hash first; mismatch is `conflict`, never a silent rebase. A `context_revision` that no longer matches the current schema/protection context is `conflict`. Result: `{ valid, context_revision, validation_level (schema | toml_only), candidate_content_hash, candidate_raw_toml, candidate_structured, diagnostics: [<max 32>] }`. Syntax and known-setting failures are successful results with `valid: false`; malformed envelopes are `invalid_argument`. Each diagnostic: `{ severity, code, message, field_id?, line?, column? }` with no literal values or source lines. No `preview_id` issued.

### preview

Params match validate (including the required `context_revision` check). Preview repeats validation and the current-hash comparison; it has no write side effect. Invalid content returns `valid: false` with the validation payload plus `preview: null` and no token. Successful validate/preview results require all three candidate fields; successful preview requires a non-null preview object. Valid content returns the validation payload plus `preview: { preview_id, base_content_hash, candidate_content_hash, changed, diff, diff_truncated, changed_field_ids, application_effects, protected_projection_changes }`. The diff is deterministic for the same base and candidate bytes. Preview creates no backup, creates no directories, and never tests providers, launches a host, or restarts a process.

### save

Params: `{ host_id, document_id, expected_content_hash, preview_id, candidate_content_hash, candidate_raw_toml, idempotency_key }`. Behavior, in order:

1. Exact idempotent replay first: identical principal + `idempotency_key` + request fingerprint returns the original result unchanged (even though the caller's expected hash is now old). The server records only principal, method, key, fingerprint, hashes, and safe result metadata; candidate TOML and diff text are never persisted in the idempotency record. Same key with a different request is `conflict`.
2. Unknown or malformed `preview_id` is rejected; a consumed `preview_id` returns `conflict` (`preview_consumed`). A missing `preview_id` is `invalid_argument`.
3. Staleness: `expected_content_hash` mismatch returns `conflict` (external edit); schema/protection context change returns `conflict`. Validation never silently rebases.
4. The server recomputes `sha256(candidate_raw_toml)`, verifies it equals `candidate_content_hash` and the hash bound to `preview_id` (principal/host/document/base/context/candidate); mismatch is `conflict`. Then it revalidates the exact candidate; failure is `invalid_argument` carrying the safe diagnostics (never a `valid: false` result and never a write).
5. Under an exclusive lock: reread the target, verify every parent component is a directory and neither the target nor any parent is a symlink, require the target to be absent or a regular file (anything else is refused), compare the expected hash again, create the durable pre-write backup (exact bytes, mode `0600`, beneath the injected Model Deck state root, identified by a safe reference, never exposed through plugins or exports), write a mode `0600` temporary file in the target directory, fsync it, atomically replace the target, fsync the directory, and verify the resulting hash.
6. `preview_id` is consumed only on success. A byte-identical candidate returns `saved: true, changed: false`, the same revision, and `backup: null`.

Result: `{ saved, changed, context_revision, document_id, previous_content_hash, document_revision, backup { backup_id, display_path } | null, application_effects }`. Saving never restarts Codex or Model Deck. A post-replace verification failure produces a recoverable error with the retained backup; there is no unsafe automatic rollback after replacement. Injected failures before replace leave the original intact.

Domain errors use the existing vocabulary: `capability_denied` (not a local operator with the grant), `not_found` (host/document ID unknown), `conflict` (external edit, changed protection, consumed preview, idempotency reuse), `version_mismatch` (document/schema identity no longer applicable), `unsupported_capability` (no settings adapter, or structured op on `toml_only`), `resource_exhausted` (source or candidate exceeds a bound), `internal` (safe write/backup failure message with no TOML, secret, or raw exception text).

## 6. Transport bounds and errors

- Encoded JSON-RPC request frames and response frames each cap at the existing 1 MiB limit (per `API.md`); attachments larger than a frame use scoped handles, which this contract does not define. `raw_toml` (256 KiB UTF-8 source cap) and `diff` (128 KiB display cap) fit inside the frame only for small documents; oversize input or output is rejected with `resource_exhausted`, never by raising the transport cap. No claim is made that any fixed field count fits a frame.
- The source cap is 256 KiB UTF-8, enforced before parsing or echoing content. The preview diff is capped at 128 KiB with `diff_truncated: true` and the prefix returned; the candidate hash and raw candidate remain authoritative. Truncation applies to the display diff only, never to the saved source bytes.
- Explicit frozen limits: descriptors per snapshot 256; sections per snapshot 64; structured `changes` 64; `diff` 128 KiB with `diff_truncated`; `raw_toml` 256 KiB UTF-8; `diagnostics` 32 (each: `code` <=128, `message` <=2048); `preview` changed-field IDs 64; `application_effects` 16; precedence layers 64; `unrepresented_paths` 256 (each <=2048); `choices` 256; `field_id`/`entry_id`/`section_id` 128 chars; `label` 256; `description` 2048; `host_id` 128; `document_id` 256; `context_revision`/`preview_id` 256; `idempotency_key` 128; entry nesting depth 4 and entry total 256 (both application-enforced); subscriber/event caps per `API.md`.
- Error `data`: `{ code, retryable, request_id }` plus non-sensitive fields only (`field_id`, hashes, `application_effects`). `conflict` and `preview_consumed` suggest re-read, re-validate, re-preview. `resource_exhausted` is not retryable with the same payload.

## 7. Sensitivity rule

Raw TOML and raw diffs are sensitive local-operator content. They may cross the authenticated local socket for this feature but must never enter request logs, diagnostics, events, telemetry, idempotency records, state exports, or test-failure messages. Receipt metadata carries hashes and safe result fields only, never raw content or secret values.

## 8. Non-goals

No shared-schema edits, codegen, Python/Swift consumer changes, Git operations, live-settings mutation, toolchain changes, new providers, or new OS support in this document.

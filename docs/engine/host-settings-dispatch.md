# Host settings dispatch integration

`EngineDispatch` routes the four frozen `engine.v1.hosts.settings.*` wire
methods (`read`, `validate`, `preview`, `save`) to the accepted
`model_deck.engine.host_settings.HostSettingsService`. This file owns the
registration blocks only; the service, schemas, adapters, and bootstrap
wiring live elsewhere.

## Wiring

- `EngineDispatch(..., host_settings=None, host_settings_caller=None)`:
  inject a configured `HostSettingsService` plus the explicit server-side
  `CallerContext` for the local operator to enable the four methods.
  Either default `None` denies settings: no service reports
  `unsupported_capability`, and no trusted operator context reports
  `capability_denied` (`host settings operator context not
  configured`). The injected context must match the service's
  configured local operator; it is server composition, never request
  JSON.
- When configured, the methods appear in `engine.v1.operations.list`
  via four catalog entries pointing at the frozen
  `contracts/engine.v1/methods/hosts.settings.*` schema ids.
- No Codex keys, settings files, rendezvous, or endpoint state are
  touched here. No bootstrap change is included: there is no durable
  preview/receipt ledger or host adapter to construct yet, so bootstrap
  keeps passing nothing (legacy `None`).

## Caller identity

- After the existing `hello` auth gate, dispatch uses the exact
  injected `host_settings_caller` verbatim: never the `client_name`
  derived principal, and never manufactured `READ`/`WRITE` grants.
  Nothing is read from request JSON. The enrollment credential in this
  slice is operator-only, so any connection presenting it is the
  operator; renaming `client_name` with the same credential cannot
  change settings access (covered by test). A wrong credential fails
  the auth gate and the adapter is never invoked. Unauthenticated
  connections are rejected before dispatch (`authentication required`).
- The local-operator check stays inside the service (`principal ==
  local_operator_principal` plus grant check), so composition must
  keep the injected context identical to the service's local
  operator. Host/plugin credential auth is NOT claimed here; that is
  a separate authority-supervisor concern. Generic run-principal
  behavior is untouched by this slice: operator-only scope applies to
  the four settings methods only.

## Error mapping

Service errors map to fixed wire codes with the service's safe
messages (raw TOML never appears in errors, logs, or the outbox):
`SettingsDeniedError` to `capability_denied`, `SettingsNotFoundError`
to `not_found`, `SettingsConflictError` to `conflict` (the safe ledger `reason`,
e.g. `preview_consumed`, is folded into the fixed message text as
`an allowlisted fixed message (preview_consumed keeps its fixed text; any unknown reason maps to generic `settings save conflict` with no echo)`; no extra `error.data` field, per
the frozen `contracts/common/error.schema.json` with
`additionalProperties: false`), SettingsVersionMismatchError` to
`version_mismatch`, `SettingsUnsupportedError` (unknown host or
missing adapter path) to `unsupported_capability`,
`SettingsExhaustedError` to `resource_exhausted`,
`SettingsInvalidError` to `invalid_argument`, and
`SettingsInternalError` (or any unexpected failure) to `internal`.

## Tests

Run `PYTHONPATH=python/src /tmp/md-b18-venv/bin/python -m unittest
python.tests.engine.test_host_settings_dispatch` (12 tests, ephemeral
fixture transport only): operations listing, framed authenticated
`read`, framed `validate`/`preview`/`save` against a fake
`SettingsDocument` plus in-memory ledgers, unauthenticated denial,
wrong-credential denial with the adapter untouched,
client-rename-keeps-access, missing-operator-context denial, missing-service
`unsupported_capability` for all four methods, unsupported-host
mapping, consumed-preview `conflict` naming `preview_consumed` in the fixed
message with no extra schema field, and
a no-raw-TOML-in-errors check, and an unknown secret-long (2100-char) conflict-reason test asserting the generic fixed message, frozen error-schema validity, no `reason` field, no secret substring, and a 2048-byte wire cap.

## Limits

- Real ledger durability, host adapters, and bootstrap construction
  are out of scope for this slice.
- Every failure response validates against the frozen
  `contracts/common/error.schema.json`; tests assert this for each
  denial path.

# Provider continuation

This package owns B15 provider-private continuation state and the pure
compaction/translation helpers shared by the existing V2 provider paths. It
does not own Codex thread history, provider credentials, provider dispatch, or
generic job resume.

## State and scope

`ContinuationStore` is a private SQLite adapter at an explicit application
path. The V2 OpenAI-compatible profile is composed with
`<state-root>/engine/provider-continuation.sqlite3`; constructing the adapter
does not create that path before the application's root-safety validation.
The directory and database are owner-only, symlinks and changed inodes are
rejected, metadata is bounded, and exception/repr text excludes stored content.

Each session is bound on its first non-empty save to all nine trusted values
captured by the engine: session, connection and connection revision, provider,
provider model, execution mode, endpoint configuration reference, credential
reference, and the engine-issued opaque continuation handle. Request bodies
and provider workers cannot select this scope. Changing any value refuses the
load rather than falling back to a fresh request. Another session cannot load
the records. Empty first turns do not bind a session.

Completed provider responses are written atomically in provider item order.
The store preserves the complete raw provider item—including native IDs,
encrypted reasoning/function arguments, and signatures—beside a stable visible
identity. The next request matches records to normalized visible history in
order and restores private fields only for the same response/scope. Missing,
ambiguous, corrupt, schema-incompatible, or identity-incompatible required
state fails before provider HTTP. Failed, incomplete, or cancelled responses
are never installed.

Response ordering is assigned transactionally by the store rather than derived
from wall-clock timestamps, so repeated or backward-moving clocks cannot swap
provider-private items between otherwise identical visible responses. An
explicit engine session reset prepares a durable intent only when the persisted
session id and engine-issued handle match. The old records remain recoverable
until the application session commit succeeds; commit retires them, while a
crash after that commit lets the replacement scope consume the pending intent
and bind fresh state. Existing engine databases receive an additive nullable
`continuation_scope_json` column migration before session queries run.

Records survive an engine restart because the store is reopened from the same
isolated path. There is no TTL in B15. A decoded Model Deck compaction summary
is an explicit checkpoint barrier: pre-compaction provider records are cleared
before the post-compaction request, and only fresh successful output can become
new continuation state. Summary-generation output itself is never saved as
ordinary continuation. The legacy store/codec remains untouched and readable;
there is no live-state migration.

## Compaction and host boundary

`compaction.py` preserves the legacy router marker and summary codec. The Codex
bridge supports both existing entry forms: streamed `POST /v1/responses` with
`compaction_trigger`, and unary `POST /v1/responses/compact`. Both start a
tool-less summary on the selected route/model. Exactly one compaction item is
returned only after `run.completed` and a non-empty summary. Partial text
followed by failure, interruption, cancellation, or an empty result is
discarded and cannot replace history.

Host-visible summaries are not portable encrypted provider state. The bridge
decodes only its own marker. Opaque reasoning received from the host is stripped
at the provider-neutral normalization boundary; the compatible provider
adapter restores it from this private scoped store. A route change therefore
cannot forward foreign ciphertext. `translate.py` retains the legacy foreign
provider stripping and rejected-reasoning healing behavior.

Cursor does not expose a verified portable native resume API in the pinned SDK.
Its B15 mechanism remains Codex-owned normalized full-history replay between
turns plus the existing within-run tool callback suspension. A non-null native
continuation handle is explicitly refused. Cursor compaction still uses the
same host-owned summary flow; no SDK handle is fabricated.

Cache accounting is separate. Stable history and tools may allow a provider to
report cached input, but cache reuse neither proves nor replaces continuation.
B15 fixtures do not manufacture cache hits and leave unavailable metrics absent.

## Compatibility matrix

| Legacy behavior | B15 contract and evidence |
|---|---|
| Private response item metadata survives a follow-up | `test_same_scope_tool_resume_restores_private_function_fields` and `test_responses_restore_complete_raw_item_and_reasoning_order` restore IDs, encrypted arguments, signatures, and ordered reasoning. |
| Provider/account/model scope is not global | Store scope-mismatch/session-isolation tests plus `test_different_scope_and_missing_record_fail_before_http` refuse reuse before HTTP. |
| Streamed trigger compaction | `test_streamed_compaction_emits_one_local_item_and_withholds_tools` uses the bridge's real streamed entry. |
| Unary compact endpoint | `test_unary_compaction_returns_only_item_shape` uses the bridge's real unary entry. |
| Failed summary preserves history | `test_failed_compaction_with_partial_text_returns_no_item` proves partial failed output is not emitted or installed. |
| Tool callback continuation | `test_codex_bridge_continues_tools_and_resumes_after_compaction` crosses bridge, engine, provider and store; Cursor's `test_account_parameters_tools_and_active_run_reuse` covers its actual SDK harness callback mechanism. |
| Compaction followed by continued work | The integrated bridge test performs first response, private-state restoration, tool call/result, summary compaction, barrier clearing, and a successful post-compaction response. |
| Restart retention | `test_store_reopen_reuses_state_and_compaction_clears_it` reopens the real SQLite adapter and restores scoped state before the compaction barrier. |
| Foreign provider reasoning stripping/healing | `test_translate_healing` fixtures and the bridge opaque-reasoning test cover explicit stripping without logging ciphertext. |

From `python/`, run:

```sh
PYTHONPATH=src python -m unittest discover -s tests/providers_continuation
```

The combined B15 gate additionally includes the OpenAI-compatible execution,
Codex host bridge, engine session/run persistence, and focused Cursor runtime
fixture suites. No fixture result is described as live provider qualification.

## Limitations

- Stored provider content is protected by local file ownership and scope, not
  encrypted at rest; B15 intentionally does not redesign secret management.
- Same-user processes are outside the static filesystem-race threat model.
- No TTL or provider-native Cursor resume is claimed.
- The public `engine.v1.sessions.compact` catalog entry is not used by the
  Codex compatibility endpoints. Those endpoints return their compaction item
  directly; generic public job-backed compaction still depends on the B19 job
  runner/history contract and is not fabricated here.
- Live provider cache/opaque-state behavior was not requalified for B15.

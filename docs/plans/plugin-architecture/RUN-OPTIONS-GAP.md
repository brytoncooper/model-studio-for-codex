# Run options: implementation and remaining parity work

The shared contract is implemented. Engine admission, serialization and SQLite
recovery are implemented, independently reviewed and committed. Provider-specific mappings
and end-to-end behavior remain unfinished.

## Contract

Both `runs.start` and normalized provider requests use the shared `run_options`
definition in [the vocabulary](../../../contracts/engine.v1/vocabulary.schema.json).
It supports instructions, reasoning effort, service tier, maximum output tokens,
output format, tool choice and parallel tool calls. Unknown fields and explicit
null values are rejected. Omission and an empty object have the same admission
identity; explicit false, empty instructions and standard service tier survive.

The engine owns validation, request identity and durable retention. Providers
own translation into their supported execution protocols. Host-specific names
and provider defaults must not enter the core contract.

## Implemented engine path

- Typed options and a shared schema-backed codec detach caller-owned structures.
- Admission counts options toward the combined request limit and hashes nonempty
  options while retaining the historical hash for omitted options.
- Named tool choices must reference an authorized declared tool. Choosing no tools
  suppresses provider-facing tools while retaining the full requested tool set in
  the admission identity.
- SQLite stores a versioned options envelope. Only a legacy SQL NULL defaults to
  empty options; malformed present data fails closed. Recovery carries options
  for unclaimed runs and interrupts claimed runs without redispatching them.

Focused tests cover these boundaries. See the
[storage guide](../../../python/src/model_deck/adapters/storage/RUN_OPTIONS.md)
and [delivery checklist](STATUS.md) for acceptance status.

## Remaining provider and host work

Reuse the existing Cursor SDK model-selection logic and OpenAI-compatible
translation helpers. Connect normalized options to those adapters and verify
instructions, reasoning, Fast/service tier, output format, tool choice, parallel
calls and output limits against existing behavior.

Preserving a preference is not proof that a provider enforces it. Cursor reasoning
may be advisory; prompt guidance alone does not guarantee structured output or
required tool invocation. Document supported behavior and reject unsupported
requirements deliberately before dispatch rather than silently inventing support.
Resolve these mappings against existing behavior before claiming parity.

Host conversion, provider execution, persisted recovery and usage accounting need
cross-system tests. This work does not implement another host or operating system.

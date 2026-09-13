# `model-deck invoke` — B23 generic kernel operation invocation

The `invoke` subcommand is a thin, generic gateway to any kernel operation the
engine advertises through `engine.v1.operations.list`. It does not own
validation, authorization, or routing logic — the engine does. The CLI's job is
to:

1. load the rendezvous descriptor and authenticate,
2. call `engine.v1.operations.list` with `params = {}`,
3. validate the listing against the bundled `contracts/engine.v1/methods/operations.list.result.schema.json`,
4. dispatch the requested operation as a direct JSON-RPC method.

## Usage

```
model-deck invoke <operation-id> \
    --rendezvous <absolute-path> \
    --credential <absolute-path> \
    [--input-file <absolute-path>]
```

* `--rendezvous` — absolute path to the rendezvous JSON published by the engine.
* `--credential` — absolute path to the operator enrollment credential.
* `--input-file` — absolute path to a strict bounded JSON object (optional;
  defaults to `{}`).

The exit code is `0` on success and a fixed `1` for every failure mode
(invalid rendezvous, failed authentication, malformed input, missing
discovery match, denied grant, disconnected engine, malformed result). The
exact reason is written to stderr; the success result JSON is written to
stdout. Neither stream echoes the credential contents or the parsed input.

## Input file contract

When `--input-file` is provided, the file is read and validated **before any
socket is opened**. Rejections:

| Condition                              | Result                         |
|----------------------------------------|--------------------------------|
| `len(raw_bytes) > MAX_FRAME_BYTES`      | exit 1, "input file exceeds frame budget" |
| non-UTF-8 bytes                        | exit 1, "input file is not valid UTF-8"    |
| not valid JSON                         | exit 1, "input file is not valid JSON"     |
| JSON followed by trailing data         | exit 1, "input file has trailing data"    |
| duplicate object keys                  | exit 1, "duplicate key in input object"   |
| non-object top-level value             | exit 1, "input file must contain a JSON object" |
| non-finite number (NaN, ±Inf)          | exit 1, "non-finite numbers are not allowed" |
| nesting depth > 64                     | exit 1, "input exceeds depth limit"        |
| total node count > 200,000             | exit 1, "input exceeds node limit"         |
| non-string object keys                 | exit 1, "input object keys must be strings" |

The 1 MiB byte budget is reused from `model_deck.adapters.transport.framing.MAX_FRAME_BYTES`
so the engine frame cap and the CLI input budget stay aligned.

**Preserved values** (these are legitimate values, not rejection triggers):
`false`, `0`, `""`, `null`, `[]`, `{}`. Empty strings, the integer zero, and the
boolean `false` all pass through unchanged. The same `strict bounded JSON`
guarantees used by the engine's frame pipeline apply here.

## Discovery contract

`engine.v1.operations.list` is called with `params = {}`. The CLI then
validates the response against
`contracts/engine.v1/methods/operations.list.result.schema.json` via
`validate_schema_ref`. Validation failure is an exit 1 — the engine is the
authoritative source and the CLI does not fabricate operations.

The requested `<operation-id>` must be advertised by the engine **exactly
once** in the listing. Zero matches or multiple matches both exit 1
("operation not advertised: ..."). No arbitrary kernel-private calls are
issued.

When the operation is found, the CLI issues a direct JSON-RPC call with
`method = <operation-id>` and `params = <parsed input file>`. The engine's
existing kernel dispatch routes `_kernel_methods` through `_invoke_kernel`,
so the CLI does not need (and must not introduce) an `operations.invoke`
wrapper.

## Result handling

On success, the result is serialized with `json.dumps(..., indent=2)` and
written to stdout. If the engine returns an error envelope, the
`error.message` field is written to stderr and the exit code is 1. If the
result is not a JSON value the CLI can serialize, the CLI exits 1
("engine returned an invalid result") and writes nothing to stdout.

## Authentication

The CLI reuses the existing `_negotiate_and_authenticate` flow used by
`models list` and `runs fixture-text`. The credential file is read only
after the engine publishes its hello-1 challenge — never before.

## Honest guarantees and limitations

* No automatic retry is performed. A write that fails does not get
  re-sent. (Discovery and reads are single-attempt; writes are
  single-attempt.)
* Authentication must succeed before discovery is attempted.
* Operations must be advertised exactly once. The CLI never invents
  operations or bypasses the engine's validation/authorization layer.
* The input file is bounded by the same `MAX_FRAME_BYTES` the engine uses
  for framing. A larger payload is rejected without contacting the engine.
* Streaming, batching, and progress reporting are out of scope for the
  `invoke` subcommand. Use `runs fixture-text` for streaming output.

## Examples

```
# discover available operations first (engine provides a separate listing tool
# or run engine.v1.operations.list by hand) — pick one id.

model-deck invoke com.example.plugin.run \
    --rendezvous /var/run/model-deck/rendezvous.json \
    --credential /var/run/model-deck/operator_credential \
    --input-file /tmp/run_input.json
```

`run_input.json` is a strict JSON object, e.g.

```json
{
  "flag": false,
  "count": 0,
  "label": "fixture",
  "items": []
}
```

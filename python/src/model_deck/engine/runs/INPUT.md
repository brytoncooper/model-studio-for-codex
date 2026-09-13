# Normalized run input

## Purpose and ownership

The run engine owns a provider-neutral, ordered conversation-item contract.
Host adapters convert native history into this contract before admission;
provider adapters translate it into their own wire format without silently
dropping content. This module does not define Codex JSON-RPC or provider HTTP
payloads.

The canonical item schema is
`contracts/engine.v1/vocabulary.schema.json#/definitions/normalized_input_item`.
`input_codec.py` is its Python boundary:

```python
parse_normalized_messages(value: Any) -> NormalizedRunInput
normalized_messages_to_wire(value: NormalizedRunInput) -> list[dict[str, Any]]
```

## Item contract

The ordered list contains only these closed items:

- `message` with role `system` or `developer` and one or more `input_text`
  parts;
- `message` with role `user` and one or more `input_text` or `input_image`
  parts;
- `message` with role `assistant` and one or more `output_text` parts;
- `function_call` with bounded `call_id`, function `name`, and `arguments`
  containing a JSON-encoded object;
- `function_call_output` with bounded `call_id` and either a string or one or
  more `input_text` or `input_image` parts.

An image part carries a nonempty `image_url` and optional `detail` of `auto`,
`low`, or `high`. The contract does not decide which URL schemes a provider
supports. A provider must reject an unsupported image before dispatch.

Function-call outputs keep their `call_id`. The matching call need not appear
in the same submitted list because it may belong to earlier continuation
history. Function arguments reject malformed JSON, non-object JSON, duplicate
keys, non-finite numbers, invalid UTF-8 in decoded keys or values, and values
outside the common JSON bounds.

## Invariants

- Wire input is a JSON list, limited to 256 items. Tuples and legacy scalar
  messages are not normalized wire input.
- The encoded messages envelope is limited to 1 MiB. Run admission retains the
  existing 1 MiB combined input, tools, and options limit.
- Items and content parts reject unknown fields. Host IDs, encrypted provider
  metadata, reasoning state, and other host-private fields are absent.
- Parse and serialization return deep-detached structures and preserve order.
- Every validation failure raises `NormalizedInputValidationError` with one
  fixed message, no chained internal exception, and no caller content.

## Host and provider extension

B10 host conversion may accept legacy shorthand at its own ingress, but it
must emit only this tagged union before calling `runs.start`. Host-specific agent messages, compaction,
reasoning, encrypted metadata, and custom-tool history require their existing
explicit conversion, continuation, or unsupported behavior; they cannot pass
through this codec as arbitrary JSON.

B13 and other provider adapters consume `NormalizedRunInput` and translate
every item. If a provider cannot represent a valid item or part, the adapter
fails before starting a billed request rather than omitting or coercing it.

`runs.start.input.messages`, the shared `run_request` vocabulary, and run
admission now require this definition. Already canonical payloads retain their
item order and bytes for admission hashing; omitted input and options keep their
existing defaults. Scalar strings and untagged `{role, content}` objects are
rejected rather than coerced.

Durable rows written by the earlier unreleased fixture path are not migrated by
this codec. Restart recovery reconstructs and dispatches stored input under the
repository's existing compatibility behavior; it does not reinterpret an old
row as newly admitted normalized input. A future storage-version migration must
make any stronger compatibility decision explicitly.

## Tests and current limit

Run from `python/`:

```sh
PYTHONPATH=src .venv/bin/python -m unittest tests.engine.test_run_input_codec
```

The focused suite validates the canonical source schema and codec behavior.
The generated Python and macOS contract bundles are updated by the repository
contract generator after canonical review; this slice does not edit them.

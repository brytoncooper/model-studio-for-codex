# Codex input normalization

`EngineRPC` and `CodexResponsesBridge` receive their rendezvous loader and
Unix-client factory from the outer composition root. Production startup wires
these in the CLI/bootstrap layer; host code does not select transport
adapters.

## Purpose and ownership

`input_normalization.py` is the Codex host adapter for conversation history. It
recognizes Codex Responses input and returns the engine-owned
`NormalizedRunInput` value. It performs no HTTP, credential, authorization,
tool execution, continuation-store, or provider work.

The public entry point is:

```python
normalize_codex_input(
    items,
    alias_map=None,
    *,
    decode_compaction=None,
    normalize_reasoning=None,
) -> NormalizedRunInput
```

`alias_map` is a trusted result of separate host tool-definition conversion.
It maps an engine-visible name to Codex's `(namespace, name)` identity. This
adapter uses it only to preserve function-call history; it does not authorize
or advertise a tool.

## Conversion contract

- System, developer, user, and assistant messages retain ordered text and
  supported images. Canonical role/part rules are enforced by the engine input
  codec.
- Function calls retain `call_id`, normalized name, and JSON arguments.
  Missing arguments default to `{}`; supplied arguments remain exact and must
  satisfy the engine's JSON-object text contract.
  Function results retain strings and supported text/image content. Text-only
  result parts keep the legacy newline join. Structured object results are
  serialized only after strict JSON validation; non-string keys, non-finite
  numbers, cycles, and scalar coercions are rejected.
- `agent_message` becomes ordinary user text with the legacy author prefix.
- A decoded compaction summary becomes ordinary user text with the legacy
  checkpoint prefix.
- Host IDs, phases, internal chat metadata, encrypted function fields, and
  other non-contract fields are absent from the result because every canonical
  item is reconstructed field by field.
- Custom/freeform tool history, continuation controls, unknown item kinds, and
  unknown material content fail explicitly instead of disappearing.

The function returns the engine codec's detached immutable container and does
not mutate the request or collaborator results.

## Continuation boundary

Compaction bytes and reasoning state remain opaque to this host adapter. A
caller may provide `decode_compaction(item) -> str | None` and
`normalize_reasoning(item) -> list[normalized item]`. The latter is the future
B15 seam and may deliberately return an empty list when continuation policy
has determined that an item carries no model-visible material. Without these
collaborators, the corresponding input fails before run admission.

Collaborator exceptions are replaced with a fixed display-safe error. No
encrypted payload or callback diagnostic is copied into the error message.

## Extension and limitations

Add a Codex item or content kind only when it has a lossless representation in
the normalized input union. Extend the engine contract first when it does not.
Provider serialization, custom-tool support, tool grants, and compaction
decoding belong to their respective systems and must not be added here.

From `python/`, run:

```sh
PYTHONPATH=src python3.12 -m unittest \
  tests.host_codex.test_input_normalization
```

The focused fixtures cover real Codex message/tool shapes, images, detached
output, collaboration messages, decoded compaction, opaque reasoning,
callback-error redaction, and explicit rejection of unknown material.

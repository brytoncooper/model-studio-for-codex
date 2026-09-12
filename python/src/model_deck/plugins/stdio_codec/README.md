# stdio private framing (B18)

Bounded newline-delimited UTF-8 JSON framing for private plugin stdio pipes.
Generic transport only: it frames JSON-RPC objects and enforces size/shape
rules, leaving method payload schema validation to a higher layer.

## API

- `encode_frame(payload: dict) -> bytes`: encode one object plus `\n`.
  Raises `CodecError(frame_too_large | non_finite_number | not_object | invalid_json)`.
  Keys must be strings; values must be finite JSON types; cycles rejected.
- `StdioCodec().feed(data: bytes) -> list[dict]`: append injected bytes,
  return each complete frame in order. The size cap applies per frame, so one
  chunk may carry several frames whose combined size exceeds the cap.
  The first bad or oversized frame permanently fails the codec: the buffer is
  cleared and every later `feed`/`finish` raises `CodecError(failed)`.
- `StdioCodec().finish() -> None`: raise `CodecError(truncated)` when bytes
  remain without a terminating newline; empty tail is accepted.
- `CodecError.code`: stable code, fixed message, never echoes input bytes.
- `MAX_FRAME_BYTES = 1048576`: encoded frame cap shared with `API.md` transport.

## Rules

Objects only; arrays (batches) and scalars are rejected. Strict JSON:
duplicate keys and non-finite numbers (`NaN`, `Infinity`, overflow literals
such as `1e999`, nested or not) are rejected.
Stdout carries protocol frames; diagnostics belong on stderr (caller-owned).
Pathological nesting that exhausts the interpreter stack is normalized to `CodecError(invalid_json)`; decode failures latch `failed` as usual. Ordinary content is never altered.

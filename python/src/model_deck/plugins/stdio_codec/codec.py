from __future__ import annotations
import json
import math
from typing import Any

MAX_FRAME_BYTES = 1_048_576

_VALID_CODES = frozenset({
    "empty_frame",
    "frame_too_large",
    "invalid_utf8",
    "invalid_json",
    "duplicate_key",
    "non_finite_number",
    "not_object",
    "batch_not_supported",
    "truncated",
    "failed",
})

_MESSAGES = {
    "empty_frame": "empty frame",
    "frame_too_large": "frame exceeds maximum size",
    "invalid_utf8": "frame is not valid UTF-8",
    "invalid_json": "frame is not valid JSON",
    "duplicate_key": "frame has a duplicate object key",
    "non_finite_number": "frame has a non-finite number",
    "not_object": "frame must be a JSON object",
    "batch_not_supported": "batch frames are not supported",
    "truncated": "stream ended mid-frame",
    "failed": "codec is in a failed state",
}


class CodecError(ValueError):
    """Stable framing failure with a machine-readable code and no input echo."""
    def __init__(self, code: str) -> None:
        if code not in _VALID_CODES:
            raise ValueError("unknown codec error code")
        self.code = code
        super().__init__(_MESSAGES[code])


def _reject_non_finite(value: str) -> None:
    raise CodecError("non_finite_number")


def _object_no_dupes(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CodecError("duplicate_key")
        result[key] = value
    return result


def _reject_nested_non_finite(value: Any) -> None:
    try:
        _reject_nested_non_finite_inner(value)
    except RecursionError:
        raise CodecError("invalid_json") from None


def _reject_nested_non_finite_inner(value: Any) -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CodecError("non_finite_number")
    elif isinstance(value, dict):
        for item in value.values():
            _reject_nested_non_finite_inner(item)
    elif isinstance(value, list):
        for item in value:
            _reject_nested_non_finite_inner(item)


def _parse_line(line: bytes) -> dict[str, Any]:
    if line.endswith(b"\r"):
        line = line[:-1]
    if not line:
        raise CodecError("empty_frame")
    if len(line) > MAX_FRAME_BYTES:
        raise CodecError("frame_too_large")
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        raise CodecError("invalid_utf8") from None
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_no_dupes,
            parse_constant=_reject_non_finite,
        )
    except CodecError:
        raise
    except (json.JSONDecodeError, ValueError, RecursionError):
        raise CodecError("invalid_json") from None
    _reject_nested_non_finite(value)
    if isinstance(value, list):
        raise CodecError("batch_not_supported")
    if not isinstance(value, dict):
        raise CodecError("not_object")
    return value


def _check_encodable(value: Any, seen: frozenset[int]) -> None:
    try:
        _check_encodable_inner(value, seen)
    except RecursionError:
        raise CodecError("invalid_json") from None


def _check_encodable_inner(value: Any, seen: frozenset[int]) -> None:
    if value is None or isinstance(value, (str, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CodecError("non_finite_number")
        return
    if isinstance(value, bool):
        return
    if isinstance(value, dict):
        if id(value) in seen:
            raise CodecError("invalid_json")
        child = seen | {id(value)}
        for key, item in value.items():
            if type(key) is not str:
                raise CodecError("invalid_json")
            _check_encodable_inner(item, child)
        return
    if isinstance(value, (list, tuple)):
        if id(value) in seen:
            raise CodecError("invalid_json")
        child = seen | {id(value)}
        for item in value:
            _check_encodable_inner(item, child)
        return
    raise CodecError("invalid_json")


def encode_frame(payload: dict[str, Any]) -> bytes:
    """Encode one JSON-RPC object frame; raises CodecError on oversize/unsafe values."""
    if not isinstance(payload, dict):
        raise CodecError("not_object")
    _check_encodable(payload, frozenset())
    try:
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except ValueError:
        raise CodecError("non_finite_number") from None
    except (TypeError, RecursionError):
        raise CodecError("invalid_json") from None
    if len(data) > MAX_FRAME_BYTES:
        raise CodecError("frame_too_large")
    return data + b"\n"


class StdioCodec:
    """Incremental newline-delimited frame decoder over injected bytes only."""
    def __init__(self) -> None:
        self._buffer = bytearray()
        self._failed = False

    def _fail(self, code: str) -> CodecError:
        self._failed = True
        self._buffer.clear()
        return CodecError(code)

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        """Append bytes and return each complete frame; fail closed on oversize."""
        if self._failed:
            raise CodecError("failed")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes")
        self._buffer += bytes(data)
        frames: list[dict[str, Any]] = []
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                if len(self._buffer) > MAX_FRAME_BYTES:
                    raise self._fail("frame_too_large")
                return frames
            line = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            try:
                frames.append(_parse_line(line))
            except CodecError as exc:
                raise self._fail(exc.code) from None

    def finish(self) -> None:
        """Reject a stream that ends with a partial frame; empty tail is fine."""
        if self._failed:
            raise CodecError("failed")
        if self._buffer:
            raise self._fail("truncated")

    def __len__(self) -> int:
        return len(self._buffer)

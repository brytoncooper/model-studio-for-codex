from __future__ import annotations

import json
from typing import Any

MAX_FRAME_BYTES = 1_048_576


class FrameError(ValueError):
    pass


def encode_frame(payload: dict[str, Any]) -> bytes:
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_FRAME_BYTES:
        raise FrameError("frame exceeds maximum size")
    return data + b"\n"


def decode_frame(buffer: bytearray) -> dict[str, Any] | None:
    newline = buffer.find(b"\n")
    if newline < 0:
        if len(buffer) > MAX_FRAME_BYTES:
            raise FrameError("frame exceeds maximum size")
        return None
    line = bytes(buffer[:newline])
    del buffer[: newline + 1]
    if not line:
        raise FrameError("empty frame")
    if len(line) > MAX_FRAME_BYTES:
        raise FrameError("frame exceeds maximum size")
    value = json.loads(line.decode("utf-8"))
    if not isinstance(value, dict):
        raise FrameError("frame must be a JSON object")
    return value

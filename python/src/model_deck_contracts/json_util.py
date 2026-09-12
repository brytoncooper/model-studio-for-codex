from __future__ import annotations

import json
from typing import Any


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def canonical_json_equal(a: Any, b: Any) -> bool:
    return canonical_json_bytes(a) == canonical_json_bytes(b)

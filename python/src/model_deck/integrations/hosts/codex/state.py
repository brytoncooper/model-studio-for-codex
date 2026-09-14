"""Adapter-owned atomic mapping for Codex threads and pending tool calls."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class BridgeState:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.data: dict[str, dict[str, Any]] = {"threads": {}, "pending": {}}
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return
        if not isinstance(loaded, dict):
            return
        threads = loaded.get("threads")
        pending = loaded.get("pending")
        if isinstance(threads, dict) and isinstance(pending, dict):
            self.data = {"threads": dict(threads), "pending": dict(pending)}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".bridge-", dir=self.path.parent
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(self.data, output, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_name, self.path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)


__all__ = ["BridgeState"]

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Violation:
    path: str
    rule_id: str
    message: str
    line: int | None = None
    severity: str = "error"
